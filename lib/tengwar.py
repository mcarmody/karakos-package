"""tengwar.py — Discord-safe rendering of agent-authored prose.

Agents post markdown Discord cannot render: pipe tables arrive as raw pipes,
and a long reply split on length alone cuts code fences in half. This module
translates both into a form Discord can show. Stdlib only; Pillow is optional
and used only for the opt-in table-as-PNG path.

Pure and deterministic — no model call, no network, no Discord API. Posting
call sites run render_for_discord() then split_for_discord() and send the
chunks; this module never talks to Discord itself.

1. Tables: GFM pipe tables outside fenced code blocks become aligned
   monospace code blocks (display width, not len() — CJK and emoji-
   presentation characters are 2 columns; bold, inline backticks and
   [text](url) links are stripped from cells). A block that would exceed
   MAX_CODE_BLOCK_CHARS is split into several fenced blocks, each repeating
   the header and divider. Ragged rows are padded/folded; escaped pipes stay
   literal. With allow_table_images=True a wide table is instead rendered to
   a PNG (Pillow required; code block fallback otherwise).

2. Splitting: split_for_discord() cuts on line/space boundaries, defers a
   whole fenced block to the next chunk when it fits, and balance_fences()
   closes and reopens ``` fences (keeping the language tag) across cuts.

Ported from the household repo (mcarmody/karakos) commits 72aea3b9c,
8f575e8a7, a0d5d7338, 94775af49. Household channel gating and local-file-path
attachment were left behind.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path

MAX_ATTACHMENTS = 10  # Discord's own per-message limit
NARROW_TABLE_WIDTH = 60

# A GFM separator row, including a single-column table's ("|---|", no `+`
# group needed) — but a lookahead requires at least one literal "|"
# somewhere in the line, so a bare horizontal rule ("---") with no pipes at
# all never matches this by accident (that group was `+` before, which
# happened to rule out the bare-rule case as a side effect; relaxing it to
# `*` for single columns needed the lookahead to keep that guarantee).
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*(?=.*\|)\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$"
)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BACKTICK_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# Cap on a single wrapped line inside a PNG table cell (display-width
# columns), and on the whole image's pixel width — see _render_table_png.
MAX_CELL_WRAP_CHARS = 40
MAX_PNG_WIDTH = 1400
MIN_CELL_WRAP_CHARS = 10

# Discord's own hard cap is 2000 chars; stay clear of it the same way the
# splitters below do, so a long code-block table gets split into several
# fenced blocks (each repeating the header/divider) instead of being handed
# whole to a generic splitter that would cut it mid-block.
# Deliberately below the 1900-ish limit every downstream splitter cuts at
# (this module's own split_for_discord) — a fenced
# block sized right up to that limit leaves a fence-aware splitter no room
# to ever fit a whole block plus surrounding blank-line separators inside
# one of its own chunks, so rewind_cut_before_open_fence would have
# nothing to rewind TO and would fall back to a mid-block cut anyway.
MAX_CODE_BLOCK_CHARS = 1700



def _split_unescaped_pipes(s: str) -> list[str]:
    """Split on ``|``, treating ``\\|`` as a literal pipe belonging to the
    cell rather than a column delimiter (fix #1: an escaped pipe, often
    inside backticks, used to end the cell early and leak the rest of the
    row raw). The escape is consumed here, so the returned parts already
    have ``\\|`` resolved to a plain ``|`` — no separate unescape pass
    needed."""
    parts: list[str] = []
    cur: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "|":
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _parse_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    # A trailing "|" only closes the row if it isn't itself an escaped
    # (literal) pipe.
    if s.endswith("|") and not (len(s) >= 2 and s[-2] == "\\"):
        s = s[:-1]
    return [c.strip() for c in _split_unescaped_pipes(s)]


def _clean_cell(cell: str) -> str:
    """Strip cell markup that only makes sense in rendered markdown, for
    the code-block and PNG paths: bold, inline-code backticks, and
    markdown links (kept as their link text — a bare URL with no [text]()
    wrapper is left alone, per spec)."""
    cell = _BOLD_RE.sub(r"\1", cell)
    cell = _LINK_RE.sub(r"\1", cell)
    cell = _BACKTICK_RE.sub(r"\1", cell)
    return cell


def _normalize_row(row: list[str], ncols: int) -> list[str]:
    """Pad a short row with empty cells, or fold a long row's extra cells
    into the last column (joined with " | ") — fix #2. Ragged rows used to
    end table detection early and leak every remaining row as raw markdown."""
    if len(row) < ncols:
        return row + [""] * (ncols - len(row))
    if len(row) > ncols:
        return row[:ncols - 1] + [" | ".join(row[ncols - 1:])]
    return row


def _display_width(s: str) -> int:
    """Terminal display width (fix #4): CJK/full-width characters and
    emoji-presentation sequences count as 2 columns, combining marks as 0,
    everything else as 1. ``len()`` undercounts the first case and
    overcounts the second, which is what misaligned code-block columns
    carrying CJK or emoji content.

    No external dependency: wcwidth isn't installed on either host this
    runs on (desktop or Pi, checked directly), so this implements the rule
    by hand with unicodedata rather than silently varying behavior by
    which host happens to have the package."""
    width = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        cat = unicodedata.category(ch)
        if cat in ("Mn", "Me", "Cf") or unicodedata.combining(ch):
            i += 1
            continue
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        # An emoji-presentation sequence (base char + VARIATION SELECTOR-16,
        # U+FE0F) always renders at emoji width even when the base
        # character alone is narrow/ambiguous (e.g. many dingbats).
        if i + 1 < n and ord(s[i + 1]) == 0xFE0F:
            w = 2
        width += w
        i += 1
    return width


def _ljust_display(s: str, width: int) -> str:
    pad = width - _display_width(s)
    return s + (" " * pad if pad > 0 else "")


def _wrap_cell(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap to at most `max_chars` display-width columns per
    line. A single word/URL longer than the budget is hard-broken rather
    than left to overflow the column."""
    if text == "":
        return [""]

    def _break_long_word(word: str) -> list[str]:
        pieces = []
        piece = ""
        for ch in word:
            candidate = piece + ch
            if piece and _display_width(candidate) > max_chars:
                pieces.append(piece)
                piece = ch
            else:
                piece = candidate
        if piece:
            pieces.append(piece)
        return pieces

    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        candidate = f"{cur} {word}" if cur else word
        if _display_width(candidate) <= max_chars:
            cur = candidate
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if _display_width(word) > max_chars:
            broken = _break_long_word(word)
            for p in broken[:-1]:
                lines.append(p)
            cur = broken[-1] if broken else ""
        else:
            cur = word
    if cur or not lines:
        lines.append(cur)
    return lines


def _fence_line_mask(lines: list[str]) -> list[bool]:
    """True for every line that is inside (or is a delimiter of) a fenced
    code block.

    Scans for ``` by occurrence across the whole text, not by a per-line
    ``line.lstrip().startswith("```")`` check — the latter is exactly what
    broke fence balancing on an emoji-prefixed fence like
    "<emoji> ```handoff": the opening fence wasn't the first thing on its line, so a
    prefix check never saw it.
    """
    text = "\n".join(lines)
    line_starts = []
    off = 0
    for ln in lines:
        line_starts.append(off)
        off += len(ln) + 1

    def line_of(char_idx: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= char_idx:
                lo = mid
            else:
                hi = mid - 1
        return lo

    mask = [False] * len(lines)
    in_fence = False
    open_line = None
    search_idx = 0
    while True:
        i = text.find("```", search_idx)
        if i == -1:
            break
        search_idx = i + 3
        ln = line_of(i)
        if not in_fence:
            in_fence = True
            open_line = ln
        else:
            for k in range(open_line, ln + 1):
                mask[k] = True
            in_fence = False
    if in_fence and open_line is not None:
        for k in range(open_line, len(lines)):
            mask[k] = True
    return mask


def balance_fences(chunks: list[str]) -> list[str]:
    """Close and reopen ``` fences across a chunk split, preserving the
    language tag, so a split never leaves a chunk with an unclosed fence.
    """
    out, carry = [], ""
    for chunk in chunks:
        body = carry + chunk
        opens = 0
        lang = ""
        idx = 0
        while True:
            i = body.find("```", idx)
            if i == -1:
                break
            idx = i + 3
            if opens:
                opens = 0
            else:
                opens = 1
                eol = body.find("\n", idx)
                lang = (body[idx:eol] if eol != -1 else body[idx:]).strip()
        if opens:
            body += "\n```"
            carry = f"```{lang}\n"
        else:
            carry = ""
        out.append(body)
    return out


def rewind_cut_before_open_fence(text: str, cut: int, limit: int | None = None) -> int:
    """If `cut` lands strictly inside a fenced code block that opened
    earlier within `text[:cut]`, move `cut` back to the start of the line
    that opened that fence — so the whole fenced block is deferred whole
    to the next chunk instead of being split mid-block (fix #8: a long
    table's code block, rendered by tengwar itself into one or more
    blocks already under budget, must not be re-split by a downstream
    2000-char splitter that only knows about line breaks, not fences —
    that used to ship a chunk with two headerless tail rows and no
    header/divider for context).

    `balance_fences` already keeps the OUTPUT syntactically valid either
    way (every chunk gets a closed fence); this is what keeps a fenced
    block's *content* — specifically, a table's header — from being torn
    in half in the first place.

    Only rewinds when the WHOLE fenced block (open line through its own
    closing ``` ) fits within `limit` on its own — otherwise this would
    also fire on an ordinary long fenced block that has nothing to do with
    a tengwar table (e.g. a multi-KB diff pasted into a reply), where
    deferring it whole just adds a bare-prose chunk in front and then
    splits it mid-block anyway once it's on its own, since it never fit in
    one chunk to begin with. `limit=None` skips that check (used by
    plain-text callers that don't have a chunk budget to compare against).

    Returns `cut` unchanged if there's no open fence at that point, if the
    fence opened at the very start of `text` (nothing to gain by rewinding
    to 0 — that produces an empty chunk and no progress), or if the block
    doesn't fit under `limit` regardless of where it starts."""
    prefix = text[:cut]
    if prefix.count("```") % 2 == 0:
        return cut  # no fence open at the cut point
    last_fence_idx = prefix.rfind("```")
    line_start = prefix.rfind("\n", 0, last_fence_idx) + 1
    if line_start <= 0:
        return cut  # the fence opens at (or before) the start of this window
    if limit is not None:
        closer = text.find("```", last_fence_idx + 3)
        if closer == -1 or (closer + 3 - line_start) > limit:
            return cut  # block doesn't fit in one chunk regardless of the cut
    return line_start


def split_for_discord(text: str, max_len: int = 1900) -> list[str]:
    """Split a long message into Discord-sized chunks, preferring line
    breaks, then space, and always fence-balanced. Shared splitter for
    callers that don't already have their own fence-aware one."""
    if not text:
        return ["(no response)"]
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = max_len
        newline_pos = text.rfind("\n", 0, max_len)
        if newline_pos > max_len // 2:
            split_at = newline_pos + 1
        else:
            space_pos = text.rfind(" ", 0, max_len)
            if space_pos > max_len // 2:
                split_at = space_pos + 1
        split_at = rewind_cut_before_open_fence(text, split_at, limit=max_len)
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return balance_fences(chunks)


def _render_table_png(header: list[str], rows: list[list[str]], tmp_dir: Path) -> Path | None:
    """Render a table to a PNG, opaque background so it reads on either
    Discord theme. Returns None (never raises) if Pillow or a usable font
    isn't available — callers must treat that as "no image" and fall back
    to a code block, never drop the table's content.

    Long cells are wrapped to at most MAX_CELL_WRAP_CHARS display-width
    columns per line (fix #6) so one very long cell doesn't blow the image
    out to thousands of pixels wide; if wrapping at that cap still doesn't
    fit under MAX_PNG_WIDTH, the widest column's wrap width is shrunk
    (down to a MIN_CELL_WRAP_CHARS floor) and everything is rewrapped."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font = None
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ):
        if Path(candidate).is_file():
            try:
                font = ImageFont.truetype(candidate, 18)
                break
            except OSError:
                continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            return None

    pad_x, pad_y = 16, 10
    line_h = 22
    row_pad = 8
    # Breathing room beyond the last column's own right-side pad_x — the
    # per-cell pad_x*2 already gives every column left+right padding (see
    # col_x/slot_widths below), but a table's right EDGE benefits from a
    # bit more than one column's worth of padding so the last column's
    # text and the image border don't read as touching.
    right_margin = 14
    all_rows = [header] + rows
    ncols = len(header)

    tmp_img = Image.new("RGB", (10, 10))
    tmp_draw = ImageDraw.Draw(tmp_img)

    def text_w(s: str) -> int:
        if hasattr(tmp_draw, "textlength"):
            w = tmp_draw.textlength(s, font=font)
        else:
            bbox = tmp_draw.textbbox((0, 0), s, font=font)
            w = bbox[2] - bbox[0]
        return int(w) + 1  # +1: ceiling guard

    def wrap_all(max_chars_per_col: list[int]):
        wrapped = []
        for row in all_rows:
            wrapped.append([_wrap_cell(cell, max_chars_per_col[c]) for c, cell in enumerate(row)])
        return wrapped

    def col_widths_for(wrapped_rows) -> list[int]:
        widths = [0] * ncols
        for wrow in wrapped_rows:
            for c, lines in enumerate(wrow):
                for ln in lines:
                    widths[c] = max(widths[c], text_w(ln))
        return widths

    max_chars_per_col = [MAX_CELL_WRAP_CHARS] * ncols
    wrapped_rows = wrap_all(max_chars_per_col)
    col_widths = col_widths_for(wrapped_rows)
    for _ in range(50):
        slot_widths = [w + pad_x * 2 for w in col_widths]
        total_w_guess = sum(slot_widths) + right_margin
        if total_w_guess <= MAX_PNG_WIDTH:
            break
        # Shrink the column with the most wrap headroom left (i.e. not
        # already at the floor), rewrap, and recheck.
        shrinkable = [c for c in range(ncols) if max_chars_per_col[c] > MIN_CELL_WRAP_CHARS]
        if not shrinkable:
            break
        widest = max(shrinkable, key=lambda c: col_widths[c])
        max_chars_per_col[widest] = max(MIN_CELL_WRAP_CHARS, max_chars_per_col[widest] - 5)
        wrapped_rows = wrap_all(max_chars_per_col)
        col_widths = col_widths_for(wrapped_rows)

    # Per-column slot = text width + pad_x on both sides. col_x[c] is the
    # left edge of column c's slot; col_x[c+1] is both its right edge AND
    # the vertical divider position — computed once, up front, so text
    # placement and divider placement can never drift apart the way the
    # previous "x - pad_x" arithmetic did (that put the divider exactly at
    # the text's right edge, i.e. zero right padding).
    slot_widths = [w + pad_x * 2 for w in col_widths]
    col_x = [0] * (ncols + 1)
    for c in range(ncols):
        col_x[c + 1] = col_x[c] + slot_widths[c]
    total_w = col_x[-1] + right_margin

    row_line_counts = [max(len(lines) for lines in wrow) for wrow in wrapped_rows]
    row_heights = [n * line_h + row_pad * 2 for n in row_line_counts]
    total_h = sum(row_heights) + pad_y * 2

    bg = (255, 255, 255)
    header_bg = (226, 226, 230)
    grid = (150, 150, 150)
    text_color = (20, 20, 20)

    img = Image.new("RGB", (total_w, total_h), bg)
    draw = ImageDraw.Draw(img)

    def draw_row(wrapped_cells, y: int, row_h: int) -> None:
        for c, lines in enumerate(wrapped_cells):
            for li, ln in enumerate(lines):
                draw.text((col_x[c] + pad_x, y + row_pad + li * line_h), ln,
                          fill=text_color, font=font)
            if c < ncols - 1:
                draw.line([(col_x[c + 1], y), (col_x[c + 1], y + row_h)], fill=grid)

    y = pad_y
    draw.rectangle([0, y, total_w, y + row_heights[0]], fill=header_bg)
    draw_row(wrapped_rows[0], y, row_heights[0])
    y += row_heights[0]

    for wrow, row_h in zip(wrapped_rows[1:], row_heights[1:]):
        draw_row(wrow, y, row_h)
        draw.line([(0, y), (total_w, y)], fill=grid)
        y += row_h
    draw.line([(0, pad_y), (total_w, pad_y)], fill=grid)
    draw.line([(0, y), (total_w, y)], fill=grid)

    digest = hashlib.sha1(
        ("\n".join(["|".join(r) for r in all_rows])).encode("utf-8")
    ).hexdigest()[:12]
    out_path = Path(tmp_dir) / f"tengwar-table-{digest}.png"
    tmp_dir_p = Path(tmp_dir)
    tmp_dir_p.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def _code_block_lines(header_cells: list[str], widths: list[int],
                       body: list[list[str]]) -> list[str]:
    """Render a table as one or more fenced code blocks, splitting the body
    across several blocks (each repeating the header and divider) rather
    than emitting one giant block a generic 2000-char splitter would later
    cut mid-fence (fix #8). Returns already-joined output lines, blocks
    separated by a blank line."""

    def fmt_row(cells: list[str]) -> str:
        return " | ".join(_ljust_display(cell, widths[k]) for k, cell in enumerate(cells))

    header_line = fmt_row(header_cells)
    divider_line = "-+-".join("-" * w for w in widths)
    row_lines = [fmt_row(row) for row in body]

    def block_text(rows_subset: list[str]) -> str:
        return "\n".join(["```", header_line, divider_line, *rows_subset, "```"])

    chunks: list[str] = []
    cur: list[str] = []
    for rl in row_lines:
        candidate = cur + [rl]
        if cur and len(block_text(candidate)) > MAX_CODE_BLOCK_CHARS:
            chunks.append(block_text(cur))
            cur = [rl]
        else:
            cur = candidate
    chunks.append(block_text(cur))

    out: list[str] = []
    for idx, chunk in enumerate(chunks):
        out.extend(chunk.split("\n"))
        if idx != len(chunks) - 1:
            out.append("")
    return out


def _render_tables(text: str, *, allow_table_images: bool, tmp_dir: Path,
                    attachments: list[str]) -> str:
    lines = text.split("\n")
    fence_mask = _fence_line_mask(lines)
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if (not fence_mask[i] and "|" in line and i + 1 < n and not fence_mask[i + 1]
                and _TABLE_SEPARATOR_RE.match(lines[i + 1])):
            header_cells_raw = _parse_row(line)
            sep_cells = _parse_row(lines[i + 1])
            if len(header_cells_raw) >= 1 and len(sep_cells) == len(header_cells_raw):
                ncols = len(header_cells_raw)
                header_cells = [_clean_cell(c) for c in header_cells_raw]
                body: list[list[str]] = []
                j = i + 2
                while j < n and not fence_mask[j] and "|" in lines[j]:
                    row = _normalize_row(_parse_row(lines[j]), ncols)
                    body.append([_clean_cell(c) for c in row])
                    j += 1

                widths = [_display_width(c) for c in header_cells]
                for row in body:
                    for k, cell in enumerate(row):
                        widths[k] = max(widths[k], _display_width(cell))

                rendered_width = sum(widths) + 3 * (len(widths) - 1)

                if rendered_width <= NARROW_TABLE_WIDTH or not allow_table_images:
                    out.extend(_code_block_lines(header_cells, widths, body))
                elif len(attachments) >= MAX_ATTACHMENTS:
                    # Cap already hit by earlier tables/paths: don't drop
                    # this table's content, fall back to a code block.
                    out.extend(_code_block_lines(header_cells, widths, body))
                else:
                    png_path = _render_table_png(header_cells, body, tmp_dir)
                    if png_path is None:
                        # Pillow/font unavailable — degrade to text, never
                        # silently drop the table.
                        out.extend(_code_block_lines(header_cells, widths, body))
                    else:
                        attachments.append(str(png_path))
                        noun = "row" if len(body) == 1 else "rows"
                        out.append(
                            f"*[table: {len(body)} {noun}, attached as an image]*"
                        )
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def render_for_discord(text: str, *, allow_table_images: bool = False,
                       tmp_dir: str | Path | None = None) -> tuple[str, list[str]]:
    """Render agent-authored prose for posting to Discord.

    Returns (rendered_text, attachment_paths). Pure and deterministic — no
    model, no network. Text inside fenced code blocks is never modified and
    never scanned for tables.

    allow_table_images=False (default): every table becomes a monospace code
    block. True: a table wider than NARROW_TABLE_WIDTH is rendered to a PNG
    (needs Pillow; falls back to a code block when it is missing) and its
    path is returned in attachment_paths — the caller must be able to upload
    it. tmp_dir: where table PNGs are written; defaults to the system temp dir.
    """
    tmp_dir = Path(tmp_dir) if tmp_dir is not None else Path(tempfile.gettempdir())
    attachments: list[str] = []
    text = _render_tables(text, allow_table_images=allow_table_images,
                          tmp_dir=tmp_dir, attachments=attachments)
    return text, attachments
