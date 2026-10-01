"""Tests for lib/tengwar.py — Discord-safe rendering of agent posts.

Scoped run:
    python3 -m pytest tests/test_tengwar.py -q

PNG table tests are skipped when Pillow is not installed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import tengwar  # noqa: E402

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

needs_pil = pytest.mark.skipif(not HAVE_PIL, reason="Pillow not installed")


NARROW_TABLE = (
    "Here is the summary.\n"
    "\n"
    "| name | ok |\n"
    "| --- | --- |\n"
    "| a | yes |\n"
    "| b | no |\n"
    "\n"
    "That's it.\n"
)

WIDE_TABLE = (
    "Report follows.\n"
    "\n"
    "| Component | Status | Notes |\n"
    "| --- | --- | --- |\n"
    "| queue-broker unit tests | passing on both hosts | no flakes seen this week |\n"
    "| desktop build runner | needs a follow-up patch | concurrency cap still hardcoded |\n"
    "| Pi gitops poller | healthy | ~35 min alert threshold holding |\n"
    "\n"
    "End of report.\n"
)


def test_narrow_table_becomes_code_block(tmp_path):
    text, attachments = tengwar.render_for_discord(
        NARROW_TABLE, tmp_dir=tmp_path)
    assert attachments == []
    assert "```" in text
    assert "| a" not in text  # original GFM syntax gone
    assert "name" in text and "ok" in text
    assert "Here is the summary." in text
    assert "That's it." in text


@needs_pil
def test_wide_table_becomes_png_plus_caption(tmp_path):
    text, attachments = tengwar.render_for_discord(
        WIDE_TABLE, allow_table_images=True, tmp_dir=tmp_path)
    assert len(attachments) == 1
    png = attachments[0]
    assert png.endswith(".png")
    assert os.path.isfile(png)
    assert os.path.getsize(png) > 0
    assert "```" not in text  # replaced, not code-blocked
    assert "attached as an image" in text
    assert "Report follows." in text
    assert "End of report." in text
    # one-line caption: no raw GFM table syntax left behind
    assert "| Component" not in text


def test_wide_table_without_attachments_falls_back_to_code_block(tmp_path):
    text, attachments = tengwar.render_for_discord(
        WIDE_TABLE, tmp_dir=tmp_path)
    assert attachments == []
    assert "```" in text
    assert "attached image" not in text
    assert "queue-broker unit tests" in text


def test_tables_inside_fences_are_untouched(tmp_path):
    text_in = (
        "prose before\n"
        "```\n"
        "| a | b |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
        "```\n"
        "prose after\n"
    )
    text, attachments = tengwar.render_for_discord(
        text_in, tmp_dir=tmp_path)
    assert attachments == []
    assert text == text_in


def test_bold_stripped_inside_rendered_table(tmp_path):
    text_in = (
        "| name | ok |\n"
        "| --- | --- |\n"
        "| **a** | yes |\n"
    )
    text, _ = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert "**" not in text
    assert "a" in text


def test_idempotent_on_narrow_table(tmp_path):
    once, att1 = tengwar.render_for_discord(NARROW_TABLE, tmp_dir=tmp_path)
    twice, att2 = tengwar.render_for_discord(once, tmp_dir=tmp_path)
    assert once == twice
    assert att1 == att2 == []


@needs_pil
def test_idempotent_on_wide_table(tmp_path):
    # The rendered text is a stable fixed point: the caption carries no
    # filesystem path, so re-rendering it finds no new table and attaches
    # nothing further (the PNG from the first pass was already returned to
    # that caller, not re-discovered from the text).
    once, att1 = tengwar.render_for_discord(WIDE_TABLE, allow_table_images=True, tmp_dir=tmp_path)
    twice, att2 = tengwar.render_for_discord(once, tmp_dir=tmp_path)
    assert once == twice
    assert len(att1) == 1
    assert att2 == []


# ---------------------------------------------------------------------------
# Splitter interplay
# ---------------------------------------------------------------------------

def test_split_for_discord_balances_fences_across_chunks():
    body = "```python\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```\n"
    chunks = tengwar.split_for_discord(body, max_len=200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_split_for_discord_short_text_unchanged():
    assert tengwar.split_for_discord("hello") == ["hello"]


def test_wide_table_rendering_then_split_stays_fence_balanced(tmp_path):
    text, _ = tengwar.render_for_discord(
        WIDE_TABLE, tmp_dir=tmp_path)
    long_text = (text + "\n") * 30
    chunks = tengwar.split_for_discord(long_text, max_len=300)
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_escaped_pipe_kept_literal_in_cell(tmp_path):
    # Fix #1: `\|` inside a cell (often inside backticks) used to end the
    # cell early and leak every remaining row raw.
    text_in = (
        "| Expr | Meaning |\n"
        "| --- | --- |\n"
        "| `a \\| b` | a or b |\n"
        "| c | d |\n"
    )
    text, attachments = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert attachments == []
    assert "```" in text
    # backticks stripped (fix #5) but the escaped pipe survives as a literal
    # pipe, and the row after it is still part of the rendered table.
    assert "a | b" in text
    assert "a or b" in text
    assert "c" in text and "d" in text
    # no raw GFM syntax leaked past the table
    assert "\\|" not in text
    assert "| `a" not in text


def test_ragged_short_row_padded(tmp_path):
    # Fix #2: a row with fewer cells than the header used to end table
    # detection early and leak every remaining row raw.
    text_in = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        "| 1 | 2 |\n"
        "| x | y | z |\n"
    )
    text, _ = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert "```" in text
    assert "| 1 | 2 |\n" not in text  # not leaked as raw markdown
    assert "x" in text and "y" in text and "z" in text
    data_lines = [ln for ln in text.split("\n") if ln and ln not in ("```",) and "+" not in ln]
    # every data row line (header line included) has the same number of "|"
    # column separators — the short row was padded rather than leaking raw.
    header_pipes = data_lines[0].count("|")
    for ln in data_lines[1:]:
        assert ln.count("|") == header_pipes


def test_ragged_long_row_folded_into_last_column(tmp_path):
    # Fix #2: a row with MORE cells than the header folds the extras into
    # the last column instead of ending the table.
    text_in = (
        "| A | B | C |\n"
        "| --- | --- | --- |\n"
        "| 1 | 2 |\n"
        "| 3 | 4 | 5 | 6 |\n"
    )
    text, _ = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert "```" in text
    assert "5 | 6" in text  # folded, joined with " | "
    assert "| 3 | 4 | 5 | 6 |\n" not in text  # not leaked raw


def test_single_column_table_converted(tmp_path):
    # Fix #3: a one-column table wasn't recognized as a table at all.
    text_in = "| Only |\n|---|\n| one |\n| two |\n"
    text, attachments = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert attachments == []
    assert "```" in text
    assert "Only" in text
    assert "one" in text and "two" in text
    assert "| one |" not in text  # original GFM syntax gone


def test_bare_horizontal_rule_not_mistaken_for_table():
    # The single-column relaxation must not make a plain "---" horizontal
    # rule (no pipes at all) match as a table separator.
    assert not tengwar._TABLE_SEPARATOR_RE.match("---")
    assert not tengwar._TABLE_SEPARATOR_RE.match("   ---   ")


def test_display_width_cjk_and_emoji_and_combining():
    # Fix #4: CJK/full-width chars count as 2, combining marks as 0, plain
    # ASCII as 1 — len() gets all three wrong.
    assert tengwar._display_width("ab") == 2
    assert tengwar._display_width("漢字") == 4
    assert tengwar._display_width("😀") == 2
    # combining acute accent (U+0301) contributes 0 display columns.
    assert tengwar._display_width("é") == 1


def test_code_block_columns_align_with_cjk_content(tmp_path):
    # Fix #4: the pipe column must land at the same display-width offset on
    # every row, including one whose only difference from a narrower row is
    # padding computed with real display width instead of len().
    text_in = (
        "| Name | Mood |\n"
        "| --- | --- |\n"
        "| Gideon | happy |\n"
        "| 漢字 | 測試 |\n"
    )
    text, _ = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    lines = [ln for ln in text.split("\n") if ln.startswith("Name") or ln.startswith("Gideon")
              or ln.startswith("漢字")]
    widths = {tengwar._display_width(ln[:ln.index("|")]) for ln in lines}
    assert len(widths) == 1  # every row's "|" lands at the same display column


def test_cell_markup_backticks_and_links_stripped(tmp_path):
    # Fix #5: inline-code backticks stripped, markdown links reduced to
    # their link text, for both the code-block and PNG paths.
    text_in = (
        "| Link | Code |\n"
        "| --- | --- |\n"
        "| [PR #514](https://github.com/mcarmody/karakos/pull/514) | `x=1` |\n"
    )
    text, _ = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert "PR #514" in text
    assert "https://github.com" not in text
    body_lines = [ln for ln in text.split("\n") if ln != "```"]
    assert not any("`" in ln for ln in body_lines)  # only the fence markers carry backticks
    assert "x=1" in text


def test_bare_url_cell_kept(tmp_path):
    # Fix #5: a cell that is only a bare URL (no [text](url) wrapper) is
    # left alone rather than stripped.
    text_in = (
        "| Link |\n"
        "| --- |\n"
        "| https://example.com/path |\n"
    )
    text, _ = tengwar.render_for_discord(text_in, tmp_dir=tmp_path)
    assert "https://example.com/path" in text


@needs_pil
def test_long_cell_wraps_and_caps_png_width(tmp_path):
    # Fix #6: one very long cell used to blow the image out to thousands of
    # pixels wide (2964x84 before this fix); it must now wrap within the
    # table and stay under the ~1400px cap.
    from PIL import Image
    long_cell = "This is a very long cell that goes on and on. " * 6
    text_in = f"| Field | Description |\n| --- | --- |\n| why | {long_cell} |\n"
    text, attachments = tengwar.render_for_discord(text_in, allow_table_images=True, tmp_dir=tmp_path)
    assert len(attachments) == 1
    img = Image.open(attachments[0])
    assert img.width <= tengwar.MAX_PNG_WIDTH
    assert img.height > 60  # more than a single text-row's worth of height


@needs_pil
def test_caption_no_em_dash_and_singular_row(tmp_path):
    # Fix #7: house style forbids an em-dash in the caption, and the noun
    # must be singular for exactly one row.
    text_in = (
        "| PR | Build | Estimate | Actual | Agent time | Tokens | Notes |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| #503 | MAHANAXAR classifier library | 1.5% | 0.38% | 28 min | 28.9M | cache reads dominate |\n"
    )
    text, attachments = tengwar.render_for_discord(text_in, allow_table_images=True, tmp_dir=tmp_path)
    assert len(attachments) == 1
    assert "—" not in text  # no em-dash
    assert "1 row," in text
    assert "rows" not in text


@needs_pil
def test_caption_plural_rows(tmp_path):
    text, attachments = tengwar.render_for_discord(WIDE_TABLE, allow_table_images=True, tmp_dir=tmp_path)
    assert len(attachments) == 1
    assert "3 rows," in text
    assert "—" not in text


def test_long_table_code_block_splits_with_repeated_header():
    # Fix #8: a long table in a code block must not be handed whole to a
    # generic 2000-char splitter (which would cut it mid-fence); tengwar
    # itself must split it into several fenced blocks, each repeating the
    # header and divider and each well under Discord's cap.
    header = "| # | Item |\n|---|---|\n"
    rows = "\n".join(f"| {i} | row number {i} with quite a lot of padding text here |" for i in range(1, 121))
    text_in = header + rows + "\n"
    text, attachments = tengwar.render_for_discord(text_in)
    assert attachments == []
    blocks = text.split("```")
    # every non-empty fenced block (odd indices after split on ```) starts
    # with the header line and stays within Discord's ~2000-char cap.
    fenced = [b for i, b in enumerate(blocks) if i % 2 == 1]
    assert len(fenced) > 1  # actually split into more than one block
    for block in fenced:
        assert len(block) <= tengwar.MAX_CODE_BLOCK_CHARS + 10
        lines = [ln for ln in block.split("\n") if ln]
        assert lines[0].startswith("#")
        assert lines[1].startswith("-")


def _fenced_blocks(chunk: str) -> list[str]:
    parts = chunk.split("```")
    return [p for i, p in enumerate(parts) if i % 2 == 1]


def test_long_table_code_block_stays_fence_balanced_through_split_for_discord():
    # Same 120-row table, but this time run the rendered text through
    # split_for_discord to confirm it never tears a fenced block in half.
    #
    # A parity check alone (chunk.count("```") % 2 == 0) is not enough —
    # balance_fences guarantees that for ANY split, including a naive one
    # that cuts a table's code block in the middle: it just closes and
    # reopens the fence at the cut, leaving the second half's rows with no
    # header or divider for context. The real requirement (fix #8) is that
    # every fenced block a chunk contains starts with the table's own
    # header line — i.e. the block was deferred whole to its chunk, never
    # split mid-block.
    header = "| # | Item |\n|---|---|\n"
    rows = "\n".join(f"| {i} | row number {i} with quite a lot of padding text here |" for i in range(1, 121))
    # ~500 chars of intro prose ahead of the table — enough on its own to
    # push a naive last-newline cut into the middle of the FIRST rendered
    # code block (this is exactly the "40+ rows" scenario the task names;
    # the 40-row case alone is only ~1.5k chars and doesn't reach 1900).
    intro = "Intro prose before the table. " * 20 + "\n\n"
    text_in = intro + header + rows + "\n\nOutro prose after.\n"
    text, _ = tengwar.render_for_discord(text_in)
    assert text.count("```") >= 4  # confirms tengwar itself split into >1 block

    for splitter in (tengwar.split_for_discord,):
        chunks = splitter(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0
            for block in _fenced_blocks(chunk):
                block_lines = [ln for ln in block.split("\n") if ln]
                assert block_lines, f"{splitter}: empty fenced block in chunk"
                assert block_lines[0].startswith("#"), (
                    f"{splitter}: fenced block split mid-table, missing header: "
                    f"{block_lines[0]!r}"
                )
                assert block_lines[1].startswith("-")


def test_rewind_does_not_defer_a_block_too_big_to_fit_in_one_chunk():
    # The rewind in rewind_cut_before_open_fence must not fire on an
    # ordinary long fenced block that has nothing to do with a tengwar
    # table (e.g. a multi-KB diff pasted into a reply) and can never fit
    # in one chunk regardless of where the cut lands — deferring it whole
    # would just add a useless bare-prose chunk in front, then split it
    # mid-block anyway once it's on its own.
    diff_body = "\n".join(f"+line {i} of the diff" for i in range(200))  # ~4000 chars
    text_in = f"Here's the diff:\n```diff\n{diff_body}\n```\n"
    assert len(text_in) > 1900

    for splitter in (tengwar.split_for_discord,):
        chunks = splitter(text_in)
        assert len(chunks) > 1
        # The FIRST chunk must still carry part of the diff fence, not just
        # the one-line intro — proof the guard suppressed the rewind here.
        assert "```" in chunks[0], f"{splitter}: rewound an oversized block into a bare-prose chunk"
        for chunk in chunks:
            assert chunk.strip(), f"{splitter}: produced an empty chunk"
