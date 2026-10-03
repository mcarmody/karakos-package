#!/usr/bin/env python3
"""
check-secrets.py — secrets pre-commit check.

Usage: check-secrets.py --staged

(a) Forbidden paths: .env* (except .env.template), *.pem, *.key, id_*, and any
    `secrets/` path component. Always enforced.
(b) Content of staged changes: `gitleaks protect --staged` when installed,
    otherwise the regex set below. Only lines being added are scanned.

False positives: lines in config/secrets-allow.txt (exact line text), paths
under tests/fixtures/secrets-samples/, and lines containing `example`,
`placeholder` or `<token>` are skipped.

KARAKOS_SECRET_SCAN=off skips (b) only. KARAKOS_SECRET_SCAN=regex forces the
built-in regexes even when gitleaks is installed.
Repo root: $WORKSPACE_ROOT, else the current directory.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PATTERNS = [
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai-key", r"\bsk-(proj-)?[A-Za-z0-9]{32,}\b"),
    ("slack-token", r"\bxox[bap]-[A-Za-z0-9-]{10,}\b"),
    ("discord-bot-token", r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b"),
    ("auth-header", r"Authorization:\s*(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
COMPILED = [(n, re.compile(p)) for n, p in PATTERNS]
SAMPLES_DIR = "tests/fixtures/secrets-samples/"
SKIP_WORDS = ("example", "placeholder", "<token>")


def forbidden_path(path: str) -> str | None:
    parts = path.split("/")
    base = parts[-1]
    if base.startswith(".env") and base != ".env.template":
        return "environment file"
    if fnmatch.fnmatch(base, "*.pem") or fnmatch.fnmatch(base, "*.key"):
        return "key file"
    if base.startswith("id_"):
        return "ssh identity file"
    if "secrets" in parts[:-1]:
        return "secrets/ directory"
    return None


def scan_line(line: str, allow: set[str]) -> str | None:
    if line in allow:
        return None
    low = line.lower()
    if any(w in low for w in SKIP_WORDS):
        return None
    for name, rx in COMPILED:
        if rx.search(line):
            return name
    return None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def staged_files(root: Path) -> list[str]:
    r = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in r.stdout.split("\0") if p]


def added_lines(root: Path, path: str) -> list[str]:
    r = _git(root, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--", path)
    return [l[1:] for l in r.stdout.splitlines() if l.startswith("+") and not l.startswith("+++")]


def load_allow(root: Path) -> set[str]:
    p = root / "config" / "secrets-allow.txt"
    try:
        return {l.rstrip("\r\n") for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")}
    except OSError:
        return set()


def gitleaks_scan(root: Path) -> tuple[bool, str] | None:
    """(clean, output) if gitleaks gave a definite answer, None to fall back."""
    exe = shutil.which("gitleaks")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "protect", "--staged", "--no-banner", "--redact"],
                           cwd=str(root), capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return True, out
    if "leaks found" in out.lower():
        return False, out
    return None


def main(argv: list[str]) -> int:
    if "--staged" not in argv:
        print(__doc__)
        return 2
    root = Path(os.environ.get("WORKSPACE_ROOT") or os.getcwd())
    files = staged_files(root)
    problems: list[str] = []

    for f in files:
        why = forbidden_path(f)
        if why:
            problems.append(f"  {f}: {why} must not be committed")

    mode = os.environ.get("KARAKOS_SECRET_SCAN", "").lower()
    if mode != "off":
        result = None if mode == "regex" else gitleaks_scan(root)
        if result is not None:
            if not result[0]:
                problems.append("  gitleaks reported leaks:\n" + result[1])
        else:
            allow = load_allow(root)
            for f in files:
                if f.startswith(SAMPLES_DIR):
                    continue
                for line in added_lines(root, f):
                    name = scan_line(line, allow)
                    if name:
                        problems.append(f"  {f}: possible secret ({name})")
                        break

    if problems:
        print("check-secrets: commit blocked", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print("Unstage the file, or for a false positive add the exact line to "
              "config/secrets-allow.txt. KARAKOS_SECRET_SCAN=off skips content "
              "scanning for one commit.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
