#!/usr/bin/env python3
"""
bash-safety-rails.py — PreToolUse (Bash) safety rails.

Denies commands that have destroyed data or killed unrelated processes:

  * pkill -f / --full            (matches full command lines, kills bystanders)
  * ORM data-loss flags          (prisma --accept-data-loss / --force-reset)
  * rm -r on /, $HOME, $WORKSPACE_ROOT, $WORKSPACE_ROOT/.git, or any ancestor
    of the workspace; a target that cannot be resolved (unset variable, glob,
    command substitution) is denied too
  * git push --force (or a +refspec) to main/master

Matches in command position only (see lib/bashcmd.py): text inside quotes,
echo arguments, rg patterns and heredoc data never triggers a rail. Every deny
appends a JSONL line to $WORKSPACE_ROOT/logs/blocked-bash.jsonl.

Fail-safe: any parse error emits nothing and the command proceeds.
Wired by bin/hooks-sync.py into config/claude-settings.json.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import bashcmd  # noqa: E402
import hookio  # noqa: E402

_PROTECTED_BRANCHES = {"main", "master"}


def _check_pkill(argv):
    if os.path.basename(argv[0]) != "pkill":
        return None
    for a in argv[1:]:
        a = str(a)
        if a == "--full" or re.fullmatch(r"-[A-Za-z0-9]*f[A-Za-z0-9]*", a):
            return ("pkill-f",
                    "pkill -f matches against full command lines and can kill unrelated "
                    "processes, including this agent's own. Use `pkill <exact-process-name>` "
                    "or find the PID with `pgrep` and `kill <pid>`.")
    return None


def _check_orm(argv):
    names = {os.path.basename(str(a)) for a in argv}
    if "prisma" not in names:
        return None
    for a in argv:
        if str(a) in ("--accept-data-loss", "--force-reset"):
            return ("orm-data-loss",
                    f"prisma {a} can drop data. Run `prisma migrate dev` (or `prisma db push` "
                    "without the flag) so the schema change is reviewed, and back up first.")
    return None


def _expand_targets(argv, env, home):
    targets = []
    recursive = False
    opts_done = False
    for a in argv[1:]:
        s = str(a)
        if not opts_done and s == "--":
            opts_done = True
            continue
        if not opts_done and s.startswith("--"):
            recursive |= s == "--recursive"
            continue
        if not opts_done and s.startswith("-") and len(s) > 1:
            recursive |= bool(re.search(r"[rR]", s))
            continue
        targets.append(a)
    return recursive, targets


def _check_rm(argv, env, home, cwd):
    if os.path.basename(argv[0]) != "rm":
        return None
    recursive, targets = _expand_targets(argv, env, home)
    if not recursive:
        return None
    ws = env.get("WORKSPACE_ROOT")
    ws_real = os.path.realpath(ws) if ws else None
    roots = {os.path.realpath("/")}
    if home:
        roots.add(os.path.realpath(home))
    if ws_real:
        roots.add(ws_real)
        roots.add(os.path.join(ws_real, ".git"))
    for t in targets:
        path = bashcmd.expand_word(t, env, home)
        if path is None:
            return ("rm-unresolvable",
                    "rm -r target cannot be resolved (unset variable, glob or command "
                    "substitution). Pass an explicit path, for example `rm -rf ./build`.")
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        real = os.path.realpath(path)
        if real in roots or (ws_real and (ws_real == real or ws_real.startswith(real.rstrip(os.sep) + os.sep))):
            return ("rm-rf-protected",
                    "rm -r on the filesystem root, the home directory, the workspace root, "
                    ".git, or a parent of the workspace is blocked. Remove a specific "
                    "subdirectory instead, for example `rm -rf ./build`.")
    return None


def _current_branch(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _check_git_push(argv, cwd):
    if os.path.basename(argv[0]) != "git":
        return None
    i = 1
    while i < len(argv):                       # skip git global options
        a = str(argv[i])
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            if a == "-C" and i + 1 < len(argv):
                cwd = os.path.join(cwd, str(argv[i + 1]))
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    if i >= len(argv) or str(argv[i]) != "push":
        return None
    args = [str(a) for a in argv[i + 1:]]
    force = False
    positional = []
    for a in args:
        if a.startswith("--"):
            force |= a.startswith("--force")
        elif a.startswith("-") and len(a) > 1:
            force |= "f" in a[1:]
        else:
            positional.append(a)
    refspecs = positional[1:]                  # first positional is the remote
    plus = any(r.startswith("+") for r in refspecs)
    if not (force or plus):
        return None

    def to_branch(ref):
        dst = ref.lstrip("+").split(":")[-1]
        return re.sub(r"^refs/heads/", "", dst)

    if refspecs:
        hit = any(to_branch(r) in _PROTECTED_BRANCHES for r in refspecs)
    else:
        hit = _current_branch(cwd) in _PROTECTED_BRANCHES
    if hit:
        return ("git-force-push-main",
                "Force-pushing to main/master is blocked. Push a feature branch and open a "
                "PR, or use `git revert` to undo a bad commit on main.")
    return None


def evaluate(command, env, cwd):
    """Return (label, reason) for the first rail the command trips, else None."""
    home = env.get("HOME") or None
    for argv in bashcmd.commands(command):
        for hit in (_check_pkill(argv), _check_orm(argv),
                    _check_rm(argv, env, home, cwd), _check_git_push(argv, cwd)):
            if hit:
                return hit
    return None


def main() -> None:
    payload = hookio.read_bash_payload()
    if payload is None:
        return
    command = payload["tool_input"]["command"]
    cwd = payload.get("cwd") or os.getcwd()
    try:
        hit = evaluate(command, dict(os.environ), cwd)
    except Exception:
        return
    if hit:
        hookio.deny(hit[0], hit[1], command)


if __name__ == "__main__":
    main()
