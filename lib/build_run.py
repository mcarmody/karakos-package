"""Runners for queued builds and reviews (spec 3.3).

LocalRunner runs bin/invoke-builder.sh / invoke-reviewer.sh as the leader of its
own session (so the whole tree is one process group). SshRunner ships the brief
and the system prompt by CONTENT over ssh stdin and runs bin/build-runner.sh by
piping it to `bash -s`; no path of the dispatching host and no secret reaches the
remote. Cancel never signals the local ssh client: it reads the remote pid file
and signals the remote group.

Signalling is guarded: a group is signalled only when its pid is > 1, leads its
own group, is not ours or an ancestor's, and still has the start time recorded
when the run began. Signalling functions are injectable so tests record instead
of sending.
"""
import asyncio
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import build_hosts
import procinfo
import procreap

log = logging.getLogger("build_run")

PKG_ROOT = Path(__file__).resolve().parent.parent
RUNNER_SCRIPT = PKG_ROOT / "bin" / "build-runner.sh"
KILL_GRACE_S = 10.0          # remote TERM -> KILL wait (tests patch this)
LOCAL_GRACE_S = 3.0


class HostUnreachable(Exception):
    pass


@dataclass
class RunResult:
    exit_code: object = None            # int, or "timeout"
    result_text: str = ""
    report: Optional[dict] = None       # the runner's karakos_build_result
    cost_usd: float = 0.0
    duration_s: float = 0.0
    note: str = ""


def parse_stream(path):
    """-> (result_event or None, report or None) from a stream-json file."""
    result, report = None, None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "result":
                    result = ev
                elif isinstance(ev.get("karakos_build_result"), dict):
                    report = ev["karakos_build_result"]
    except OSError:
        pass
    return result, report


def _result_of(path, rc, started_at, now_fn) -> RunResult:
    result, report = parse_stream(path)
    code = rc
    if report is not None and isinstance(report.get("exit"), int) and rc in (0, None):
        code = report["exit"]
    return RunResult(
        exit_code=code,
        result_text=(result or {}).get("result", "") or "",
        report=report,
        cost_usd=float((result or {}).get("total_cost_usd") or 0.0),
        duration_s=max(0.0, now_fn() - started_at))


# --- guarded local signalling ------------------------------------------------

