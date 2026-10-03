import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import alerts  # noqa: E402
import monitor_config  # noqa: E402
from findings import make  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
CFG = monitor_config.load("/nonexistent")[0]


def F(kind, sev="warn", why="w"):
    return make(kind, "s", sev, why)


@pytest.fixture(autouse=True)
def no_owner(monkeypatch):
    monkeypatch.delenv("OWNER_DISCORD_ID", raising=False)


def test_warn_posts_once_then_resolves():
    posts, st = alerts.plan([F("a")], {}, CFG, NOW)
    assert len(posts) == 1
    posts2, st = alerts.plan([F("a")], st, CFG, NOW + timedelta(minutes=5))
    assert posts2 == []
    posts3, st = alerts.plan([], st, CFG, NOW + timedelta(minutes=10))
    assert len(posts3) == 1 and posts3[0]["text"].startswith("✅ resolved: a:s")
    assert st["keys"] == {}


def test_info_never_posts():
    assert alerts.plan([F("a", "info")], {}, CFG, NOW)[0] == []


def test_critical_repeats_then_stops():
    st = {}
    n = 0
    for i in range(10):
        posts, st = alerts.plan([F("a", "critical")], st, CFG, NOW + timedelta(seconds=1800 * i))
        n += len(posts)
    assert n == 1 + CFG["alerts"]["max_repeats"]


def test_critical_not_repeated_before_interval():
    _, st = alerts.plan([F("a", "critical")], {}, CFG, NOW)
    posts, _ = alerts.plan([F("a", "critical")], st, CFG, NOW + timedelta(seconds=60))
    assert posts == []


def test_rate_limit_collapses():
    fs = [make("k", str(i), "warn", "w") for i in range(20)]
    posts, st = alerts.plan(fs, {}, CFG, NOW)
    assert len(posts) == CFG["alerts"]["max_posts_per_10min"]
    assert "more findings, see data/health/summary.md" in posts[-1]["text"]
    assert len(st["keys"]) == 20


def test_long_post_cut():
    posts, _ = alerts.plan([F("a", why="x" * 5000)], {}, CFG, NOW)
    assert len(posts[0]["text"]) <= 1900


def test_mention(monkeypatch):
    posts, _ = alerts.plan([F("a", "critical")], {}, CFG, NOW)
    assert "<@" not in posts[0]["text"]
    monkeypatch.setenv("OWNER_DISCORD_ID", "0")
    assert "<@" not in alerts.plan([F("a", "critical")], {}, CFG, NOW)[0][0]["text"]
    monkeypatch.setenv("OWNER_DISCORD_ID", "123")
    assert "<@123>" in alerts.plan([F("a", "critical")], {}, CFG, NOW)[0][0]["text"]


def test_send_uses_discord_notify_never_poke(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "discord-notify.sh").write_text("#!/bin/sh\n")
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(alerts.subprocess, "run", fake)
    assert alerts.send({"text": "hi", "channel": "signals"}, CFG, tmp_path) is True
    assert seen["cmd"][0].endswith("discord-notify.sh") and seen["cmd"][1:] == ["signals", "hi"]
    assert "poke" not in " ".join(seen["cmd"]) and seen["kw"]["timeout"] == 30


@pytest.mark.parametrize("exc", [subprocess.CalledProcessError(1, "x", stderr=b"500"),
                                 subprocess.TimeoutExpired("x", 30), OSError("nope")])
def test_send_failures_logged(tmp_path, monkeypatch, exc):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "discord-notify.sh").write_text("")

    def fake(*a, **k):
        raise exc

    monkeypatch.setattr(alerts.subprocess, "run", fake)
    assert alerts.send({"text": "hi", "channel": "signals"}, CFG, tmp_path) is False
    assert (tmp_path / "logs" / "health-alerts.log").read_text()


def test_missing_script_false(tmp_path):
    assert alerts.send({"text": "hi", "channel": "s"}, CFG, tmp_path) is False


def test_failed_send_leaves_state_unadvanced():
    f = [F("a")]
    posts, new = alerts.plan(f, {}, CFG, NOW)
    st = alerts.commit({}, new, posts, [False])
    assert st["keys"] == {}
    posts2, _ = alerts.plan(f, st, CFG, NOW + timedelta(seconds=60))
    assert len(posts2) == 1   # retried
