"""Friction sensor (#237 build A): stream scan, state, summary, job registration."""
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PACKAGE_ROOT, import_script
from lib import friction, job_registry


def use(i, name, **inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def res(i, text, err=True):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}]}}


def rl(status="allowed", util=0.1):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": util}}}}


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "logs" / "agent-streams").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    return tmp_path


def write(ws, lines, name="marvin_20261019-010101.jsonl", mode="w"):
    with open(ws / "logs" / "agent-streams" / name, mode) as f:
        for d in lines:
            f.write((d if isinstance(d, str) else json.dumps(d)) + "\n")


def events(ws):
    p = ws / "data" / "friction-log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_classifies_errors_exits_denials(ws):
    write(ws, [
        use("a", "Read", file_path="/x"), res("a", "File does not exist"),
        use("b", "Bash", command="false"), res("b", "Exit code 1\nboom"),
        use("c", "Bash", command="sleep 9"), res("c", "<tool_use_error>Blocked: sleep</tool_use_error>"),
        use("d", "Bash", command="ls"), res("d", "fine", err=False),
    ])
    assert friction.scan(ws) == {"scanned": 1, "events": 3}
    kinds = [(e["kind"], e["tool"], e["agent"]) for e in events(ws)]
    assert kinds == [("tool_error", "Read", "marvin"), ("bash_exit", "Bash", "marvin"),
                     ("denied", "Bash", "marvin")]
    assert "false" in events(ws)[1]["detail"]


def test_second_scan_reads_only_new_lines(ws):
    write(ws, [use("a", "Read"), res("a", "nope")])
    friction.scan(ws)
    assert friction.scan(ws)["events"] == 0
    write(ws, [use("b", "Read"), res("b", "nope again")], mode="a")
    assert friction.scan(ws)["events"] == 1
    assert len(events(ws)) == 2


def test_half_written_line_waits_for_the_next_scan(ws):
    p = ws / "logs" / "agent-streams" / "marvin_1.jsonl"
    full = json.dumps(use("a", "Read")) + "\n" + json.dumps(res("a", "nope"))
    p.write_text(full)  # no trailing newline: the result is incomplete
    friction.scan(ws)
    assert events(ws) == []
    p.write_text(full + "\n")
    friction.scan(ws)
    assert [e["kind"] for e in events(ws)] == ["tool_error"]


def test_result_in_a_later_chunk_keeps_its_tool_name(ws):
    write(ws, [use("a", "Grep")])
    friction.scan(ws)
    write(ws, [res("a", "bad regex")], mode="a")
    friction.scan(ws)
    assert events(ws)[0]["tool"] == "Grep"


def test_repeated_command_reported_once_at_threshold(ws):
    write(ws, [use(str(i), "Bash", command="ls  -la") for i in range(5)]
          + [use("x", "Bash", command="ls -la")])
    friction.scan(ws)
    rep = [e for e in events(ws) if e["kind"] == "repeat" and e["agent"] == "marvin"]
    assert len(rep) == 1 and "x3" in rep[0]["detail"]


def test_rate_limit_logged_on_change_only(ws):
    write(ws, [rl(), rl(util=0.95), rl(util=0.97), rl(), rl("rejected", 1.0)])
    friction.scan(ws)
    rows = [e for e in events(ws) if e["kind"] == "rate_limit"]
    assert [r["detail"].split()[0] for r in rows] == ["status=allowed", "status=rejected"]


def test_secrets_are_redacted_and_detail_clipped(ws):
    write(ws, [use("a", "Bash", command="x"),
               res("a", "Exit code 1\n" + "Authorization: Bearer abcdefghijklmnop1234567890 " + "z" * 500)])
    friction.scan(ws)
    d = events(ws)[0]["detail"]
    assert "abcdefghijklmnop1234567890" not in d and len(d) <= friction.DETAIL_MAX


def test_truncated_stream_is_rescanned_from_the_start(ws):
    write(ws, [use("a", "Read"), res("a", "first failure with a long text" * 3)])
    friction.scan(ws)
    write(ws, [use("b", "Read"), res("b", "x")])  # smaller file replaces it
    friction.scan(ws)
    assert len(events(ws)) == 2


def test_summary_and_cli(ws, capsys):
    write(ws, [use("a", "Read"), res("a", "nope"), use("b", "Read"), res("b", "nope")])
    cli = import_script("friction-sensor")
    assert cli.main(["--workspace", str(ws)]) == 0
    assert "2 new events" in capsys.readouterr().out
    text = (ws / "data" / "friction" / "weekly.md").read_text()
    assert "tool_error: 2" in text and "2x tool_error/Read: nope" in text
    assert cli.main(["--workspace", str(ws), "--summary"]) == 0
    assert "2 events" in capsys.readouterr().out


def test_missing_dirs_and_corrupt_state_are_tolerated(tmp_path):
    assert friction.scan(tmp_path) == {"scanned": 0, "events": 0}
    (tmp_path / "data" / "friction-sensor-state.json").write_text("{nope")
    assert friction.scan(tmp_path)["scanned"] == 0
    assert friction.summary_text([]).startswith("# Friction summary")


