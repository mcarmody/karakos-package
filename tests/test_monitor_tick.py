import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import monitor_tick  # noqa: E402
import findings  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def ws(tmp_path):
    h = tmp_path / "data" / "health"
    h.mkdir(parents=True)
    (h / "scheduler-jobs.json").write_text(json.dumps(
        {"started_at": (NOW - timedelta(hours=5)).isoformat(),
         "jobs": [{"name": "ghost"}]}))
    return tmp_path


def test_tick_writes_findings_and_summary_and_posts_once(tmp_path):
    ws(tmp_path)
    sent = []
    monitor_tick.tick(tmp_path, NOW, send=lambda p: sent.append(p) or True)
    data = json.loads((tmp_path / "data/health/findings.json").read_text())
    keys = {f["key"] for f in data["findings"]}
    assert "job-not-scheduled:wedge-check" in keys and "job-unknown:ghost" in keys
    lines = (tmp_path / "data/health/summary.md").read_text().splitlines()
    assert 0 < len(lines) <= 40
    assert sent
    n = len(sent)
    monitor_tick.tick(tmp_path, NOW + timedelta(seconds=60), send=lambda p: sent.append(p) or True)
    assert len(sent) == n   # same warns do not re-post


def test_failed_send_retries_next_tick(tmp_path):
    ws(tmp_path)
    monitor_tick.tick(tmp_path, NOW, send=lambda p: False)
    sent = []
    monitor_tick.tick(tmp_path, NOW + timedelta(seconds=60), send=lambda p: sent.append(p) or True)
    assert sent


def test_since_is_stable(tmp_path):
    ws(tmp_path)
    monitor_tick.tick(tmp_path, NOW, send=lambda p: True)
    monitor_tick.tick(tmp_path, NOW + timedelta(seconds=60), send=lambda p: True)
    fs = findings.read(tmp_path)
    assert fs and all(f.since == NOW.isoformat() for f in fs)


def test_stage_exception_is_a_finding_and_others_run(tmp_path, monkeypatch):
    ws(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("bad stage")

    monkeypatch.setattr(monitor_tick.drift, "drift_report", boom)
    monkeypatch.setattr(monitor_tick, "EXTRA_STAGES", [
        ("extra", lambda w, n, c: [findings.make("x", "y", "warn", "from extra")])])
    got = monitor_tick.tick(tmp_path, NOW, send=lambda p: True)
    keys = {f.key for f in got}
    assert "monitor-stage-failed:drift" in keys and "x:y" in keys


def test_invalid_monitor_config_finding(tmp_path):
    ws(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "monitor.yaml").write_text("alerts: {bogus: 1}\n")
    got = monitor_tick.tick(tmp_path, NOW, send=lambda p: True)
    assert any(f.kind == "monitor-config-invalid" for f in got)


def test_scheduler_wrapper_records_heartbeat_and_catches_exceptions(tmp_workspace, monkeypatch):
    import heartbeats
    import job_registry
    from conftest import import_script
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
    sched = import_script("scheduler")
    job = job_registry.Job("t", "job", job_registry.Every(60), "x:y", 100)
    sched._wrap(job, lambda: None)()
    assert heartbeats.read(tmp_workspace, "t")["ok"] is True

    def boom():
        raise RuntimeError("kaput")

    sched._wrap(job, boom)()
    rec = heartbeats.read(tmp_workspace, "t")
    assert rec["ok"] is False and "kaput" in rec["detail"]
    assert rec["previous_timestamp"]
