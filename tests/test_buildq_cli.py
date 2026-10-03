"""bin/buildq: each subcommand against a temp DB (spec 3.3)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import buildq_helpers as bq  # noqa: E402
from buildq_helpers import ROOT, make_workspace  # noqa: E402

import buildq  # noqa: E402

CLI = ROOT / "bin" / "buildq"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = make_workspace(tmp_path)
    bq.isolate(monkeypatch, tmp_path, w)
    c = buildq.connect(w / "data" / "build-queue.db")
    buildq.init_schema(c)
    c.close()
    (w / "config" / "build-queue.yaml").write_text(
        "enabled: true\nhosts:\n  local: {kind: local, min_free_ram_mb: 0}\n"
        "  far: {kind: ssh, target: box, probe: [probe]}\n")
    return w


def cli(ws, *args, check=None):
    p = subprocess.run([sys.executable, str(CLI), "--workspace", str(ws), *args],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check is not None:
        assert p.returncode == check, (p.stdout, p.stderr)
    return p


def enqueue(ws, *extra, text="do the thing", check=None):
    return cli(ws, "enqueue", "--kind", "build", "--repo", "owner/name", "--target-branch", "main",
               "--text", text, "--requester", "prim", *extra, check=check)


def test_enqueue_then_list_show(ws):
    p = enqueue(ws, "--priority", "2", "--host", "far", check=0)
    qid = p.stdout.split()[-1]
    assert qid.startswith("bq-")
    out = cli(ws, "list", check=0).stdout
    assert qid in out and "queued" in out
    show = cli(ws, "show", qid, check=0).stdout
    assert qid in show and "queued" in show
    j = json.loads(cli(ws, "--json", "show", qid, check=0).stdout)
    assert j["row"]["priority"] == 2 and j["row"]["host"] == "far"
    assert j["row"]["requester"] == "prim" and "repo: owner/name" in j["row"]["brief"]
    assert j["events"][0]["event"] == "queued"


def test_enqueue_from_a_brief_file_keeps_its_frontmatter(ws, tmp_path):
    f = tmp_path / "b.md"
    f.write_text(bq.brief(body="from file", branch="rel"))
    p = cli(ws, "enqueue", "--kind", "build", "--brief-file", str(f), check=0)
    j = json.loads(cli(ws, "list", "--json", check=0).stdout)
    assert j[0]["target_branch"] == "rel" and j[0]["repo"] == "owner/name"


def test_list_json_is_valid_and_filters(ws):
    a = enqueue(ws, check=0).stdout.split()[-1]
    b = enqueue(ws, text="two", check=0).stdout.split()[-1]
    cli(ws, "cancel", a, check=0)
    allrows = json.loads(cli(ws, "list", "--json", check=0).stdout)
    assert {r["id"] for r in allrows} == {a, b}
    q = json.loads(cli(ws, "list", "--status", "queued", "--json", check=0).stdout)
    assert [r["id"] for r in q] == [b]


def test_enqueue_refusals(ws):
    p = cli(ws, "enqueue", "--kind", "build", "--repo", "owner/name", "--text", "x")
    assert p.returncode != 0 and "target_branch" in p.stderr
    p = cli(ws, "enqueue", "--kind", "build", "--repo", "bad repo", "--target-branch", "m", "--text", "x")
    assert p.returncode != 0
    p = enqueue(ws, "--host", "ghost")
    assert p.returncode != 0 and "unknown host" in p.stderr
    p = cli(ws, "enqueue", "--kind", "build", "--repo", "owner/name", "--target-branch", "m")
    assert p.returncode != 0


def test_cancel_and_requeue(ws):
    qid = enqueue(ws, check=0).stdout.split()[-1]
    assert "cancelled" in cli(ws, "cancel", qid, check=0).stdout
    assert "nothing to cancel" in cli(ws, "cancel", qid, check=0).stdout
    new = cli(ws, "requeue", qid, check=0).stdout.split()[-1]
    assert new != qid and new.startswith("bq-")
    j = json.loads(cli(ws, "--json", "requeue", qid, check=0).stdout)
    assert j["requeued_from"] == qid


def test_unknown_id_exits_nonzero(ws):
    for sub in ("cancel", "show", "requeue"):
        p = cli(ws, sub, "bq-doesnotexist")
        assert p.returncode != 0 and "no such row" in p.stderr, sub


def test_hosts_shows_config_slots_and_probe(ws, monkeypatch):
    monkeypatch.setenv("KARAKOS_FAKE_PROBE_RAM", "100")
    j = json.loads(cli(ws, "hosts", "--json", check=0).stdout)
    far = next(h for h in j if h["host"] == "far")
    assert far["kind"] == "ssh" and far["free_slots"] == 1 and far["probe"]["free_ram_mb"] == 100
    assert far["admit"] is False and "free_ram_mb" in far["reason"]
    text = cli(ws, "hosts", check=0).stdout
    assert "far" in text and "busy" in text and "local" in text


def test_ingest_runs_one_pass(ws):
    (ws / "inbox" / "builder" / "x.md").write_text(bq.brief(body="ingest me"))
    j = json.loads(cli(ws, "--json", "ingest", check=0).stdout)
    assert len(j["queued"]) == 1
    assert (ws / "inbox" / "builder" / "queued" / "x.md").exists()


def test_refuses_when_disabled_without_force(ws):
    (ws / "config" / "build-queue.yaml").write_text("enabled: false\n")
    p = cli(ws, "list")
    assert p.returncode != 0 and "disabled" in p.stderr
    assert cli(ws, "list", "--force", check=0).returncode == 0
    assert cli(ws, "--force", "list", check=0).returncode == 0
    p = cli(ws, "enqueue", "--kind", "build", "--text", "x")
    assert p.returncode != 0 and "disabled" in p.stderr


def test_refuses_on_an_invalid_config_and_a_missing_db(ws):
    (ws / "config" / "build-queue.yaml").write_text("enabled: true\ndefault_host: ghost\n")
    assert "invalid config" in cli(ws, "list").stderr
    (ws / "config" / "build-queue.yaml").write_text("enabled: true\n")
    (ws / "data" / "build-queue.db").unlink()
    p = cli(ws, "list")
    assert p.returncode != 0 and "karakos migrate" in p.stderr
