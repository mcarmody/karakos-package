"""API freeze for tests/harness.Harness (review L6).

Parallel builds (1.2, 1.5, phase 2) code against these signatures. Changing
one must fail here, not silently in someone else's branch.
"""

import inspect

from harness import Harness


def sig(name):
    return inspect.signature(getattr(Harness, name))


def params(name):
    return [(p.name, p.default) for p in sig(name).parameters.values()
            if p.name != "self"]


def prefix(name, original):
    """The original parameters and defaults, in order, as a prefix; later
    parameters are allowed if they have defaults (additive growth, 2.1)."""
    got = params(name)
    assert got[:len(original)] == original, got
    assert all(d is not EMPTY for _, d in got[len(original):]), got


EMPTY = inspect.Parameter.empty


def test_constructor():
    prefix("__init__", [("tmp_workspace", EMPTY), ("agents", ["a", "b"])])
    assert dict(params("__init__"))["shards"] is None
    assert dict(params("__init__"))["work_stealing"] is None  # 2.4
    assert dict(params("__init__"))["steering"] is None  # 2.5


def test_send():
    assert params("send") == [("agent", EMPTY), ("text", EMPTY), ("channel_id", "1")]
    assert inspect.iscoroutinefunction(Harness.send)


def test_wait_idle():
    assert params("wait_idle") == [("agent", EMPTY), ("timeout", 5)]
    assert inspect.iscoroutinefunction(Harness.wait_idle)


def test_shard_keyed_readers_are_sync():
    for name in ("sent_to", "argv", "queue_rows"):
        assert params(name) == [("shard", EMPTY)], name
        assert not inspect.iscoroutinefunction(getattr(Harness, name)), name
    prefix("cost_rows", [])
    assert dict(params("cost_rows"))["shard"] is None
    assert not inspect.iscoroutinefunction(Harness.cost_rows)


def test_steering_helpers_are_sync():
    # additive in 2.5
    for name in ("stdin_events", "results"):
        assert params(name) == [("shard", EMPTY)], name
        assert not inspect.iscoroutinefunction(getattr(Harness, name)), name
    assert params("row_status") == [("shard", EMPTY), ("id", EMPTY)]


def test_discord_recorder_attribute(tmp_workspace):
    assert Harness(tmp_workspace).discord == []


def test_poke(tmp_workspace):
    # additive in 2.7: machine rows, as bin/poke.sh posts them
    assert params("poke") == [("shard", EMPTY), ("source", EMPTY), ("text", EMPTY),
                              ("channel_id", "0")]
    assert inspect.iscoroutinefunction(Harness.poke)


# --- step 3.3: build queue stubs (additive) ---------------------------------------------

def test_build_queue_stubs_exist_and_fake_ssh_scrubs_the_environment(tmp_path):
    import os
    import subprocess
    from harness import FAKE_BIN_DIR
    for name in ("fake-ssh", "gh", "probe", "claude"):
        assert os.access(FAKE_BIN_DIR / name, os.X_OK), name
    env = {"PATH": os.environ["PATH"], "KARAKOS_FAKE_REMOTE_HOME": str(tmp_path),
           "AGENT_SERVER_TOKEN": "sekret", "FAKE_CLAUDE_X": "kept"}
    out = subprocess.run([str(FAKE_BIN_DIR / "fake-ssh"), "-o", "BatchMode=yes", "box",
                          'echo "$HOME|${AGENT_SERVER_TOKEN:-none}|$FAKE_CLAUDE_X"'],
                         capture_output=True, text=True, env=env).stdout
    assert out.strip() == f"{tmp_path}|none|kept"
    env["KARAKOS_FAKE_SSH_FAIL"] = "255"
    assert subprocess.run([str(FAKE_BIN_DIR / "fake-ssh"), "box", "true"], env=env).returncode == 255


def test_fake_claude_oneshot_runs_a_turn_and_never_commits_in_the_package_repo(tmp_path):
    import json
    import os
    import subprocess
    from harness import FAKE_BIN_DIR, PACKAGE_ROOT
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"default": {"text": "hi {{text}}"}}))
    env = dict(os.environ, FAKE_CLAUDE_SCRIPT=str(script), FAKE_CLAUDE_LOG_DIR=str(tmp_path))
    out = subprocess.run([str(FAKE_BIN_DIR / "claude"), "-p", "the prompt", "--model", "m"],
                         capture_output=True, text=True, env=env, cwd=tmp_path,
                         stdin=subprocess.DEVNULL).stdout
    result = [json.loads(l) for l in out.splitlines()][-1]
    assert result["type"] == "result" and result["result"] == "hi the prompt"
    script.write_text(json.dumps({"default": {"commit_push": {"path": "x.txt", "content": "x"}}}))
    p = subprocess.run([str(FAKE_BIN_DIR / "claude"), "-p", "go"], capture_output=True, text=True,
                       env=env, cwd=PACKAGE_ROOT, stdin=subprocess.DEVNULL)
    assert p.returncode == 3 and "refusing" in p.stderr
    assert not (PACKAGE_ROOT / "x.txt").exists()