def kill_local_group(run_ref, signal_fn=os.killpg, snapshot_fn=procreap.snapshot_tree,
                     reap_fn=procreap.reap, grace=None) -> str:
    """Stop a local run recorded in `run_ref` -> termed | killed | gone | failed.
    Never trusts the record blindly (see the module docstring)."""
    try:
        pid = int(run_ref.get("pid"))
        pgid = int(run_ref.get("pgid"))
        st = run_ref.get("starttime")
    except (TypeError, ValueError, AttributeError):
        return "failed"
    if pid <= 1 or pgid != pid or pid == os.getpid() or pgid == os.getpgrp():
        return "failed"
    if st is None or procinfo.starttime(pid) != st or procinfo.proc_state(pid) in (None, "Z"):
        return "gone"
    snap = snapshot_fn(pid)
    if not snap:
        return "failed"
    try:
        signal_fn(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        return "failed"
    out = reap_fn(snap, grace=LOCAL_GRACE_S if grace is None else grace)
    if out.get("survived"):
        return "failed"
    return "killed" if out.get("killed") else "termed"


# --- remote ------------------------------------------------------------------

REMOTE_KILL = r'''
D="$1"; GRACE="$2"
D="${D/#\~/$HOME}"
[ -f "$D/exit" ] && { echo gone; exit 0; }
[ -f "$D/run.pid" ] || { echo gone; exit 0; }
PID=$(sed -n 1p "$D/run.pid"); PG=$(sed -n 2p "$D/run.pid")
case "$PG" in ''|*[!0-9]*) echo failed; exit 0;; esac
case "$PID" in ''|*[!0-9]*) echo failed; exit 0;; esac
[ "$PG" -gt 1 ] && [ "$PG" = "$PID" ] || { echo failed; exit 0; }
MYPG=$(ps -o pgid= -p $$ | tr -d ' ')
[ "$PG" = "$MYPG" ] && { echo failed; exit 0; }
kill -0 -- "-$PG" 2>/dev/null || { echo gone; exit 0; }
kill -TERM -- "-$PG" 2>/dev/null || { echo failed; exit 0; }
i=0; n=$(( GRACE * 10 ))
while [ "$i" -lt "$n" ]; do
    [ -f "$D/exit" ] && { echo termed; exit 0; }
    kill -0 -- "-$PG" 2>/dev/null || { echo termed; exit 0; }
    sleep 0.1; i=$(( i + 1 ))
done
[ -f "$D/exit" ] && { echo termed; exit 0; }
kill -KILL -- "-$PG" 2>/dev/null
echo killed
'''


def remote_dir(host_cfg, qid) -> str:
    return f"{host_cfg.workdir.rstrip('/')}/{qid}"


def _remote_path(p: str) -> str:
    """A path for a remote shell command: a leading ~/ expands on the remote."""
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def remote_kill_sync(host_cfg, qid, grace=None, exec_fn=None) -> str:
    """-> termed | killed | gone | failed. One ssh call: the guarded kill script on
    stdin; the pgid comes from the remote's own run.pid."""
    grace = KILL_GRACE_S if grace is None else grace
    argv = build_hosts.ssh_argv(host_cfg.target, shlex.join(
        ["bash", "-s", "--", remote_dir(host_cfg, qid), str(int(max(1, round(grace))))]))
    try:
        if exec_fn is not None:
            rc, out = exec_fn(argv, REMOTE_KILL)
        else:
            p = subprocess.run(argv, input=REMOTE_KILL, capture_output=True, text=True,
                               timeout=grace + 60, start_new_session=True)
            rc, out = p.returncode, p.stdout
    except Exception as e:  # noqa: BLE001
        log.warning("remote kill failed: %s", e)
        return "failed"
    word = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    if rc != 0 or word not in ("termed", "killed", "gone", "failed"):
        return "failed"
    return word


# --- runners -----------------------------------------------------------------

class _Base:
    def __init__(self, workspace, clock=None):
        import time
        self.workspace = Path(workspace)
        self.clock = clock or time.time
        self.state = self.workspace / "data" / "build-queue"
        (self.state / "runs").mkdir(parents=True, exist_ok=True)
        (self.state / "briefs").mkdir(parents=True, exist_ok=True)

    def stream_path(self, qid) -> Path:
        return self.state / "runs" / f"{qid}.jsonl"


class LocalRunner(_Base):
    def __init__(self, workspace, script_dir=None, clock=None, signal_fn=os.killpg,
                 reap_fn=procreap.reap, snapshot_fn=procreap.snapshot_tree):
        super().__init__(workspace, clock)
        ws_bin = self.workspace / "bin"
        self.script_dir = Path(script_dir) if script_dir else (ws_bin if ws_bin.is_dir()
                                                              else PKG_ROOT / "bin")
        self.signal_fn, self.reap_fn, self.snapshot_fn = signal_fn, reap_fn, snapshot_fn

    async def run(self, row, cfg, host_cfg, set_ref, **_) -> RunResult:
        qid = row["id"]
        brief = self.state / "briefs" / f"{qid}.md"     # the id, never the user's filename
        brief.write_text(row["brief"])
        script = self.script_dir / ("invoke-builder.sh" if row["kind"] == "build"
                                    else "invoke-reviewer.sh")
        env = dict(os.environ)
        env["KARAKOS_QUEUE_RUN"] = "1"
        env["WORKSPACE_ROOT"] = str(self.workspace)
        started = self.clock()
        out = open(self.stream_path(qid), "wb")
        err = open(self.state / "runs" / f"{qid}.err", "wb")
        try:
            proc = await asyncio.create_subprocess_exec(
                str(script), str(brief), stdin=asyncio.subprocess.DEVNULL, stdout=out,
                stderr=err, env=env, cwd=str(self.workspace), start_new_session=True)
        finally:
            out.close()
            err.close()
        set_ref({"pid": proc.pid, "pgid": proc.pid, "starttime": procinfo.starttime(proc.pid),
                 "dir": str(self.state), "host": host_cfg.name})
        rc = await proc.wait()
        return _result_of(self.stream_path(qid), rc, started, self.clock)

    async def kill(self, row, run_ref) -> str:
        return await asyncio.to_thread(
            kill_local_group, run_ref or {}, self.signal_fn, self.snapshot_fn, self.reap_fn)

    def cleanup(self, row, run_ref) -> str:
        return kill_local_group(run_ref or {}, self.signal_fn, self.snapshot_fn, self.reap_fn,
                                grace=1.0)


class SshRunner(_Base):
    def __init__(self, workspace, system_prompt_fn: Callable, clock=None, exec_fn=None,
                 kill_exec_fn=None):
        super().__init__(workspace, clock)
        self.system_prompt_fn = system_prompt_fn      # (kind, row) -> system prompt text
        self.kill_exec_fn = kill_exec_fn

    async def _upload(self, host_cfg, dir_, name, content):
        cmd = f"mkdir -p {_remote_path(dir_)} && cat > {_remote_path(dir_ + '/' + name)}"
        proc = await asyncio.create_subprocess_exec(
            *build_hosts.ssh_argv(host_cfg.target, cmd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        _, err = await proc.communicate(content.encode())
        if proc.returncode == 255:
            raise HostUnreachable(f"ssh exit 255 uploading {name}")
        if proc.returncode != 0:
            raise RuntimeError(f"upload of {name} failed: exit {proc.returncode}")

    async def run(self, row, cfg, host_cfg, set_ref, branch_prefix="", **_) -> RunResult:
        qid = row["id"]
        dir_ = remote_dir(host_cfg, qid)
        started = self.clock()
        await self._upload(host_cfg, dir_, "brief.md", row["brief"])
        await self._upload(host_cfg, dir_, "system.md", self.system_prompt_fn(row["kind"], row))
        args = [row["kind"], qid, row["repo"] or "", row["target_branch"] or "",
                os.environ.get("MODEL", "sonnet"),
                str(cfg.timeout_s("build" if row["kind"] == "build" else "review")),
                f"{cfg.cost_ceiling_usd:g}", dir_, branch_prefix, host_cfg.repo_url,
                host_cfg.workdir]
        cmd = "bash -s -- " + " ".join(shlex.quote(a) for a in args)
        out = open(self.stream_path(qid), "wb")
        try:
            proc = await asyncio.create_subprocess_exec(
                *build_hosts.ssh_argv(host_cfg.target, cmd),
                stdin=asyncio.subprocess.PIPE, stdout=out, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
        finally:
            out.close()
        set_ref({"pid": proc.pid, "pgid": proc.pid, "starttime": procinfo.starttime(proc.pid),
                 "dir": dir_, "host": host_cfg.name})
        try:
            proc.stdin.write(RUNNER_SCRIPT.read_bytes())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        rc = await proc.wait()
        res = _result_of(self.stream_path(qid), rc, started, self.clock)
        if rc == 255 and res.report is None:
            res.note = "ssh connection lost"
        return res

    async def kill(self, row, run_ref, host_cfg=None) -> str:
        if host_cfg is None:
            return "failed"
        word = await asyncio.to_thread(remote_kill_sync, host_cfg, row["id"], None,
                                       self.kill_exec_fn)
        return word

    def cleanup(self, row, run_ref, host_cfg=None) -> str:
        """After a dispatcher restart: stop the local ssh client (guarded) and, best
        effort, the remote group."""
        local = kill_local_group(run_ref or {}, grace=1.0)
        if host_cfg is not None:
            remote = remote_kill_sync(host_cfg, row["id"], 2, self.kill_exec_fn)
            return f"{local}/{remote}"
        return local
