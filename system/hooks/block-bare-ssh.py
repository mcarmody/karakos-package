#!/usr/bin/env python3
"""
block-bare-ssh.py — PreToolUse (Bash) block on bare ssh/scp/rsync to listed hosts.

Generic by design: the host list comes from config/hooks.json
(`bare_ssh_hosts`, default empty, which makes this hook a no-op) and the
replacement command from `ssh_wrapper`. Typical use: a host that must be
reached through a wrapper that adds timeouts, keys or audit logging.

Command-position only (lib/bashcmd.py): `echo "ssh host"` is not an ssh call.
Fail-safe: any error emits nothing. Denies are logged to
$WORKSPACE_ROOT/logs/blocked-bash.jsonl.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import bashcmd  # noqa: E402
import hookio  # noqa: E402

_SSH_VALUED = set("pilobFJLRDcEeImOQSWwB")  # single-letter ssh options that take a value
_REMOTE = re.compile(r"^(?:[^@/:\s]+@)?(\[[^\]]+\]|[^@/:\s]+):")


def load_config(root):
    try:
        with open(os.path.join(root, "config", "hooks.json")) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _host(token):
    return token.rsplit("@", 1)[-1].strip("[]").lower()


def _ssh_target(args):
    skip = False
    for a in args:
        a = str(a)
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            if len(a) == 2 and a[1] in _SSH_VALUED:
                skip = True
            continue
        return _host(a)
    return None


def _remote_hosts(args):
    hosts = []
    for a in args:
        m = _REMOTE.match(str(a))
        if m:
            hosts.append(m.group(1).strip("[]").lower())
    return hosts


def _rsh(args):
    args = [str(a) for a in args]
    for i, a in enumerate(args):
        if a == "-e" and i + 1 < len(args):
            return args[i + 1]
        if a == "--rsh" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--rsh="):
            return a[6:]
        if a.startswith("-e") and len(a) > 2 and not a.startswith("--"):
            return a[2:]
    return None


def evaluate(command, cfg):
    hosts = {str(h).lower() for h in cfg.get("bare_ssh_hosts") or []}
    if not hosts:
        return None
    wrapper = cfg.get("ssh_wrapper") or ""
    for argv in bashcmd.commands(command):
        name = os.path.basename(argv[0])
        hit = None
        if name in ("ssh", "sftp", "mosh"):
            t = _ssh_target(argv[1:])
            hit = t if t in hosts else None
        elif name == "scp":
            hit = next((h for h in _remote_hosts(argv[1:]) if h in hosts), None)
        elif name == "rsync":
            rsh = _rsh(argv[1:])
            if rsh is None or os.path.basename(rsh.split()[0] if rsh.split() else "ssh") == "ssh":
                hit = next((h for h in _remote_hosts(argv[1:]) if h in hosts), None)
        if hit:
            how = f"Use `{wrapper}` instead." if wrapper else \
                "Use the approved wrapper for this host (set `ssh_wrapper` in config/hooks.json)."
            return ("bare-ssh", f"Bare {name} to {hit} is blocked by config/hooks.json. {how}")
    return None


def main() -> None:
    payload = hookio.read_bash_payload()
    if payload is None:
        return
    root = os.environ.get("WORKSPACE_ROOT")
    if not root:
        return
    command = payload["tool_input"]["command"]
    try:
        hit = evaluate(command, load_config(root))
    except Exception:
        return
    if hit:
        hookio.deny(hit[0], hit[1], command)


if __name__ == "__main__":
    main()
