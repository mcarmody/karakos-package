"""
bashcmd.py — shell command segmentation shared by the Bash PreToolUse rails.

The rule every rail relies on: match in COMMAND POSITION only, never in prose.
`rg 'next build' f` and `echo "never run pkill -f"` contain the dangerous text
but run nothing dangerous, so a rail must not fire on them. This module splits
a command line into the commands that would actually execute:

  * quote-aware (single, double, backslash), heredoc-aware (bodies dropped),
    redirect-aware (redirect targets dropped);
  * splits on ; && || | & and newlines;
  * recurses into $(...), `...` and <(...) bodies, and into `bash -c`,
    `sh -c`, `eval` payloads;
  * strips wrapper prefixes (sudo, env, timeout, nice, nohup, ...) and leading
    VAR=value assignments.

`commands(cmd)` returns a list of argv lists (of `Word`, a str subclass whose
value is the unquoted text). It is best-effort: on anything it cannot parse it
returns what it has, and rails fail open.
"""
from __future__ import annotations

import os
import re

# Encodings inside Word.raw: characters that must NOT be expanded later.
_LIT_DOLLAR = "\x01"   # a `$` that came from single quotes / a backslash
_LIT_TILDE = "\x02"    # a `~` that was quoted
_CMDSUB = "\x03"       # a command/process substitution: value unknowable

_KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done",
             "!", "{", "}", "time", "coproc"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_MAX_DEPTH = 6


class Word(str):
    """An argv word. str value = unquoted text; extra attributes below."""
    raw: str
    glob: bool

    def __new__(cls, raw: str, glob: bool = False):
        obj = super().__new__(
            cls, raw.replace(_LIT_DOLLAR, "$").replace(_LIT_TILDE, "~").replace(_CMDSUB, "$(...)")
        )
        obj.raw = raw
        obj.glob = glob
        return obj


