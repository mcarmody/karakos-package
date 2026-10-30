import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import cron_drift as cd  # noqa: E402

HB = "/srv/hb"


def test_live_names_skip_comments():
    text = """
*/5 * * * * run.sh && touch /srv/hb/a # a
# 0 * * * * old.sh && touch /srv/hb/b
"""
    assert cd.heartbeat_jobs_in_text(text, HB) == {"a"}


def test_paused_same_line_and_block_above():
    text = """
# DISABLED 2026-01-01 moved to the new host
# 1 2 * * * x.sh && touch /srv/hb/c # c
# */5 * * * * y.sh && touch /srv/hb/d # d PAUSED
*/5 * * * * e.sh && touch /srv/hb/e
"""
    assert cd.paused_and_live_names(text, HB) == ({"c", "d"}, {"e"})


def test_blank_line_ends_marked_block():
    text = """
# DISABLED 2026-01-01 moved to the new host

# 1 2 * * * x.sh && touch /srv/hb/c # c
"""
    paused, live = cd.paused_and_live_names(text, HB)
    assert "c" not in paused
    assert live == set()


def test_paused_jobs_live_elsewhere_wins():
    t1 = "# 0 * * * * y.sh && touch /srv/hb/d # d PAUSED\n"
    t2 = "0 * * * * d.sh && touch /srv/hb/d\n"
    assert cd.paused_jobs([t1], ["d"], HB) == {"d"}
    assert cd.paused_jobs([t1, t2], ["d"], HB) == set()


def test_unmonitored_and_disabled():
    text = """
0 * * * * a.sh && touch /srv/hb/a
0 * * * * b.sh && touch /srv/hb/b
0 * * * * z.sh && touch /srv/hb/z
"""
    assert cd.unmonitored([text], ["a"], HB, deliberately_disabled=["z"]) == ["b"]


def test_drift_unreadable_and_both_directions():
    kg = "0 * * * * a.sh && touch /srv/hb/a\n0 * * * * c.sh && touch /srv/hb/c\n"
    live = "0 * * * * a.sh && touch /srv/hb/a\n0 * * * * b.sh && touch /srv/hb/b\n"
    assert cd.drift(None, kg, HB)["readable"] is False
    result = cd.drift(live, kg, HB)
    assert result["readable"] is True
    assert result["live_only"] == ["b"]
    assert result["known_good_only"] == ["c"]


def test_read_crontab_handles_missing_binary(monkeypatch):
    def raise_oserror(*args, **kwargs):
        raise OSError("no crontab binary")

    monkeypatch.setattr(subprocess, "run", raise_oserror)
    assert cd.read_crontab() is None

    class Done:
        returncode = 0
        stdout = "x"

    seen = {}

    def ok(cmd, **kwargs):
        seen["cmd"] = cmd
        return Done()

    monkeypatch.setattr(subprocess, "run", ok)
    assert cd.read_crontab() == "x"
    assert seen["cmd"] == ["crontab", "-l"]
    assert cd.read_crontab(ssh=["ssh", "h"]) == "x"
    assert seen["cmd"] == ["ssh", "h", "crontab -l"]


def test_read_crontab_nonzero_exit_is_unreadable(monkeypatch):
    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: Failed())
    assert cd.read_crontab() is None
