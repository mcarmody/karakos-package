"""Crontab coverage, pause and drift checks for installs that keep cron.

A cron job proves it ran by touching `<heartbeat_dir>/<job>` (lib/heartbeats.py,
contract 07). These helpers read crontab text only; the caller decides which
host's crontab to read and keeps the job table.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Iterable

PAUSE_MARKER_RE = re.compile(r"\b(PAUSED|DISABLED)\b")
_TRAILING_TAG_RE = re.compile(r"#\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\([^)]*\))?\s*$")
_CRON_SCHEDULE_RE = re.compile(
    r"^(@(reboot|hourly|daily|weekly|monthly|yearly|annually)\b"
    r"|[0-9*,/-]+\s+[0-9*,/-]+\s+[0-9*,/-]+\s+[0-9*,/a-zA-Z-]+\s+[0-9*,/a-zA-Z-]+\s)"
)


def touch_re(heartbeat_dir: str | os.PathLike) -> re.Pattern:
    return re.compile(r"touch\s+" + re.escape(str(heartbeat_dir)) + r"/([A-Za-z0-9._-]+)")


def heartbeat_jobs_in_text(text: str, heartbeat_dir: str | os.PathLike) -> set[str]:
    """Names touched on live (non-comment) lines."""
    rx = touch_re(heartbeat_dir)
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        names.update(rx.findall(line))
    return names


def _job_line_names(body: str, heartbeat_dir: str | os.PathLike) -> set[str]:
    names = set(touch_re(heartbeat_dir).findall(body))
    if "#" in body:
        m = _TRAILING_TAG_RE.search(body)
        if m:
            names.add(m.group(1))
    return names


def paused_and_live_names(text: str, heartbeat_dir: str | os.PathLike) -> tuple[set[str], set[str]]:
    """Return (paused, live) heartbeat names found in one crontab text.

    A commented job line is paused when it carries a PAUSED/DISABLED marker on its
    own line or sits under a marked comment block (a blank line ends the block).
    """
    rx = touch_re(heartbeat_dir)
    paused: set[str] = set()
    live: set[str] = set()
    block_marked = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            block_marked = False
            continue
        if not line.startswith("#"):
            live |= _job_line_names(line, heartbeat_dir)
            continue
        body = line.lstrip("#").strip()
        marked_here = bool(PAUSE_MARKER_RE.search(body))
        if rx.search(body) or _CRON_SCHEDULE_RE.search(body):
            if marked_here or block_marked:
                paused |= _job_line_names(body, heartbeat_dir)
        block_marked = block_marked or marked_here
    return paused, live


def paused_jobs(texts: Iterable[str], jobs: Iterable[str], heartbeat_dir: str | os.PathLike) -> set[str]:
    paused: set[str] = set()
    live: set[str] = set()
    for text in texts:
        p, l = paused_and_live_names(text, heartbeat_dir)
        paused |= p
        live |= l
    return (paused - live) & set(jobs)


def read_crontab(ssh: list[str] | None = None, timeout: float = 10) -> str | None:
    """Return the crontab text, or None when it cannot be read (never "empty")."""
    if ssh is None:
        cmd = ["crontab", "-l"]
    else:
        cmd = list(ssh) + ["crontab -l"]
        timeout = max(timeout, 30)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def unmonitored(
    texts: Iterable[str],
    jobs: Iterable[str],
    heartbeat_dir: str | os.PathLike,
    deliberately_disabled: Iterable[str] = (),
) -> list[str]:
    touched: set[str] = set()
    for text in texts:
        touched |= heartbeat_jobs_in_text(text, heartbeat_dir)
    return sorted(touched - set(jobs) - set(deliberately_disabled))


def drift(live_text: str | None, known_good_text: str, heartbeat_dir: str | os.PathLike) -> dict:
    if live_text is None:
        return {"readable": False, "live_only": [], "known_good_only": []}
    live = heartbeat_jobs_in_text(live_text, heartbeat_dir)
    known = heartbeat_jobs_in_text(known_good_text, heartbeat_dir)
    return {
        "readable": True,
        "live_only": sorted(live - known),
        "known_good_only": sorted(known - live),
    }
