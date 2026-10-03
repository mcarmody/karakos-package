"""Queue dispatcher hosted by the relay (spec 3.3).

`QueueDispatcher.tick()` ingests the inbox directories, recovers rows left
running by a previous dispatcher (once), claims rows until every host is full
or nothing is claimable, starts a runner task per claim, reaps finished tasks,
stops cancelled runs, and fails rows held for a host that stayed unreachable
past its grace. The relay's own DispatchAdapter is not constructed when the
queue is enabled, so the two never both consume the inbox.
"""
import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import build_hosts
import build_run
import buildq

try:
    import heartbeats
except Exception:  # noqa: BLE001  (3.2 not merged: degrade)
    heartbeats = None
try:
    import job_registry
except Exception:  # noqa: BLE001
    job_registry = None
try:
    import usage_governor
except Exception:  # noqa: BLE001  (2.7 not merged: degrade)
    usage_governor = None

log = logging.getLogger("build_dispatcher")

INBOX_KIND = {"builder": "build", "reviewer": "review"}
HEARTBEAT_EVERY_S = 15
UNREACHABLE_RETRY_S = 30
MIGRATE_MSG = "run karakos migrate"


def _now_ts(clock) -> float:
    return float(clock())


def make_governor(workspace, config) -> Optional[Callable]:
    """Adapter for machine-originated rows: (row, deferred_before) -> (ok, reason).
    None when 2.7's governor is absent or the config bypasses it. Fails open."""
    if usage_governor is None or not config.governor:
        return None
    ws = Path(workspace)

    def check(row, deferred_before=False, now=None):
        try:
            policy = usage_governor.Policy.load(ws / "config" / "governor.yaml")
            db = None
            for cand in (ws / "data" / "memory" / "agent-server.db", ws / "data" / "agent-server.db"):
                if cand.is_file():
                    db = cand
                    break
            pct = usage_governor.weekly_pct_sync(db, now) if db else None
            d = usage_governor.decide(f"build-queue:{row['repo'] or 'none'}", pct, policy,
                                      deferred_before)
            if d.decision == "defer":
                return False, f"governor: {d.reason} (seven-day {pct:g}%)"
        except Exception as e:  # noqa: BLE001
            log.warning("governor check failed open: %s", e)
        return True, ""
    return check


def poke_notifier(workspace) -> Callable:
    script = Path(workspace) / "bin" / "poke.sh"

    def notify(requester, channel, message):
        if not requester:
            return
        try:
            subprocess.run([str(script), "--agent", requester, "--source", "build-queue",
                            "--reply-channel", channel or "general", message],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                           env=_poke_env(workspace))
        except Exception as e:  # noqa: BLE001
            log.warning("notify %s failed: %s", requester, e)
    return notify


def _poke_env(workspace):
    import os
    env = dict(os.environ)
    env["WORKSPACE_ROOT"] = str(workspace)
    return env


