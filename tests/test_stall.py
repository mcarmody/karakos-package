"""Stall diagnosis: every cause from a constructed beacon plus a procinfo stub."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import procinfo  # noqa: E402
import stall  # noqa: E402

NOW = 1_800_000_000.0
CFG = {"stall_s": 120, "tool_stall_s": 900}


class Proc:
    def __init__(self, alive=True, state="S", kids=()):
        self._alive, self._state, self._kids = alive, state, list(kids)

    def alive(self, pid):
        return self._alive

    def proc_state(self, pid):
        return self._state if self._alive else None

    def children(self, pid):
        return self._kids


def beacon(**kw):
    b = {"agent": "jarvis-2", "shard": "jarvis-2", "state": "PROCESSING", "proc_pid": 4000,
         "silent_for": 300.0, "last_event_type": "assistant", "phase": "streaming"}
    b.update(kw)
    return b


def dx(b, proc=None, beacons=None, cfg=CFG):
    return stall.diagnose(b, {"proc": proc or Proc(), "now": NOW, "beacons": beacons or {}}, cfg)


def test_process_dead():
    d = dx(beacon(), Proc(alive=False))
    assert (d.cause, d.severity) == ("process_dead", "critical")


def test_process_stopped():
    d = dx(beacon(), Proc(state="T"))
    assert (d.cause, d.severity) == ("process_stopped", "critical")


def test_paused_by_gate_is_info():
    d = dx(beacon(paused={"reason": "5h window", "until": "14:00"}))
    assert (d.cause, d.severity) == ("paused_by_gate", "info")
    assert "5h window" in d.why and "14:00" in d.why


def test_held_by_wall_only_while_in_future():
    assert dx(beacon(held_until=NOW + 600)).cause == "held_by_wall"
    assert dx(beacon(held_until=NOW - 5)).cause == "model_silent"


def test_waiting_on_user():
    d = dx(beacon(ask_pending=True))
    assert (d.cause, d.severity) == ("waiting_on_user", "info")


def test_hive_call_blocked_with_chain_hop():
    other = beacon(agent="b", shard="b", silent_for=500.0, held_until=NOW + 60)
    d = dx(beacon(blocked_on={"call_id": "c1", "callee": "b"}, silent_for=100.0),
           beacons={"b": other})
    assert d.cause == "blocked_in_hive_call" and d.severity == "info"
    assert "b" in d.why and "usage wall" in d.why
    assert dx(beacon(blocked_on={"call_id": "c1", "callee": "b"}, silent_for=1000.0)).severity == "warn"


def test_tool_running_under_and_over_threshold():
    kid = {"pid": 4123, "argv": "npm test", "age_s": 348.0}
    d = dx(beacon(), Proc(kids=[kid]))
    assert (d.cause, d.severity) == ("tool_running", "info")
    assert '"npm test"' in d.why and "5.8 min" in d.why and "4123" in d.why
    kid["age_s"] = 1000.0
    assert dx(beacon(), Proc(kids=[kid])).severity == "warn"


def test_token_shaped_argument_is_redacted():
    assert procinfo.redact_arg("--api-key=sk-abcdefghijklmnopqrstuvwxyz1234") == "--api-key=***"
    assert procinfo.redact_arg("sk-abcdefghijklmnopqrstuvwxyz1234") == "***"
    assert procinfo.redact_arg("--port=3000") == "--port=3000"
    assert procinfo.redact_arg("test") == "test"


def test_model_silent_warn_then_critical():
    assert dx(beacon(silent_for=300.0)).severity == "warn"
    d = dx(beacon(silent_for=601.0))
    assert (d.cause, d.severity) == ("model_silent", "critical")


def test_unknown_when_not_past_threshold():
    d = dx(beacon(silent_for=10.0))
    assert (d.cause, d.severity) == ("unknown", "warn")


def write_beacon(ws, shard, state, age_s, **kw):
    d = ws / "data" / "health" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    last = datetime.fromtimestamp(NOW - age_s, timezone.utc)
    (d / f"{shard}.json").write_text(json.dumps(
        {"agent": shard, "state": state, "last_activity": last.isoformat(), **kw}))


def test_stage_makes_finding_per_active_silent_shard(tmp_path):
    write_beacon(tmp_path, "s1", "PROCESSING", 400)
    write_beacon(tmp_path, "s2", "IDLE", 9999)
    write_beacon(tmp_path, "s3", "PROCESSING", 10)
    now = datetime.fromtimestamp(NOW, timezone.utc)
    fs = stall.stall_stage(tmp_path, now, {"thresholds": CFG}, proc=Proc())
    assert [f.key for f in fs] == ["stall:s1"]
    assert "Why:" in fs[0].why and fs[0].severity == "warn"


def test_info_diagnosis_does_not_page(tmp_path):
    import alerts
    write_beacon(tmp_path, "s1", "PROCESSING", 400, paused={"reason": "x", "until": "1"})
    now = datetime.fromtimestamp(NOW, timezone.utc)
    fs = stall.stall_stage(tmp_path, now, {"thresholds": CFG}, proc=Proc())
    import monitor_config
    posts, _ = alerts.plan(fs, {}, monitor_config.DEFAULTS, now)
    assert fs[0].severity == "info" and posts == []
