"""Tests for ai_pipeline/extract.py's margin line-number filtering --
pure logic, tested directly on plain line dicts rather than a real PDF
fixture (the rest of extract_pages needs an actual PyMuPDF document and
is exercised indirectly through the full pipeline instead)."""
from ai_pipeline.extract import _drop_margin_line_numbers


def _bl(text, x0, y0=0.0):
    return {"bbox": (x0, y0, x0 + len(text) * 6, y0 + 12), "text": text, "size": 12.0, "bold": False}


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
