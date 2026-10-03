"""One predicate for "should this text be posted to Discord" (stdlib, pure).

Two kinds of text never reach a channel: text with nothing visible in it, and
the convention reply PASS ("I have nothing to say here"). Every post path asks
this module, so interstitial turn events, final replies and incidental posts
agree on what counts.
"""
from __future__ import annotations

# Zero width space/joiners/marks U+200B..U+200F, word joiner, BOM, soft hyphen.
_INVISIBLE = {chr(c) for c in range(0x200B, 0x2010)} | {"⁠", "﻿", "­"}
_WRAP = set("*_`\"'.!“”‘’")


def _visible(text) -> str:
    if not isinstance(text, str):
        return ""
    return "".join(ch for ch in text if ch not in _INVISIBLE).strip()


def has_visible(text) -> bool:
    """True when anything is left after removing invisible chars and whitespace."""
    return bool(_visible(text))


def is_pass(text) -> bool:
    """The whole message is PASS (case-insensitive), ignoring whitespace and
    surrounding markdown/quote/punctuation. `PASS/WARN/FAIL: ok` is not a PASS."""
    s = _visible(text)
    while s and (s[0] in _WRAP or s[0].isspace()):
        s = s[1:]
    while s and (s[-1] in _WRAP or s[-1].isspace()):
        s = s[:-1]
    return s.lower() == "pass"


def post_decision(text):
    """(should_post, reason); reason is "" when posting, else `empty` or `pass`."""
    if not has_visible(text):
        return False, "empty"
    if is_pass(text):
        return False, "pass"
    return True, ""
