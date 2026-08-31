"""Tests for ai_pipeline/extract.py's margin line-number filtering and
bold+italic leading-run detection -- pure logic, tested directly on plain
line/span dicts rather than a real PDF fixture (the rest of extract_pages
needs an actual PyMuPDF document and is exercised indirectly through the
full pipeline instead)."""
from ai_pipeline.extract import _drop_margin_line_numbers, _leading_bold_italic


def _bl(text, x0, y0=0.0):
    return {"bbox": (x0, y0, x0 + len(text) * 6, y0 + 12), "text": text, "size": 12.0, "bold": False}


# PyMuPDF span flags: bit 1 (2) = italic, bit 4 (16) = bold.
_PLAIN, _BOLD, _ITALIC, _BOLD_ITALIC = 4, 20, 6, 22


def _span(text, flags):
    return {"text": text, "flags": flags}


def test_drops_a_single_stray_line_number():
    block_lines = [_bl("relating to criminal procedure in the", 235.3), _bl("5", 57.5), _bl("Magistrates' Court", 235.3)]
    kept = _drop_margin_line_numbers(block_lines)
    assert [l["text"] for l in kept] == ["relating to criminal procedure in the", "Magistrates' Court"]


def test_drops_multiple_stray_line_numbers_in_the_same_block():
    """Regression: two line-number stragglers in the same block used to
    mask each other -- each one's "siblings" comparison set included the
    *other* straggler (also far left), which won the min() and defeated
    the gap check for both, instead of comparing against the real body
    text's own x0."""
    block_lines = [
        _bl("(2) The Chief Magistrate may from time to time, by", 190.1),
        _bl("10", 52.2),
        _bl("notice published in the Government Gazette,", 209.8),
        _bl("proceeding.", 209.8),
        _bl("15", 52.2),
    ]
    kept = _drop_margin_line_numbers(block_lines)
    assert [l["text"] for l in kept] == [
        "(2) The Chief Magistrate may from time to time, by",
        "notice published in the Government Gazette,",
        "proceeding.",
    ]


def test_keeps_a_short_number_close_to_its_siblings():
    """A short numeric line that ISN'T well separated from the rest of its
    block (e.g. a genuine subsection number printed close to its own
    text) is left alone -- only a line far enough left to be a margin
    annotation gets dropped."""
    block_lines = [_bl("123", 140.0), _bl("is the section number", 145.0)]
    assert _drop_margin_line_numbers(block_lines) == block_lines


def test_block_of_only_numbers_is_left_alone():
    """No real content in the block to compare against -- nothing to
    confidently call a margin annotation, so nothing is dropped."""
    block_lines = [_bl("5", 57.5), _bl("10", 57.5)]
    assert _drop_margin_line_numbers(block_lines) == block_lines


def test_non_numeric_short_text_is_never_dropped():
    block_lines = [_bl("the", 57.5), _bl("Magistrates' Court, the County Court and the", 235.3)]
    assert _drop_margin_line_numbers(block_lines) == block_lines


def test_leading_bold_italic_finds_a_defined_terms_own_lead_span():
    """"accused means a person who—" -- the real shape of a Definitions
    section's own entry: the term itself set bold+italic, the "means ..."
    continuation plain, both on the same physical line."""
    line = {"spans": [_span("accused ", _BOLD_ITALIC), _span("means a person who— ", _PLAIN)]}
    assert _leading_bold_italic(line) == "accused"


def test_leading_bold_italic_joins_a_multi_span_lead():
    line = {"spans": [_span("appropriate ", _BOLD_ITALIC), _span("registrar", _BOLD_ITALIC), _span(" means— ", _PLAIN)]}
    assert _leading_bold_italic(line) == "appropriate registrar"


def test_leading_bold_italic_returns_none_for_a_plain_line():
    line = {"spans": [_span("(a) is charged with an offence; or ", _PLAIN)]}
    assert _leading_bold_italic(line) is None


def test_leading_bold_italic_returns_none_for_bold_only_emphasis():
    """A bold-only (not also italic) lead -- an Act-name citation's usual
    styling elsewhere in the document -- isn't a defined term."""
    line = {"spans": [_span("Crimes Act 1958 ", _BOLD), _span("is amended.", _PLAIN)]}
    assert _leading_bold_italic(line) is None


def test_leading_bold_italic_returns_none_for_italic_only_emphasis():
    """Italic-only (not also bold) -- a case citation's usual styling --
    isn't a defined term either; both flags are required together."""
    line = {"spans": [_span("R v Smith ", _ITALIC), _span("was decided in 2001.", _PLAIN)]}
    assert _leading_bold_italic(line) is None


def test_leading_bold_italic_stops_at_the_first_non_matching_span():
    """A line that happens to end with a bold+italic run (not open with
    one) doesn't count -- only a *leading* run is a defined term's own
    introduction."""
    line = {"spans": [_span("see the definition of ", _PLAIN), _span("accused", _BOLD_ITALIC)]}
    assert _leading_bold_italic(line) is None
