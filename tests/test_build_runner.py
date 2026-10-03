"""bin/build-runner.sh under the fake claude and a local bare repo (spec 3.3).

The runner is piped to `bash -s` exactly as the dispatcher does over ssh. Only
processes this test started are ever signalled.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildq_helpers as bq  # noqa: E402
from buildq_helpers import ROOT, branches, git, make_remote, write_script  # noqa: E402

RUNNER = ROOT / "bin" / "build-runner.sh"
ID = "bq-0123456789ab"


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = bq.isolate(monkeypatch, tmp_path)
    e.base = make_remote(tmp_path)
    e.dir = e.rhome / "karakos-builds" / ID
    e.dir.mkdir(parents=True)
    (e.dir / "brief.md").write_text("build it\n")
    (e.dir / "system.md").write_text("system\n")
    return e


def start(env, kind="build", prefix="bot/", timeout=120, branch="main", repo=bq.REPO):
    args = [kind, ID, repo, branch, "sonnet", str(timeout), "5", f"~/karakos-builds/{ID}", prefix,
            f"file://{env.base}/{{repo}}.git", "~/karakos-builds"]
    e = dict(os.environ, HOME=str(env.rhome))
    return subprocess.Popen(["bash", "-s", "--", *args], stdin=open(RUNNER), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, env=e, start_new_session=True)


def finish(proc, timeout=60):
    out, _ = proc.communicate(timeout=timeout)
    last = [l for l in out.splitlines() if l.strip()][-1]
    return out, json.loads(last)["karakos_build_result"]


def remote_sha(env, branch):
    r = subprocess.run(["git", "--git-dir", str(bq.bare_path(env.base)), "rev-parse", branch],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def pgid_of(env):
    pid, pg = (env.dir / "run.pid").read_text().split()
    assert pid == pg and int(pg) > 1
    return int(pg)


def test_normal_build_reports_a_verified_push(env):
    write_script(env.script, {"text": "opened https://example.invalid/owner/name/pull/1",
                              "commit_push": {"path": "new.txt", "content": "x\n"}})
    out, rep = finish(start(env))
    assert rep["exit"] == 0 and rep["salvaged"] is False
    assert rep["branch"] == f"bot/{ID}"
    assert rep["pr_url"] == "https://example.invalid/owner/name/pull/1"
    assert rep["pushed_sha"] and rep["pushed_sha"] == remote_sha(env, f"bot/{ID}")
    assert (env.dir / "exit").read_text().strip() == "0"
    assert not (env.dir / "work").exists()                     # nothing unsaved: removed
    # last line is the one JSON line
    assert out.strip().splitlines()[-1].startswith('{"karakos_build_result"')


def test_nothing_produced_is_reported_as_nothing(env):
    write_script(env.script, {"no_pr": True, "text": "I did nothing"})
    out, rep = finish(start(env))
    assert rep["exit"] == 0 and rep["pr_url"] == "" and rep["pushed_sha"] == ""


def test_pid_file_exists_before_claude_starts(env):
    write_script(env.script, {"text": "ok"})
    finish(start(env))
    rec = json.loads((env.logs / "prompts.jsonl").read_text().splitlines()[0])
    assert rec["run_pid_exists"] is True
    assert (env.dir / "run.pid").read_text().split()[0].isdigit()


def test_no_secret_or_workspace_variable_reaches_claude(env, monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_TOKEN", "sekret")
    write_script(env.script, {"text": "ok"})
    # the runner is started by the test, not through ssh, so scrub like ssh does
    args = ["build", ID, bq.REPO, "main", "sonnet", "120", "5", f"~/karakos-builds/{ID}", "bot/",
            f"file://{env.base}/{{repo}}.git", "~/karakos-builds"]
    clean = {k: v for k, v in os.environ.items() if k != "AGENT_SERVER_TOKEN"}
    p = subprocess.Popen(["bash", "-s", "--", *args], stdin=open(RUNNER), stdout=subprocess.PIPE,
                         text=True, env=dict(clean, HOME=str(env.rhome)), start_new_session=True)
    finish(p)
    rec = json.loads((env.logs / "prompts.jsonl").read_text().splitlines()[0])
    assert "AGENT_SERVER_TOKEN" not in rec["env_keys"]
    assert "--max-budget-usd" in rec["argv"] and rec["argv"][rec["argv"].index("--max-budget-usd") + 1] == "5"
    assert "--dangerously-skip-permissions" in rec["argv"]


def test_pre_push_hook_refuses_the_target_branch(env):
    write_script(env.script, {"commit_push": {"path": "a.txt", "content": "a", "to": "main"}})
    before = remote_sha(env, "main")
    out, rep = finish(start(env))
    push = json.loads((env.logs / "push.log").read_text().splitlines()[0])
    assert push["rc"] != 0 and "pre-push" in push["stderr"]
    assert remote_sha(env, "main") == before
    assert rep["pushed_sha"] == ""                       # the local branch was never pushed


def test_pre_push_hook_refuses_other_refs_and_force(env, tmp_path):
    seed = tmp_path / "seed"
    git(seed, "checkout", "-q", "-b", "bot/diverged", home=tmp_path)
    (seed / "other.txt").write_text("other\n")
    git(seed, "add", "-A", home=tmp_path)
    git(seed, "commit", "-qm", "diverge", home=tmp_path)
    git(seed, "push", "-q", "origin", "bot/diverged", home=tmp_path)
    theirs = remote_sha(env, "bot/diverged")
    write_script(env.script, {"commit_push": {"path": "a.txt", "content": "a",
                                              "to": "bot/diverged", "force": True}})
    finish(start(env))
    push = json.loads((env.logs / "push.log").read_text().splitlines()[0])
    assert push["rc"] != 0 and "non-fast-forward" in push["stderr"]
    assert remote_sha(env, "bot/diverged") == theirs


def test_pre_push_hook_refuses_a_ref_outside_the_prefix(env):
    write_script(env.script, {"commit_push": {"path": "b.txt", "content": "b", "to": "elsewhere/x"}})
    finish(start(env))
    push = json.loads((env.logs / "push.log").read_text().splitlines()[0])
    assert push["rc"] != 0 and "elsewhere/x" not in branches(env.base)


def hang_with_dirt(env, **step):
    write_script(env.script, {"write_file": {"path": "dirty.txt", "content": "unsaved\n"},
                              "hang": True, **step})
    p = start(env)
    assert wait_for(lambda: (env.dir / "work" / "dirty.txt").exists() and (env.dir / "run.pid").exists())
    return p


def salvaged_files(env):
    out = subprocess.run(["git", "--git-dir", str(bq.bare_path(env.base)), "diff", "--name-only",
                          "main", f"bot/salvage-{ID}"], capture_output=True, text=True).stdout
    return out.split()


def test_term_to_the_runner_salvages_exactly_the_dirty_file(env):
    p = hang_with_dirt(env)
    os.kill(p.pid, 15)                                   # our own child
    out, rep = finish(p)
    assert rep["salvaged"] is True and rep["branch"] == f"bot/salvage-{ID}"
    assert salvaged_files(env) == ["dirty.txt"]
    assert rep["pushed_sha"] == remote_sha(env, f"bot/salvage-{ID}")


@pytest.mark.parametrize("sig", [2, 1])
def test_int_and_hup_salvage_too(env, sig):
    p = hang_with_dirt(env)
    os.kill(p.pid, sig)
    out, rep = finish(p)
    assert rep["salvaged"] is True and salvaged_files(env) == ["dirty.txt"]


def test_killing_the_claude_group_salvages(env):
    """What the dispatcher's remote cancel does: TERM the group in run.pid."""
    p = hang_with_dirt(env)
    pg = pgid_of(env)
    assert pg != os.getpgrp() and pg != p.pid
    os.killpg(pg, 15)
    out, rep = finish(p)
    assert rep["salvaged"] is True and rep["exit"] != 0
    assert salvaged_files(env) == ["dirty.txt"]
    assert (env.dir / "exit").exists()


