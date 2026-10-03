"""Secret masking for the turn-log surfaces (spec 6.4). stdlib only.

Surfaces: the stream-log tee (`redact_line`), `turn_events` rows and the tool
line (`redact_text`), and error text (`redact_for_log`). The in-memory events
the turn loop acts on and the agent's final reply are never redacted here.

`PATTERNS` is the shape-based list: the three that `redact_for_log` always had,
the seven of `system/check-secrets.py` (same regex text; a test pins the two
lists together) and AWS access keys. `register_literals` adds exact values
taken from environment variables whose names look secret, which catches
secrets with no recognisable shape.
"""
import json
import re
from typing import Dict, Iterable, List, Tuple

MASK = "[redacted]"
NAME_RE = re.compile(r"(?i)(token|secret|key|password|passwd|credential)")

# (name, regex text). The first three are the original `redact_for_log` set.
PATTERNS: List[Tuple[str, str]] = [
    ("legacy-prefixed-key", r"(?:sk|pk|xox[a-z]|gh[pousr]|glpat)[-_][A-Za-z0-9_\-]{10,}"),
    ("legacy-keyword-value", r"(?i)\b(bearer|token|api[_-]?key|secret|password)([\"'\s:=]+)[^\s\"',}]{6,}"),
    ("legacy-jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"),
    # system/check-secrets.py PATTERNS, same regex text.
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    ("openai-key", r"\bsk-(proj-)?[A-Za-z0-9]{32,}\b"),
    ("slack-token", r"\bxox[bap]-[A-Za-z0-9-]{10,}\b"),
    ("discord-bot-token", r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,38}\b"),
    ("auth-header", r"Authorization:\s*(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"),
]

# A private key masks from the BEGIN line through the END line, or to the end.
_PRIVATE_KEY_TAIL = r"(?:.*?-----END [A-Z ]*PRIVATE KEY-----|.*\Z)"


def _compile(name: str, text: str):
    if name == "private-key":
        return re.compile(text + _PRIVATE_KEY_TAIL, re.DOTALL)
    return re.compile(text)


_COMPILED = [(n, _compile(n, t)) for n, t in PATTERNS]

# Cheap pre-filter for `redact_line`: any anchor a pattern needs. Looser than
# the patterns on purpose (a JSON-escaped line has backslashes the patterns do
# not expect); `token(?!s)` keeps usage fields (`input_tokens`) off the slow path.
_QUICK = re.compile(
    r"(?i)bearer|token(?!s)|api[_-]?key|secret|password|"
    r"sk[-_]|pk[-_]|xox|gh[pousr]_|glpat|eyJ|AKIA|PRIVATE KEY|Authorization|"
    r"[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.")

_literals: List[str] = []          # longest first
_literal_escaped: List[str] = []   # the JSON-escaped form of each, for the raw check


def register_literals(env: Dict[str, str], min_len: int = 8) -> int:
    """Record the values of env vars named like secrets (>= min_len chars).
    Returns how many new values were recorded. Idempotent."""
    added = 0
    for name, value in (env or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not NAME_RE.search(name):
            continue
        value = value.strip()
        if len(value) < min_len or value in _literals:
            continue
        _literals.append(value)
        added += 1
    if added:
        _literals.sort(key=len, reverse=True)
        _literal_escaped[:] = [json.dumps(v)[1:-1] for v in _literals]
    return added


def clear_literals() -> None:
    _literals.clear()
    _literal_escaped.clear()


def _mask_keyword(m):
    return f"{m.group(1)}{m.group(2)}{MASK}"


def redact_text(s) -> str:
    out = s if isinstance(s, str) else str(s if s is not None else "")
    for lit in _literals:
        if lit in out:
            out = out.replace(lit, MASK)
    for name, rx in _COMPILED:
        if name == "legacy-keyword-value":
            out = rx.sub(_mask_keyword, out)
        else:
            out = rx.sub(MASK, out)
    return out


def redact_value(obj):
    """Redact every `str` leaf of dicts and lists; keys are untouched."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_value(v) for v in obj]
    return obj


def _needs_work(text: str) -> bool:
    if _QUICK.search(text):
        return True
    for lit, esc in zip(_literals, _literal_escaped):
        if lit in text or esc in text:
            return True
    return False


def redact_line(line: bytes) -> bytes:
    """Mask a raw stream-json line, keeping it valid JSON when it was. A clean
    line is returned as is (one regex scan). A line that does not parse is kept
    and masked as text."""
    if not line:
        return line
    text = line.decode("utf-8", errors="replace")
    if not _needs_work(text):
        return line
    nl = b"\n" if line.endswith(b"\n") else b""
    body = text[:-1] if nl else text
    try:
        obj = json.loads(body)
    except ValueError:
        return redact_text(text).encode("utf-8") if not nl else \
            redact_text(body).encode("utf-8") + nl
    return json.dumps(redact_value(obj), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + nl
