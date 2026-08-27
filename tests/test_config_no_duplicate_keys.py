"""
Tests for issue #153 — duplicate keys in shipped JSON config.

config/claude-settings.json defined "PreToolUse" twice and "Stop" twice.
JSON parsers keep the *last* occurrence, so the first block of each pair was
silently discarded. It was harmless only because the duplicates happened to
be identical; the moment someone edits the first copy, their change has no
effect and nothing says why.

json.load() cannot see this — by the time you have a dict the earlier keys
are gone. object_pairs_hook is the only place the raw key sequence is
visible, so that is what this guard walks, at every nesting level, over
every JSON file the package ships under config/ plus the root .mcp.json.
"""

import json
from pathlib import Path

import pytest

from conftest import PACKAGE_ROOT

SHIPPED_JSON = sorted((PACKAGE_ROOT / "config").glob("*.json")) + [
    PACKAGE_ROOT / ".mcp.json"
]


def find_duplicate_keys(text):
    """Return [(json_path, key, count)] for every object in `text` that
    repeats a key. Empty list means the document is unambiguous."""
    found = []

    def walk(node, path):
        if isinstance(node, list) and node and all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in node
        ):
            # An object, preserved as its raw (key, value) pairs.
            counts = {}
            for key, _ in node:
                counts[key] = counts.get(key, 0) + 1
            for key, count in counts.items():
                if count > 1:
                    found.append((path or "<root>", key, count))
            for key, value in node:
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(json.loads(text, object_pairs_hook=lambda pairs: pairs), "")
    return found


def test_find_duplicate_keys_detects_a_planted_duplicate():
    """The guard has to be able to fail, including when nested."""
    assert find_duplicate_keys('{"a": 1, "a": 2}') == [("<root>", "a", 2)]
    assert find_duplicate_keys('{"h": {"Stop": [], "Stop": []}}') == [
        (".h", "Stop", 2)
    ]
    assert find_duplicate_keys('{"l": [{"k": 1, "k": 2}]}') == [(".l[0]", "k", 2)]
    assert find_duplicate_keys('{"a": 1, "b": {"a": 2}}') == []


@pytest.mark.parametrize("path", SHIPPED_JSON, ids=lambda p: p.name)
def test_shipped_json_has_no_duplicate_keys(path):
    assert path.exists(), f"{path} is listed as shipped config but does not exist"
    duplicates = find_duplicate_keys(path.read_text())
    assert not duplicates, (
        f"{path.relative_to(PACKAGE_ROOT)} repeats keys; a JSON parser keeps only "
        f"the last of each, silently discarding the rest: "
        + ", ".join(f"{key!r} x{count} at {where}" for where, key, count in duplicates)
    )


def test_claude_settings_declares_each_hook_event_once():
    """Named explicitly because this is the file #153 was filed against, and
    because the parametrized sweep above would keep passing if the file were
    ever dropped from config/."""
    path = PACKAGE_ROOT / "config" / "claude-settings.json"
    assert find_duplicate_keys(path.read_text()) == []

    hooks = json.loads(path.read_text())["hooks"]
    commands = [
        Path(hook["command"]).name
        for entries in hooks.values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    # Every hook script the package ships is wired exactly once.
    shipped = sorted(
        p.name
        for p in (PACKAGE_ROOT / "system" / "hooks").iterdir()
        if p.is_file() and p.suffix in {".py", ".sh"}
    )
    assert sorted(commands) == shipped, (
        f"wired hooks {sorted(commands)} != shipped hook scripts {shipped}"
    )