def test_salvage_survives_a_missing_git_identity(env, monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=scratch, check=True)
    (scratch / "f").write_text("x")
    subprocess.run(["git", "add", "f"], cwd=scratch, check=True)
    plain = subprocess.run(["git", "commit", "-qm", "x"], cwd=scratch, capture_output=True)
    assert plain.returncode != 0                         # without -c identity a commit would fail
    p = hang_with_dirt(env)
    os.kill(p.pid, 15)
    out, rep = finish(p)
    assert rep["salvaged"] is True and salvaged_files(env) == ["dirty.txt"]


def test_failed_salvage_push_keeps_the_worktree(env):
    hook = bq.bare_path(env.base) / "hooks" / "pre-receive"
    hook.write_text('#!/bin/bash\nwhile read o n r; do case "$r" in */salvage-*) exit 1;; esac; done\n')
    hook.chmod(0o755)
    p = hang_with_dirt(env)
    os.kill(p.pid, 15)
    out, rep = finish(p)
    assert rep["salvaged"] is False and rep["pushed_sha"] == ""
    assert "salvage push failed" in out
    assert (env.dir / "work" / "dirty.txt").read_text() == "unsaved\n"
    assert f"bot/salvage-{ID}" not in branches(env.base)


def test_interrupted_with_nothing_to_save_does_not_salvage(env):
    write_script(env.script, {"hang": True})
    p = start(env)
    assert wait_for(lambda: (env.dir / "run.pid").exists() and (env.logs / "prompts.jsonl").exists())
    time.sleep(0.3)
    os.kill(p.pid, 15)
    out, rep = finish(p)
    assert rep["salvaged"] is False and not [b for b in branches(env.base) if "salvage" in b]


def test_a_nonzero_exit_with_commits_salvages(env):
    write_script(env.script, {"write_file": {"path": "half.txt", "content": "h"}, "exit": 3})
    out, rep = finish(start(env))
    assert rep["exit"] == 3 and rep["salvaged"] is True
    assert salvaged_files(env) == ["half.txt"]


def test_timeout_ends_before_the_dispatchers_bound(env):
    write_script(env.script, {"write_file": {"path": "t.txt", "content": "t"}, "hang": True})
    out, rep = finish(start(env, timeout=61), timeout=30)      # timeout - 60 = 1s
    assert rep["exit"] == 124 and rep["salvaged"] is True


def test_review_kind_never_pushes(env):
    write_script(env.script, {"write_file": {"path": "r.txt", "content": "r"}, "exit": 2,
                              "text": "Verdict: APPROVE"})
    out, rep = finish(start(env, kind="review"))
    assert rep["salvaged"] is False and branches(env.base) == ["main"]


@pytest.mark.parametrize("bad", [dict(branch="-x"), dict(prefix="a b/"), dict(repo="a/..")])
def test_bad_arguments_are_refused(env, bad):
    out, rep = finish(start(env, **bad))
    assert rep["exit"] == 2


def test_missing_brief_is_refused(env):
    (env.dir / "brief.md").unlink()
    out, rep = finish(start(env))
    assert rep["exit"] == 2 and "missing" in out