def test_registered_as_an_hourly_job():
    j = {x.name: x for x in job_registry.BUILTIN}["friction-sensor"]
    assert j.run == "monitor_jobs.friction_sensor:run_scheduled" and j.schedule.seconds == 3600
    sys.path.insert(0, str(PACKAGE_ROOT / "lib"))  # the scheduler runs with lib/ on the path
    try:
        assert job_registry.resolve_callable(j.run)
    finally:
        sys.path.remove(str(PACKAGE_ROOT / "lib"))


TS = "2026-01-01T00:00:00.000Z"


def test_transcript_glob_source(ws, tmp_path, monkeypatch):
    d = tmp_path / "projects" / "x-agents-helper"
    d.mkdir(parents=True)
    lines = [dict(use("1", "Bash", command="false"), timestamp=TS),
             dict(res("1", "Exit code 1"), timestamp=TS)]
    with open(d / "session.jsonl", "w") as f:
        for x in lines:
            f.write(json.dumps(x) + "\n")
    monkeypatch.setenv("KARAKOS_TRANSCRIPT_GLOBS", str(tmp_path / "projects" / "*"))
    assert friction.scan(ws) == {"scanned": 1, "events": 1}
    (e,) = events(ws)
    assert e["agent"] == "helper" and e["kind"] == "bash_exit"
    assert e["category"] == "command_error" and e["evidence"] == e["detail"]
    assert e["session"] == "helper" and e["ts"] == TS
    assert friction.scan(ws)["events"] == 0  # offset kept


def test_both_names_on_stream_rows(ws):
    write(ws, [use("a", "Bash", command="false"), res("a", "Exit code 1")])
    friction.scan(ws)
    (e,) = events(ws)
    assert e["category"] == "command_error" and e["evidence"] == e["detail"]
    assert e["session"] == e["agent"] == "marvin"


def test_read_log_accepts_workspace_rows_and_counts_malformed(ws):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"ts": now, "session": "amos", "category": "command_error", "tool": "Bash", "evidence": "x"}
    (ws / "data" / "friction-log.jsonl").write_text(json.dumps(row) + "\n{not json\n")
    stats = {}
    rows = friction.read_log(ws, 7, stats)
    assert rows[0]["kind"] == "bash_exit" and rows[0]["detail"] == "x"
    assert rows[0]["agent"] == "amos" and stats["skipped"] == 1
    assert friction.summary_text(rows).startswith("# Friction summary")


def test_redaction_on_detail(ws):
    write(ws, [use("1", "Bash", command="curl https://example.invalid/x"),
               res("1", "Exit code 1: TOKEN=hunter2 failed https://user:pw@host/x")])
    friction.scan(ws)
    (e,) = events(ws)
    assert "TOKEN=[redacted]" in e["detail"] and "https://[redacted]@host/x" in e["detail"]
    assert "hunter2" not in e["detail"] and "user:pw" not in e["detail"]
    assert e["evidence"] == e["detail"]


def test_cross_session_repetition(ws):
    for s in "abcde":
        write(ws, [use("1", "Bash", command="git status")], name=f"{s}_1.jsonl")
    friction.scan(ws)
    rep = [e for e in events(ws) if e["kind"] == "repeat"]
    assert len(rep) == 1
    e = rep[0]
    assert e["agent"] == "(cross-session)" and e["count"] == 5
    assert e["category"] == "repetition" and e["signature"] == "git status"
    write(ws, [use("1", "Bash", command="git status")], name="f_1.jsonl")
    friction.scan(ws)
    assert len([e for e in events(ws) if e["kind"] == "repeat"]) == 1  # step still 5
    for s in "ghij":
        write(ws, [use("1", "Bash", command="git status")], name=f"{s}_1.jsonl")
    friction.scan(ws)
    rep = [e for e in events(ws) if e["kind"] == "repeat"]
    assert len(rep) == 2 and rep[-1]["count"] == 10


def test_window_prunes_old(ws):
    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path = ws / "data" / "friction-sensor-state.json"
    state_path.write_text(json.dumps({"cmd_window": [[old, "x"]] * 5}))
    friction.scan(ws)
    assert not [e for e in events(ws) if e["kind"] == "repeat"]
    assert json.loads(state_path.read_text())["cmd_window"] == []


def test_note(ws, monkeypatch):
    monkeypatch.delenv("KARAKOS_AGENT", raising=False)
    friction.note(ws, "typecheck needs the right tsconfig KEY=abc", tag="tsc")
    e = events(ws)[-1]
    assert e["kind"] == "note" and e["category"] == "note" and e["signature"] == "tsc"
    assert e["detail"].endswith("KEY=[redacted]") and e["evidence"] == e["detail"]
    assert e["session"] == e["agent"] == "(hand-logged)"


def test_cli_note(ws):
    cli = import_script("friction-sensor")
    assert cli.main(["--workspace", str(ws), "--note", "hello"]) == 0
    rows = events(ws)
    assert len(rows) == 1 and rows[0]["kind"] == "note"