def _match_paren(s: str, i: int) -> int:
    """s[i] == '('. Return index of the matching ')' (quote-aware), or len(s)."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = (j if j != -1 else n) + 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _parse(s: str, depth: int = 0) -> list[list[Word]]:
    segs: list[list[Word]] = []
    if depth > _MAX_DEPTH:
        return segs
    n = len(s)
    i = 0
    cur: list[Word] = []
    buf: list[str] = []
    state = {"inword": False, "glob": False, "skip": False}
    heredocs: list[tuple[str, bool]] = []

    def put(text: str) -> None:
        buf.append(text)
        state["inword"] = True

    def end_word() -> None:
        if not state["inword"]:
            return
        raw = "".join(buf)
        buf.clear()
        glob = state["glob"]
        state["inword"] = False
        state["glob"] = False
        if state["skip"]:           # redirect target
            state["skip"] = False
            return
        if not cur and raw in _KEYWORDS:
            return
        cur.append(Word(raw, glob))

    def end_seg() -> None:
        nonlocal cur
        end_word()
        if cur:
            segs.append(cur)
        cur = []

    def sub(inner: str) -> None:
        segs.extend(_parse(inner, depth + 1))

    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 < n and s[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                nxt = s[i + 1]
                put(_LIT_DOLLAR if nxt == "$" else (_LIT_TILDE if nxt == "~" and not buf else nxt))
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j == -1:
                j = n
            put("".join(_LIT_DOLLAR if ch == "$" else (_LIT_TILDE if ch == "~" and not buf else ch)
                        for ch in s[i + 1:j]) or "")
            state["inword"] = True
            i = j + 1
            continue
        if c == '"':
            i += 1
            state["inword"] = True
            while i < n and s[i] != '"':
                ch = s[i]
                if ch == "\\" and i + 1 < n:
                    nxt = s[i + 1]
                    if nxt in '$`"\\':
                        put(_LIT_DOLLAR if nxt == "$" else nxt)
                        i += 2
                        continue
                    if nxt == "\n":
                        i += 2
                        continue
                    put("\\")
                    i += 1
                    continue
                if ch == "$" and s.startswith("$(", i):
                    j = _match_paren(s, i + 1)
                    sub(s[i + 2:j])
                    put(_CMDSUB)
                    i = j + 1
                    continue
                if ch == "`":
                    j = s.find("`", i + 1)
                    j = n if j == -1 else j
                    sub(s[i + 1:j])
                    put(_CMDSUB)
                    i = j + 1
                    continue
                put(_LIT_TILDE if ch == "~" and not buf else ch)
                i += 1
            i += 1  # closing quote
            continue
        if c == "$" and s.startswith("$(", i):
            j = _match_paren(s, i + 1)
            sub(s[i + 2:j])
            put(_CMDSUB)
            i = j + 1
            continue
        if c == "`":
            j = s.find("`", i + 1)
            j = n if j == -1 else j
            sub(s[i + 1:j])
            put(_CMDSUB)
            i = j + 1
            continue
        if c == "#" and not state["inword"]:
            j = s.find("\n", i)
            i = n if j == -1 else j
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_seg()
            i += 1
            while heredocs and i <= n:
                delim, strip = heredocs.pop(0)
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:n if j == -1 else j]
                    i = n if j == -1 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            continue
        if c in "<>":
            if s.startswith("<(", i) or s.startswith(">(", i):
                j = _match_paren(s, i + 1)
                sub(s[i + 2:j])
                put(_CMDSUB)
                i = j + 1
                continue
            # fd number glued to the operator (`2>`) is not an argument
            if state["inword"] and "".join(buf).isdigit():
                buf.clear()
                state["inword"] = False
            else:
                end_word()
            if s.startswith("<<", i) and not s.startswith("<<<", i):
                i += 2
                strip = i < n and s[i] == "-"
                if strip:
                    i += 1
                while i < n and s[i] in " \t":
                    i += 1
                d: list[str] = []
                while i < n and s[i] not in " \t\n;&|()<>":
                    if s[i] in "'\"":
                        q = s[i]
                        j = s.find(q, i + 1)
                        j = n if j == -1 else j
                        d.append(s[i + 1:j])
                        i = j + 1
                    elif s[i] == "\\" and i + 1 < n:
                        d.append(s[i + 1])
                        i += 2
                    else:
                        d.append(s[i])
                        i += 1
                heredocs.append(("".join(d), strip))
                continue
            while i < n and s[i] in "<>&|":
                i += 1
            state["skip"] = True
            continue
        if c == "&" and s.startswith("&>", i):
            end_word()
            i += 2
            if i < n and s[i] == ">":
                i += 1
            state["skip"] = True
            continue
        if c in ";|&":
            end_seg()
            while i < n and s[i] in ";|&":
                i += 1
            continue
        if c in "()":
            end_seg()
            i += 1
            continue
        if c in "*?[":
            state["glob"] = True
        put(c)
        i += 1
    end_seg()
    return segs


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# wrapper -> (options that consume a value, number of leading positionals to skip)
_WRAPPERS = {
    "sudo": ({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group"}, 0),
    "doas": ({"-u", "-C"}, 0),
    "env": ({"-u", "-C", "-S", "--unset", "--chdir"}, 0),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1),
    "nice": ({"-n", "--adjustment"}, 0),
    "ionice": ({"-c", "-n", "-p", "-t"}, 0),
    "nohup": (set(), 0),
    "setsid": (set(), 0),
    "command": (set(), 0),
    "exec": ({"-a"}, 0),
    "stdbuf": ({"-i", "-o", "-e"}, 0),
    "chronic": (set(), 0),
}


def _strip_prefix(argv: list[Word]) -> list[Word]:
    """Drop VAR=value assignments and wrapper commands (with their options)."""
    argv = list(argv)
    for _ in range(12):
        while argv and _ASSIGN.match(argv[0]):
            argv = argv[1:]
        if not argv:
            return argv
        name = os.path.basename(argv[0])
        spec = _WRAPPERS.get(name)
        if spec is None:
            return argv
        valued, positionals = spec
        i = 1
        while i < len(argv):
            a = str(argv[i])
            if a == "--":
                i += 1
                break
            if name == "env" and _ASSIGN.match(a):
                i += 1
                continue
            if name == "nice" and re.fullmatch(r"-\d+", a):
                i += 1
                continue
            if a in valued:
                i += 2
                continue
            if a.startswith("-") and len(a) > 1:
                i += 1
                continue
            break
        i += positionals
        argv = argv[i:]
    return argv


def commands(cmd: str, _depth: int = 0) -> list[list[Word]]:
    """Every command that `cmd` would execute, as argv lists, wrappers stripped."""
    out: list[list[Word]] = []
    if not isinstance(cmd, str) or _depth > _MAX_DEPTH:
        return out
    for seg in _parse(cmd):
        argv = _strip_prefix(seg)
        if not argv:
            continue
        out.append(argv)
        name = os.path.basename(argv[0])
        if name in _SHELLS:
            for k, a in enumerate(argv[1:], 1):
                if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", a) and k + 1 < len(argv):
                    out.extend(commands(str(argv[k + 1]), _depth + 1))
                    break
        elif name == "eval" and len(argv) > 1:
            out.extend(commands(" ".join(str(a) for a in argv[1:]), _depth + 1))
    return out


_VAR = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand_word(word: Word, env: dict, home: str | None = None) -> str | None:
    """Expand ~ and $VAR in `word`. None if it cannot be resolved to one path
    (unset/empty variable, command substitution, glob, unsupported syntax)."""
    if getattr(word, "glob", False):
        return None
    raw = word.raw if hasattr(word, "raw") else str(word)
    if _CMDSUB in raw:
        return None
    if raw.startswith("~"):
        if raw == "~" or raw.startswith("~/"):
            if not home:
                return None
            raw = home + raw[1:]
        else:
            return None
    bad = False

    def repl(m: re.Match) -> str:
        nonlocal bad
        val = env.get(m.group(1) or m.group(2), "")
        if not val:
            bad = True
        return val

    raw = _VAR.sub(repl, raw)
    if bad:
        return None
    if re.search(r"\$[^\x01]|\$$", raw.replace(_LIT_DOLLAR, "")):
        return None  # $1, ${X:-y}, $$ ... unsupported -> unresolvable
    return raw.replace(_LIT_DOLLAR, "$").replace(_LIT_TILDE, "~")
