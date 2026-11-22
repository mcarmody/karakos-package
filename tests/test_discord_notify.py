"""bin/discord-notify.sh: argv, --stdin and --file bodies, splitting, error text.

Curl is replaced by a recording fake on PATH, so nothing reaches Discord.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "discord-notify.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="needs jq and bash",
)

FAKE_CURL = """#!/usr/bin/env bash
out=""; data=""; url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -d) data="$2"; shift 2 ;;
    https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
printf '%s\\n' "$url" >> "$FAKE_CURL_URLS"
printf '%s\\n' "$data" >> "$FAKE_CURL_BODIES"
[[ -n "$out" ]] && printf '{"message":"fake"}' > "$out"
printf '%s' "${FAKE_CURL_CODE:-204}"
"""


def run(tmp_path, *args, stdin=None, code="204", channels=None):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    curl = fakebin / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if channels is not None:
        (workspace / "config").mkdir(exist_ok=True)
        (workspace / "config" / "channels.json").write_text(
            json.dumps({"channels": channels})
        )

    urls_file = tmp_path / "urls.txt"
    bodies_file = tmp_path / "bodies.txt"
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "WORKSPACE_ROOT": str(workspace),
        "DISCORD_NOTIFY_BOT_TOKEN": "test-token",
        "FAKE_CURL_URLS": str(urls_file),
        "FAKE_CURL_BODIES": str(bodies_file),
        "FAKE_CURL_CODE": code,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    contents = []
    if bodies_file.exists():
        contents = [
            json.loads(line)["content"]
            for line in bodies_file.read_text().splitlines()
            if line.strip()
        ]
    urls = []
    if urls_file.exists():
        urls = [line for line in urls_file.read_text().splitlines() if line.strip()]
    return proc, contents, urls


def test_argv_message_is_posted(tmp_path):
    proc, contents, urls = run(tmp_path, "general", "hello")
    assert proc.returncode == 0, proc.stderr
    assert contents == ["hello"]
    assert len(urls) == 1
    assert urls[0].endswith("/channels/general/messages")


def test_stdin_carries_prose_with_shell_syntax(tmp_path):
    body = 'Sam\'s (note) with `ticks` and "quotes"'
    proc, contents, _ = run(tmp_path, "general", "--stdin", stdin=body)
    assert proc.returncode == 0, proc.stderr
    assert contents == [body]


def test_file_body(tmp_path):
    body = 'Sam\'s (note) with `ticks` and "quotes"'
    path = tmp_path / "body.txt"
    path.write_text(body)
    proc, contents, _ = run(tmp_path, "general", "--file", str(path))
    assert proc.returncode == 0, proc.stderr
    assert contents == [body]


def test_file_missing_path_posts_nothing(tmp_path):
    proc, contents, urls = run(tmp_path, "general", "--file", str(tmp_path / "missing.txt"))
    assert proc.returncode != 0
    assert contents == []
    assert urls == []


def test_long_body_is_split_under_the_limit(tmp_path):
    body = "\n".join("line %03d " % i + "x" * 90 for i in range(60))
    proc, contents, _ = run(tmp_path, "general", body)
    assert proc.returncode == 0, proc.stderr
    assert len(contents) >= 3
    assert all(len(c) <= 1900 for c in contents)
    assert "\n".join(contents) == body


def test_single_overlong_line_is_cut(tmp_path):
    body = "y" * 4000
    proc, contents, _ = run(tmp_path, "general", body)
    assert proc.returncode == 0, proc.stderr
    assert all(len(c) <= 1900 for c in contents)
    assert "".join(contents) == body


def test_rejected_post_is_loud(tmp_path):
    proc, _, _ = run(tmp_path, "general", "hello", code="400")
    assert proc.returncode != 0
    assert "HTTP 400" in proc.stderr


def test_channel_name_with_a_quote_resolves(tmp_path):
    proc, _, urls = run(
        tmp_path, "it's", "hi", channels={"it's": {"id": "4242"}}
    )
    assert proc.returncode == 0, proc.stderr
    assert urls[0].endswith("/channels/4242/messages")


def test_no_message_is_a_usage_error(tmp_path):
    proc, _, _ = run(tmp_path, "general")
    assert proc.returncode == 1
    assert "Usage" in proc.stderr
