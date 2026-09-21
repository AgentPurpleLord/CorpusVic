"""Tests for reading a provision out of the box drawn over it.

Two halves, and the seam between them is the point. Geometry decides
*which* printed lines a box covers (extract.lines_in_rects,
extract.text_in_rects); the provision itself decides what those words
then mean for it (rule_parser.read_box) -- a table's words are columns, a
Part's are a heading, and a paragraph's arrive with the "(c)" the page
prints and the node does not store.

The coordinates are the real ones off Criminal Procedure Act s 6, page
44, because the whole of this is about where things sit.
"""
import pytest

from corpus.parsing.extract import lines_in_rects, text_in_rects
from corpus.domain.profiles import load_profile
from corpus.parsing.rule_parser import read_box

from conftest import line


def _at(text, x0, y0, page=1, width=200.0, bold=False, size=12.0):
    return line(text, x0=x0, y0=y0, x1=x0 + width, page_no=page, bold=bold, size=size)


def _rect(x0, y0, x1, y1, page=1):
    return {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _s6_page():
    """Paragraph (c), then the Notes block set below it two points
    smaller -- the shape that made this worth building."""
    return [
        _at("(c) if a summons is issued under section 14, at the", 216.2, 349.6, width=228),
        _at("time the charge-sheet is signed.", 216.2, 361.6, width=150),
        _at("Notes", 190.1, 393.0, bold=True, size=10.0),
        _at("1 A criminal proceeding against a child is commenced", 199.0, 405.0, width=240, size=10.0),
    ]


# ---------------------------------------------------------------------
# Which lines a box covers
# ---------------------------------------------------------------------

def test_a_box_covers_the_lines_inside_it():
    lines = _s6_page()
    assert [l.text for l in lines_in_rects(lines, [_rect(216, 345, 445, 380)])] == [
        "(c) if a summons is issued under section 14, at the",
        "time the charge-sheet is signed.",
    ]


def test_a_box_drawn_too_far_down_pulls_in_the_note():
    """The reviewer's mistake this is meant to make visible: a box is
    exactly as right as it is drawn, and reading one that overshoots has
    to give the overshoot back rather than quietly stopping at the
    provision the box belongs to."""
    lines = _s6_page()
    covered = lines_in_rects(lines, [_rect(190, 345, 445, 410)])
    assert [l.text for l in covered][-1].startswith("1 A criminal proceeding")


def test_a_line_clipped_at_top_or_bottom_still_counts():
    """A box drawn by hand shaves a descender or an ascender. The test is
    on the line's middle, so it doesn't matter."""
    lines = _s6_page()
    # Top and bottom both inside the two lines' own extents.
    covered = lines_in_rects(lines, [_rect(216, 352, 445, 368)])
    assert len(covered) == 2


def test_a_line_mostly_outside_the_box_is_left_out():
    """The page has a body column and a margin, and a box over one must
    never pull in what is printed beside it. Half a line is the test: it
    is never ambiguous about which column a line is in."""
    lines = [
        _at("s. 6", 60.0, 349.6, width=30),          # margin
        _at("(c) if a summons is issued", 216.2, 349.6, width=228),
    ]
    covered = lines_in_rects(lines, [_rect(200, 345, 445, 380)])
    assert [l.text for l in covered] == ["(c) if a summons is issued"]


def test_a_box_on_another_page_covers_nothing():
    lines = _s6_page()
    assert lines_in_rects(lines, [_rect(0, 0, 600, 800, page=45)]) == []


def test_two_boxes_come_back_in_the_pages_own_order():
    """Several boxes are how a provision that wraps across a column or a
    page break is marked up. Drawn in whatever order the reviewer
    happened to draw them, they still have to read top to bottom."""
    lines = _s6_page()
    later, earlier = _rect(190, 385, 445, 410), _rect(216, 345, 445, 380)
    covered = lines_in_rects(lines, [later, earlier])
    assert [l.text for l in covered][0].startswith("(c) if a summons")
    assert [l.text for l in covered][-1].startswith("1 A criminal proceeding")


def test_a_line_under_two_overlapping_boxes_is_read_once():
    lines = _s6_page()
    both = [_rect(216, 345, 445, 380), _rect(210, 350, 450, 375)]
    assert len(lines_in_rects(lines, both)) == 2


def test_text_in_rects_joins_the_way_the_parser_does():
    """A provision left alone has to read exactly as it did before, so
    the wrap points close up the same way."""
    lines = _s6_page()
    assert text_in_rects(lines, [_rect(216, 345, 445, 380)]) == (
        "(c) if a summons is issued under section 14, at the time the charge-sheet is signed."
    )


def test_text_in_an_empty_box_is_empty():
    assert text_in_rects(_s6_page(), [_rect(216, 200, 445, 240)]) == ""


# ---------------------------------------------------------------------
# What those words mean for the provision
# ---------------------------------------------------------------------

@pytest.fixture
def patterns():
    return load_profile("criminal-procedure-act")


def _boxed(lines, rect):
    return lines_in_rects(lines, [rect])


def test_a_paragraphs_own_marker_comes_off(patterns):
    """The page prints "(c) if a summons...", the node stores the "(c)"
    as its number. Left on, a provision corrected twice would print it
    twice."""
    node = {"type": "paragraph", "number": "c",
            "text": "if a summons is issued under section 14, at the time the charge-sheet is signed."}
    fields = read_box(node, _boxed(_s6_page(), _rect(216, 345, 445, 380)), patterns)
    assert fields == {"text": "if a summons is issued under section 14, at the time the charge-sheet is signed."}


def test_a_box_drawn_too_wide_gives_back_what_it_covers(patterns):
    node = {"type": "paragraph", "number": "c", "text": "if a summons is issued"}
    fields = read_box(node, _boxed(_s6_page(), _rect(190, 345, 445, 410)), patterns)
    assert fields["text"].startswith("if a summons is issued")
    assert "Notes" in fields["text"]


def test_a_heading_only_provision_is_split_into_number_and_heading(patterns):
    """A Part has no text; its words live in `heading`, and the number in
    front of them is split off by the same profile pattern that split it
    at parse time."""
    lines = [_at("Part 2.1—Commencing a criminal proceeding", 161.6, 250.0, bold=True)]
    node = {"type": "part", "number": "2.1", "heading": "Commencing a criminal proceeding", "text": ""}
    assert read_box(node, lines, patterns) == {
        "number": "2.1", "heading": "Commencing a criminal proceeding",
    }


def test_a_heading_that_matches_no_pattern_is_kept_whole(patterns):
    lines = [_at("Offences relating to Horse-drawn Vehicles", 161.6, 250.0, bold=True)]
    node = {"type": "part", "number": "2.1", "heading": "Something else", "text": ""}
    assert read_box(node, lines, patterns) == {
        "heading": "Offences relating to Horse-drawn Vehicles",
    }


def test_a_definitions_term_comes_off_the_front(patterns):
    """A defined term's marker is the term itself, printed bold-italic in
    front of its own definition."""
    lines = [_at("accused means a person who is charged with an offence;", 199.0, 300.0, width=280)]
    node = {"type": "definition", "heading": "accused",
            "text": "means a person who is charged with an offence;"}
    assert read_box(node, lines, patterns) == {
        "text": "means a person who is charged with an offence;",
    }


def test_a_section_loses_its_number_and_heading_together(patterns):
    lines = [
        _at("6 When does a criminal proceeding commence", 161.6, 250.0, bold=True),
        _at("A criminal proceeding commences when a charge-sheet is filed.", 190.1, 268.0, width=280),
    ]
    node = {"type": "section", "number": "6",
            "heading": "When does a criminal proceeding commence",
            "text": "A criminal proceeding commences when a charge-sheet is filed."}
    assert read_box(node, lines, patterns) == {
        "text": "A criminal proceeding commences when a charge-sheet is filed.",
    }


def test_a_table_is_read_as_columns(patterns):
    """A table's meaning is in where its cells sit, so its box is read
    the way the page was read in the first place -- not as a stream of
    lines, which is the thing tables.py exists to undo."""
    lines = [
        _at("Column 1", 222.6, 579.5, width=50), _at("Column 2", 336.0, 579.5, width=50),
        _at("An offence against a", 222.6, 597.2, width=90),
        _at("A defence that would", 336.0, 597.2, width=90),
    ]
    node = {"type": "table", "heading": None, "text": ""}
    fields = read_box(node, lines, patterns)
    assert "Column 1 | Column 2" in fields["text"]
    assert "An offence against a | A defence that would" in fields["text"]


def test_a_box_with_no_table_in_it_says_so(patterns):
    """Drawn round one column instead of both, there is nothing to
    recover -- and silently returning one column as prose would be the
    table bug back again, but invisible."""
    node = {"type": "table", "heading": None, "text": ""}
    lines = [_at("An offence against a", 222.6, 597.2, width=90)]
    with pytest.raises(ValueError, match="No table in that box"):
        read_box(node, lines, patterns)
