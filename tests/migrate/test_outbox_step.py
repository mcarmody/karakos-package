"""Migrator step 60_outbox (spec 6.1): legacy dead-letter file into the outbox."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib.migrate import runner  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("KARAKOS_SKIP_STAMP_CHECK", raising=False)
    monkeypatch.delenv("KARAKOS_ENV", raising=False)


def make(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "agents.yaml").write_text("agents: {}\n")   # a recognisable 1.x install
    lines = [json.dumps({"ts": f"2026-09-0{i}T10:00:00+00:00", "agent": "amos", "channel_id": f"c{i}",
                         "reason": f"HTTP 40{i}", "attempts": i, "content": f"reply {i}"}) for i in (1, 2, 3)]
    lines.insert(2, "this line is corrupt")
    (data / "discord-dead-letter.jsonl").write_text("\n".join(lines) + "\n")
    return data, tmp_path / "config"


def dead_rows(data):
    con = sqlite3.connect(data / "outbox" / "outbox.db")
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM outbox ORDER BY created_at")]
    finally:
        con.close()


def step():
    return next(s for s in runner.load_steps() if s.name == "60_outbox")


def test_chain_order_places_outbox_after_memory_and_before_the_end():
    names = [s.name for s in runner.load_steps()]
    assert names == sorted(names) and names.index("40_memory") < names.index("60_outbox")
    assert names[-1] == "60_outbox"


def test_dry_run_reports_counts_and_changes_nothing(tmp_path):
    data, config = make(tmp_path)
    out = []
    assert runner.run(data, config, dry_run=True, out=out.append) == 0
    text = "\n".join(out)
    assert "60_outbox" in text and "import 3 dead-letter record(s)" in text and "1 malformed" in text
    assert (data / "discord-dead-letter.jsonl").exists() and not (data / "outbox").exists()
    assert not (data / "discord-dead-letter.jsonl.migrated").exists()


def test_apply_imports_valid_lines_renames_and_second_run_is_a_noop(tmp_path):
    data, config = make(tmp_path)
    out = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out.append, parity_queries=0) == 0, out
    rows = dead_rows(data)
    assert len(rows) == 3 and {r["status"] for r in rows} == {"dead"}
    assert [r["content"] for r in rows] == ["reply 1", "reply 2", "reply 3"]
    assert rows[0]["dead_reason"] == "legacy dead letter: HTTP 401" and rows[2]["attempts"] == 3
    assert rows[0]["agent"] == "amos" and rows[0]["channel_id"] == "c1" and rows[0]["content_sha"]
    assert not (data / "discord-dead-letter.jsonl").exists()
    assert (data / "discord-dead-letter.jsonl.migrated").exists()
    from lib.migrate import runner as r2
    ctx = r2.Context(data, config, None, None, None)
    assert step().detect(ctx) is False
    out2 = []
    assert runner.run(data, config, backup_root=tmp_path / "bk", out=out2.append) == 0
    assert any("nothing to do" in l for l in out2) and len(dead_rows(data)) == 3


def test_step_verify_and_idempotent_import(tmp_path):
    data, config = make(tmp_path)
    ctx = runner.Context(data, config, None, None, None)
    s = step()
    assert s.detect(ctx)
    s.apply(ctx)
    s.verify(ctx)
    # an interrupted run (insert done, rename lost) must not import twice
    (data / "discord-dead-letter.jsonl.migrated").rename(data / "discord-dead-letter.jsonl")
    ctx2 = runner.Context(data, config, None, None, None)
    s.apply(ctx2)
    s.verify(ctx2)
    assert len(dead_rows(data)) == 3


def test_empty_file_is_not_detected(tmp_path):
    data, config = make(tmp_path)
    (data / "discord-dead-letter.jsonl").write_text("")
    assert step().detect(runner.Context(data, config, None, None, None)) is False