def post_cost_default(agent, cost, duration_s):
    import os
    import urllib.request
    try:
        payload = json.dumps({"agent": agent, "cost_delta": cost, "session_total": cost,
                              "duration_ms": int(duration_s * 1000)}).encode()
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("AGENT_SERVER_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        port = os.environ.get("AGENT_SERVER_PORT", "18791")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/cost", data=payload,
                                     headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception:  # noqa: BLE001
        pass


class QueueDispatcher:
    def __init__(self, db_path, config, runners, clock=time.time, governor=None, *,
                 workspace, registry_loader=None, notifier=None, cost_poster=None,
                 probe_runner=None, poll_s=15):
        self.db_path = Path(db_path)
        self.config = config
        self.runners = runners
        self.clock = clock
        self.governor = governor
        self.workspace = Path(workspace)
        self.poll_s = poll_s
        self.notify = notifier or poke_notifier(workspace)
        self.post_cost = cost_poster or post_cost_default
        self.probes = build_hosts.ProbeCache(probe_runner, clock)
        self._registry_loader = registry_loader
        self.conn = None
        self.tasks: dict = {}          # id -> asyncio.Task (the run)
        self.killing: dict = {}        # id -> asyncio.Task (a cancel in flight)
        self._cancelling: set = set()
        self._kill_issued: set = set()   # cancelled rows whose kill already ran (once each)
        self.refs: dict = {}
        self._recovered = False
        self._last_beat = 0.0
        self._unreach: dict = {}       # host -> first seen unreachable (ts)
        self._retry_at: dict = {}
        self._stop = False
        self.inbox_root = self.workspace / "inbox"

    # -- setup ---------------------------------------------------------------

    def open(self):
        if self.conn is None:
            if not self.db_path.is_file():
                raise SystemExit(MIGRATE_MSG)
            self.conn = buildq.connect(self.db_path)
            if not buildq.tables_present(self.conn):
                raise SystemExit(MIGRATE_MSG)
        return self.conn

    def registry(self):
        if self._registry_loader:
            return self._registry_loader()
        import registry
        return registry.load_registry(self.workspace)

    def role_agent(self, kind) -> Optional[str]:
        try:
            agents = self.registry().by_role(buildq.ROLE_OF_KIND[kind])
            return agents[0].id if agents else None
        except Exception:  # noqa: BLE001
            return None

    # -- ingest --------------------------------------------------------------

    def ingest_inboxes(self) -> list:
        conn = self.open()
        out = []
        try:
            reg = self.registry()
        except Exception as e:  # noqa: BLE001
            log.error("registry unreadable, ingest skipped: %s", e)
            return out
        for typ, kind in INBOX_KIND.items():
            d = self.inbox_root / typ
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime):
                try:
                    text = f.read_text(errors="replace")
                except OSError:
                    continue
                meta = buildq.parse_frontmatter(text)
                origin = meta.get("origin") if meta.get("origin") in buildq.ORIGINS else "human"
                try:
                    prio = int(meta.get("priority") or 0)
                except ValueError:
                    prio = 0
                try:
                    qid = buildq.enqueue(
                        conn, kind, text, origin=origin, source=f"inbox:{typ}", priority=prio,
                        source_ref=buildq.sha256_text(text), now=self.clock(), registry=reg)
                except buildq.BriefError as e:
                    self._move(f, d / "rejected", reason=str(e))
                    req = (meta.get("requester") or "").strip()
                    if req and buildq.AGENT_RE.match(req):
                        self.notify(req, meta.get("callback_channel") or "general",
                                    f"Brief {f.name} was rejected by the build queue: {e}")
                    continue
                self._move(f, d / "queued")
                out.append(qid)
        return out

    @staticmethod
    def _move(f: Path, dest_dir: Path, reason=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f.name
        if dest.exists():
            dest = dest_dir / f"{f.stem}.{int(time.time() * 1000)}{f.suffix}"
        shutil.move(str(f), str(dest))
        if reason is not None:
            Path(str(dest) + ".reason").write_text(reason + "\n")

    # -- slots, admission ----------------------------------------------------

    def free_slots(self) -> dict:
        conn = self.open()
        used = {r[0]: r[1] for r in conn.execute(
            "SELECT exec_host, COUNT(*) FROM build_queue WHERE status='running' GROUP BY exec_host")}
        return {name: hc.concurrency - used.get(name, 0) for name, hc in self.config.hosts.items()}

    def gate(self, row):
        now = _now_ts(self.clock)
        host = buildq.resolve_host(row, self.config.default_host)
        hc = self.config.host(host)
        if hc is None:
            return False, f"unknown-host: {host}"
        data, unreachable = self.probes.get(hc)
        if unreachable:
            self._unreach.setdefault(host, now)
            return False, "host-unreachable"
        if host in self._unreach:
            if hc.probe:                      # a probe answered: the host is back
                self._unreach.pop(host, None)
            elif now < self._retry_at.get(host, 0):
                return False, "host-unreachable"
            else:
                self._retry_at[host] = now + UNREACHABLE_RETRY_S
        adm = build_hosts.admit(hc, data)
        if not adm.ok:
            return False, f"host-busy: {adm.reason}"
        if row["origin"] == "machine" and self.governor is not None:
            before = self.conn.execute(
                "SELECT 1 FROM build_queue_events WHERE queue_id=? AND event='governor' LIMIT 1",
                (row["id"],)).fetchone() is not None
            ok, reason = self.governor(row, before)
            if not ok:
                return False, reason or "governor"
        return True, ""

    def _fail_unknown_hosts(self):
        """A row naming a host that is not in the config can never run: fail it."""
        names = list(self.config.hosts)
        marks = ",".join("?" * len(names))
        for row in self.conn.execute(
                f"SELECT * FROM build_queue WHERE status='queued' AND host IS NOT NULL "
                f"AND host NOT IN ({marks})", names).fetchall():
            buildq.finish(self.conn, row["id"], "failed", "unknown-host", None, self.clock())
            self._tell(row, "failed", "unknown-host", None)

    def _fail_unreachable(self):
        now = _now_ts(self.clock)
        grace = self.config.unreachable_grace_s
        for host, since in list(self._unreach.items()):
            if now - since < grace:
                continue
            for row in self.conn.execute(
                    "SELECT * FROM build_queue WHERE status='queued' AND COALESCE(host, ?)=?",
                    (self.config.default_host, host)).fetchall():
                buildq.finish(self.conn, row["id"], "failed", "host-unreachable", None, now)
                self._tell(row, "failed", "host-unreachable", None)
            self._unreach[host] = now   # restart the window for rows queued later

    # -- tick ----------------------------------------------------------------

    def _cleanup_row(self, row):
        ref = {}
        try:
            ref = json.loads(row["run_ref"]) if row["run_ref"] else {}
        except ValueError:
            pass
        host = self.config.host(ref.get("host") or row["exec_host"] or "")
        runner = self.runners.get(host.kind) if host else None
        if runner is None or not ref:
            return
        if host.kind == "ssh":
            word = runner.cleanup(row, ref, host)
        else:
            word = runner.cleanup(row, ref)
        buildq.add_event(self.conn, row["id"], "recover-cleanup", word)

    async def tick(self):
        conn = self.open()
        now = _now_ts(self.clock)
        if not self._recovered:
            self._recovered = True
            buildq.recover(conn, now, self._cleanup_row, self.config.max_attempts)
        self.ingest_inboxes()
        await self._reap_finished()
        self._poll_cancels()
        self._fail_unknown_hosts()
        self._fail_unreachable()
        caps = self.free_slots()
        while any(v > 0 for v in caps.values()):
            row = buildq.claim_next(conn, caps, self.clock(), self.gate, self.config.default_host)
            if row is None:
                break
            caps[row["exec_host"]] = caps.get(row["exec_host"], 0) - 1
            self.tasks[row["id"]] = asyncio.ensure_future(self._run_row(row))
        self._beat(now)

    def _beat(self, now):
        if now - self._last_beat < HEARTBEAT_EVERY_S:
            return
        self._last_beat = now
        if heartbeats is not None:
            try:
                heartbeats.touch(self.workspace, "build-dispatcher")
            except Exception as e:  # noqa: BLE001
                log.debug("heartbeat failed: %s", e)

    async def _reap_finished(self):
        for qid, t in list(self.tasks.items()):
            if t.done():
                self.tasks.pop(qid, None)
                try:
                    t.result()
                except Exception as e:  # noqa: BLE001
                    log.error("run task for %s crashed: %r", qid, e)
        for qid, t in list(self.killing.items()):
            if t.done():
                self.killing.pop(qid, None)

    def _poll_cancels(self):
        for qid in list(self.tasks):
            if qid in self.killing or qid in self._cancelling or qid in self._kill_issued:
                continue
            row = buildq.get(self.conn, qid)
            if row is not None and row["status"] == "cancelled":
                self._kill_issued.add(qid)
                self.killing[qid] = asyncio.ensure_future(self._kill_cancelled(row))

    # -- running -------------------------------------------------------------

    def _runner_for(self, row):
        hc = self.config.host(row["exec_host"])
        return hc, self.runners.get(hc.kind)

    def _kill_args(self, hc, row):
        ref = self.refs.get(row["id"]) or {}
        return (row, ref, hc) if hc.kind == "ssh" else (row, ref)

    async def _kill_run(self, row) -> str:
        hc, runner = self._runner_for(row)
        return await runner.kill(*self._kill_args(hc, row))

    def _record_kill(self, row, word):
        ssh = self.config.host(row["exec_host"]).kind == "ssh"
        buildq.add_event(self.conn, row["id"], "remote-kill" if ssh else "local-kill",
                         f"remote-kill: {word}" if ssh else f"kill: {word}", self.clock())
        if word == "failed":
            self._kill_issued.discard(row["id"])      # a later cancel may try again
            self.conn.execute("UPDATE build_queue SET status='running', reason='cancel-failed', "
                              "finished_at=NULL WHERE id=?", (row["id"],))
            self.conn.commit()
            buildq.add_event(self.conn, row["id"], "cancel-failed",
                             "kill did not stop the run", self.clock())

    async def _kill_cancelled(self, row):
        """The CLI set `cancelled` on a running row; stop the work and record the kill."""
        try:
            self._record_kill(row, await self._kill_run(row))
        finally:
            self.killing.pop(row["id"], None)

    async def cancel(self, qid) -> Optional[str]:
        """Cancel now: a queued row at once; a running row after its kill result is
        recorded as an event (a failed kill leaves it `running`, reason
        `cancel-failed`). -> previous status."""
        row = buildq.get(self.open(), qid)
        if row is None:
            return None
        if row["status"] != "running" or qid not in self.tasks:
            return buildq.cancel(self.conn, qid, self.clock())
        self._cancelling.add(qid)
        try:
            word = await self._kill_run(row)
            self._record_kill(row, word)
            if word != "failed":
                self._kill_issued.add(qid)
                buildq.cancel(self.conn, qid, self.clock())
        finally:
            self._cancelling.discard(qid)
        return "running"

    async def _run_row(self, row):
        conn = self.conn
        qid = row["id"]
        hc, runner = self._runner_for(row)
        if runner is None or hc is None:
            buildq.finish(conn, qid, "failed", "unknown-host", None, self.clock())
            return
        agent = self.role_agent(row["kind"])
        meta = buildq.parse_frontmatter(row["brief"])
        prefix = meta.get("branch_prefix") or (f"{agent}/" if agent else "")

        def set_ref(ref):
            self.refs[qid] = ref
            buildq.set_run_ref(conn, qid, ref)

        timeout = self.config.timeout_s(row["kind"])
        task = asyncio.ensure_future(runner.run(row, self.config, hc, set_ref,
                                                branch_prefix=prefix))
        res = None
        try:
            res = await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            word = await self._kill_run(row)
            buildq.add_event(conn, qid, "timeout-kill", word, self.clock())
            try:
                await asyncio.wait_for(task, 30)
            except Exception:  # noqa: BLE001
                task.cancel()
            res = build_run.RunResult(exit_code="timeout")
        except build_run.HostUnreachable as e:
            self._unreach.setdefault(hc.name, _now_ts(self.clock))
            self._retry_at[hc.name] = _now_ts(self.clock) + UNREACHABLE_RETRY_S
            conn.execute("UPDATE build_queue SET status='queued', exec_host=NULL, "
                         "attempts=MAX(attempts-1,0), started_at=NULL, run_ref=NULL "
                         "WHERE id=? AND status='running'", (qid,))
            conn.commit()
            buildq.add_event(conn, qid, "host-unreachable", str(e), self.clock())
            return
        except Exception as e:  # noqa: BLE001
            log.error("runner error for %s: %r", qid, e)
            res = build_run.RunResult(exit_code="error", note=repr(e))
        finally:
            self.refs.pop(qid, None)
        self._unreach.pop(hc.name, None) if hc.kind == "ssh" and res.exit_code != 255 else None

        while qid in self.killing or qid in self._cancelling:
            await asyncio.sleep(0.05)     # a cancel is stopping this run: it owns the row
        self._kill_issued.discard(qid)
        cur = buildq.get(conn, qid)
        if cur is None or cur["status"] != "running":
            return                       # cancelled while running: the kill path owns it
        code = res.exit_code
        if code == "error":
            status, reason = "failed", "runner-error"
        else:
            status, reason = buildq.verify_outcome(row["kind"], code, res.result_text, res.report)
        report = res.report or {}
        result = {"exit": code, "pr_url": buildq.pr_url_of(res.result_text, res.report),
                  "branch": report.get("branch"), "pushed_sha": report.get("pushed_sha"),
                  "cost_usd": res.cost_usd, "duration_s": round(res.duration_s, 2)}
        if report.get("salvaged"):
            result["salvaged"] = True
        buildq.finish(conn, qid, status, reason, result, self.clock())
        if hc.kind == "ssh" and agent and res.cost_usd:
            try:
                self.post_cost(agent, res.cost_usd, res.duration_s)
            except Exception:  # noqa: BLE001
                pass
        self._tell(row, status, reason, result, res)

    def _tell(self, row, status, reason, result, res=None):
        req = row["requester"]
        if not req:
            return
        qid, kind = row["id"], row["kind"]
        if status == "done":
            if kind == "build":
                msg = f"Build {qid} complete. PR: {result['pr_url']}"
            else:
                m = re.search(r"Verdict:\s*(APPROVE|REVISE|RETHINK)", (res.result_text if res else "")
                              or "", re.I)
                msg = f"Review {qid} complete. Verdict: {m.group(1).upper() if m else 'UNKNOWN'}"
        elif reason == "no-pr":
            pushed = (result or {}).get("pushed_sha")
            msg = (f"Build {qid} exited cleanly but produced nothing: no PR"
                   + (f"; a branch was pushed ({result.get('branch')})." if pushed
                      else " and no pushed branch. Check the brief's repo and target branch."))
        elif reason == "empty-review":
            msg = f"Review {qid} exited cleanly but produced nothing: the result was empty."
        elif reason == "timeout":
            msg = f"{kind.title()} {qid} failed: timed out."
        else:
            msg = f"{kind.title()} {qid} failed ({reason})."
        try:
            self.notify(req, row["callback_channel"] or "general", msg)
        except Exception as e:  # noqa: BLE001
            log.warning("notify failed: %s", e)

    # -- loop ----------------------------------------------------------------

    async def run_forever(self, poll_s=None):
        poll_s = poll_s or self.poll_s
        self._register_component()
        try:
            while not self._stop:
                try:
                    await self.tick()
                except SystemExit:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.error("dispatcher tick error: %r", e)
                await asyncio.sleep(poll_s)
        finally:
            self._unregister_component()

    async def start(self):
        self._task = asyncio.ensure_future(self.run_forever())
        log.info("Build queue dispatcher started")

    async def stop(self):
        """Stop ticking. Running rows are NOT cancelled: `recover` re-examines them
        at the next start."""
        self._stop = True
        t = getattr(self, "_task", None)
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, SystemExit):
                pass

    def _register_component(self):
        if job_registry is not None and self.config.enabled:
            try:
                job_registry.register(job_registry.Job(
                    "build-dispatcher", "component", None, None, 120))
            except Exception as e:  # noqa: BLE001
                log.debug("job registry: %s", e)

    def _unregister_component(self):
        if job_registry is not None:
            try:
                job_registry.unregister("build-dispatcher")
            except Exception:  # noqa: BLE001
                pass


def build_dispatcher(workspace, config, **kw) -> "QueueDispatcher":
    """The wiring the relay uses (and the CLI's `ingest`)."""
    ws = Path(workspace)

    def system_prompt(kind, row):
        import prompt_compose
        agent = qd.role_agent(kind)
        return prompt_compose.compose_system_prompt(ws, agent)

    runners = {"local": build_run.LocalRunner(ws), "ssh": build_run.SshRunner(ws, system_prompt)}
    qd = QueueDispatcher(ws / "data" / "build-queue.db", config, runners,
                         governor=make_governor(ws, config), workspace=ws, **kw)
    return qd
