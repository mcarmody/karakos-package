#!/usr/bin/env python3
"""
block-heavy-build.py — PreToolUse (Bash) block on heavy local builds.

OPT-IN: a resource policy, not a safety rule. bin/hooks-sync.py wires this
hook only when config/hooks.json has `"heavy_build_block": true` (default
false). Useful on small hosts where tsc / next build / webpack saturate the
machine; run those in CI or on a build host instead.

Denies: tsc, next build, webpack, vite build, `npm ci`, bare installs
(`npm install` / `pnpm install` / `yarn` with no package argument), and
`npm|pnpm|yarn|bun run build|typecheck`. A scoped `npm install <pkg>` is
allowed. Command-position only (lib/bashcmd.py): `rg 'next build' f` passes.
Denies are logged to $WORKSPACE_ROOT/logs/blocked-bash.jsonl.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import bashcmd  # noqa: E402
import hookio  # noqa: E402

_PM = {"npm", "pnpm", "yarn", "bun"}
_BUILD_SCRIPTS = {"build", "typecheck", "type-check", "tsc"}
_VALUED = {"--prefix", "-C", "--cwd", "--workspace", "-w", "--filter", "--registry"}


def _unwrap(argv):
    """Strip package-runner prefixes: npx tsc -> tsc, pnpm exec tsc -> tsc."""
    a = [str(x) for x in argv]
    name = os.path.basename(a[0])
    if name in ("npx", "pnpx", "bunx"):
        rest = [x for x in a[1:]]
    elif name in _PM and len(a) > 1 and a[1] in ("exec", "dlx", "x"):
        rest = a[2:]
    elif name == "npm" and len(a) > 1 and a[1] == "exec":
        rest = a[2:]
    else:
        return a
    while rest and rest[0].startswith("-") and rest[0] != "--":
        rest = rest[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    return rest or a


def _positionals(args):
    out, skip = [], False
    for x in args:
        if skip:
            skip = False
            continue
        if x in _VALUED:
            skip = True
        elif not x.startswith("-"):
            out.append(x)
    return out


def check(argv):
    a = _unwrap(argv)
    if not a:
        return None
    name = os.path.basename(a[0])
    rest = a[1:]
    if name == "tsc":
        return "tsc"
    if name == "webpack":
        return "webpack"
    if name == "next" and "build" in rest[:1]:
        return "next build"
    if name == "vite" and "build" in rest[:1]:
        return "vite build"
    if name == "npm" and rest[:1] == ["ci"]:
        return "npm ci"
    if name in _PM:
        pos = _positionals(rest)
        sub = pos[0] if pos else None
        pkgs = pos[1:]
        if name == "yarn" and sub in (None, "install"):
            return "yarn install"
        if name != "yarn" and sub in ("install", "i") and not pkgs:
            return f"{name} install"
        if sub == "run" and pkgs and pkgs[0] in _BUILD_SCRIPTS:
            return f"{name} run {pkgs[0]}"
        if name in ("yarn", "pnpm") and sub in _BUILD_SCRIPTS:
            return f"{name} {sub}"
    return None


def evaluate(command):
    for argv in bashcmd.commands(command):
        what = check(argv)
        if what:
            return ("heavy-build",
                    f"`{what}` is blocked on this host (heavy_build_block in config/hooks.json). "
                    "Run it in CI or on a build host. A scoped `npm install <package>` is allowed.")
    return None


def main() -> None:
    payload = hookio.read_bash_payload()
    if payload is None:
        return
    command = payload["tool_input"]["command"]
    try:
        hit = evaluate(command)
    except Exception:
        return
    if hit:
        hookio.deny(hit[0], hit[1], command)


if __name__ == "__main__":
    main()
