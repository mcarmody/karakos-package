"""lib/monitor_jobs/memory_consolidate: job fields, heartbeat on success and failure."""
import json
import sys
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "lib"))

from lib.graph.store import open_graph  # noqa: E402
from lib.monitor_jobs import memory_consolidate as job  # noqa: E402
import job_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    monkeypatch.setenv("KARAKOS_SEMANTIC_RECALL", "0")


def health(ws):
    return json.loads((ws / "data" / "health" / "memory-consolidate.json").read_text())


def test_job_has_registry_fields():
    assert job.JOB["name"] == "memory-consolidate"
    assert job.JOB["schedule"] == "0 3 * * *"
    assert job.JOB["run"] is job.run_job
    assert job.JOB["timeout_s"] == 1800
    assert job.JOB["heartbeat"] == "memory-consolidate"


def test_schedule_is_a_valid_five_field_cron():
    fields = job.JOB["schedule"].split()
    assert len(fields) == 5
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for f, (lo, hi) in zip(fields, bounds):
        assert f == "*" or lo <= int(f) <= hi


def test_registered_in_the_job_table():
    j = {x.name: x for x in job_registry.BUILTIN}["memory-consolidate"]
    assert j.schedule == job_registry.Daily("03:00") and j.health_file == "memory-consolidate.json"
    assert job_registry.resolve_callable(j.run).__name__ == "run_scheduled"  # lib/ is on the scheduler's path


def test_success_writes_heartbeat(tmp_path):
    open_graph(tmp_path / "data", create=True)
    stats = job.run_job({"workspace": tmp_path, "score_fn": lambda s: 5.0})
    assert stats["errors"] == {} and "finished" in stats
    h = health(tmp_path)
    assert h["status"] == "healthy" and "timestamp" in h
    assert h["stats"]["episodes"]["created"] == 0


def test_failure_writes_heartbeat_with_error(tmp_path):
    with pytest.raises(Exception):  # no graph.db: boot never creates it
        job.run_job({"workspace": tmp_path})
    h = health(tmp_path)
    assert h["status"] == "error" and "graph" in h["stats"]["error"]


def test_pass_failure_is_reported_in_heartbeat(tmp_path, monkeypatch):
    from lib.graph import consolidate
    open_graph(tmp_path / "data", create=True)

    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(consolidate, "PASSES", (("decay", boom),))
    job.run_job({"workspace": tmp_path})
    h = health(tmp_path)
    assert h["status"] == "error" and "nope" in h["stats"]["error"]


def test_dry_run_writes_no_heartbeat(tmp_path):
    open_graph(tmp_path / "data", create=True)
    job.run_job({"workspace": tmp_path, "dry_run": True})
    assert not (tmp_path / "data" / "health" / "memory-consolidate.json").exists()


def test_cli_dry_run_prints_counts(tmp_path, capsys):
    import importlib.util
    open_graph(tmp_path / "data", create=True)
    spec = importlib.util.spec_from_file_location("gc", PACKAGE_ROOT / "bin" / "graph-consolidate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert m.main(["--dry-run", "--workspace", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and "dedup" in out and "episodes" in out
