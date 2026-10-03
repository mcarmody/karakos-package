"""post_guard: what never reaches a channel (B2)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import post_guard as pg  # noqa: E402


@pytest.mark.parametrize("text", ["", "   \n\t", "​‍﻿", None, " ⁠ \n"])
def test_blank_is_empty(text):
    assert pg.post_decision(text) == (False, "empty")


@pytest.mark.parametrize("text", ["PASS", " pass. ", "**PASS**", "`Pass`", '"pass"!', "_pass_",
                                  "​PASS​", "\n*pass*\n"])
def test_pass_variants(text):
    assert pg.is_pass(text) and pg.post_decision(text) == (False, "pass")


@pytest.mark.parametrize("text", ["PASS/WARN/FAIL: looks fine", "pass the salt", "passed", "no pass here",
                                  "PASS PASS", "hello"])
def test_not_pass(text):
    assert not pg.is_pass(text) and pg.post_decision(text) == (True, "")


def test_has_visible_ignores_markers_other_than_invisibles():
    assert pg.has_visible("PASS") and not pg.has_visible("​ ")
