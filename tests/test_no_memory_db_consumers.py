"""After 4.2b only the migrator (lib/migrate) opens a 1.x memory.db.

Walks every tracked .py/.sh/.ts file outside tests/ and lib/migrate/ and fails
on a memory.db reference or a 1.x table statement.
"""
import subprocess

from conftest import PACKAGE_ROOT

NEEDLES = ("memory.db", "memory/memory.db", "FROM episodes", "FROM facts", "FROM patterns",
           "INTO episodes", "INTO facts")
SUFFIXES = (".py", ".sh", ".ts")
EXCLUDED_PREFIXES = ("tests/", "lib/migrate/", "docs/")
# bin/memory-maintenance.py is the nightly 1.x writer; 4.3 retires it and this
# entry with it. Nothing else may be listed here.
ALLOWLIST = {"bin/memory-maintenance.py"}
MUST_BE_SCANNED = ("bin/relay.py", "bin/oneshot.py", "bin/purge-data.py")


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=PACKAGE_ROOT, capture_output=True,
                         text=True, check=True).stdout.split("\n")
    return [f for f in out if f.endswith(SUFFIXES)]


def scanned():
    return [f for f in tracked() if not f.startswith(EXCLUDED_PREFIXES)]


def test_audited_files_are_scanned():
    files = set(scanned())
    for f in MUST_BE_SCANNED:
        assert f in files, f"{f} is not covered by the guard"
        assert f not in ALLOWLIST


def test_no_memory_db_consumers():
    hits = []
    for f in scanned():
        if f in ALLOWLIST:
            continue
        text = (PACKAGE_ROOT / f).read_text(errors="replace")
        for n in NEEDLES:
            if n in text:
                hits.append(f"{f}: {n}")
    assert not hits, "memory.db consumers outside the migrator:\n" + "\n".join(hits)
