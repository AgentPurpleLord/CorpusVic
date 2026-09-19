"""Tests for review.py's edit endpoint, and specifically for what a type
change does to the numbering around it.

Separate from tests/test_review.py, which covers that module's pure logic
and says so: the bug these exist for (issue #51) was that the endpoint
did not *call* the correct logic, so testing the logic on its own would
have passed throughout. That means driving the endpoint, which means
standing up review.py's per-process state -- one document, its units, its
hierarchy -- as _load_state would.

The fixture is section 97 of the Criminal Procedure Act, as the parser
actually reads it today, because that is the section the bug was reported
against and it carries both halves of it.
"""
import pytest

import review
from review import EditRequest, compute_unit_labels, edit_node_endpoint

HIERARCHY = ["chapter", "part", "division", "subdivision", "section",
             "subsection", "paragraph", "subparagraph", "sub_subparagraph"]


def _node(type_, number=None, text="", **extra):
    return {"type": type_, "number": number, "heading": None, "text": text, **extra}


# Criminal Procedure Act s 97, as parsed. Two things are wrong in it, and
# both are a reviewer's to correct:
#
#   (c) was read as a subparagraph, so it hangs under (a) and reads
#       "(a)(c)" -- the example in issue #51;
#   (iv) was read as a paragraph, because "iv" is also a run of plain
#       letters, so it closed (d)'s roman list and started a new one --
#       which is why (v) after it reads "(iv)(v)".
#
# The repealed "* * * * *" markers are where (b) and (iii) used to be,
# and are what breaks the run of numbering in both places.
SECTION_97 = [
    _node("section", "97", "The purposes of a committal proceeding are—"),
    _node("paragraph", "a", "to determine whether a charge is appropriate…"),
    _node("repealed", None, "* * * * *"),
    _node("subparagraph", "c", "to determine how the accused proposes to plead…"),
    _node("paragraph", "d", "to ensure a fair trial, if the matter proceeds…"),
    _node("subparagraph", "i", "ensuring that the prosecution case is adequate…"),
    _node("subparagraph", "ii", "enabling the accused to hear or read the evidence…"),
    _node("repealed", None, "* * * * *"),
    _node("paragraph", "iv", "enabling the accused to adequately prepare…"),
    _node("subparagraph", "v", "enabling the issues in contention to be adequate…"),
]


@pytest.fixture
def section_97(tmp_path, monkeypatch):
    """review.py's state for one Section, set up as _load_state would.

    Nothing here is committed, so every edit lands in _pending_edits and
    nothing touches the database -- but chdir anyway, so that a test that
    grows a committed row later cannot write into the real one."""
    monkeypatch.chdir(tmp_path)
    from corpus import tree

    nodes = [dict(n) for n in SECTION_97]
    # The paths a parse would have stamped, by the same code that stamps
    # them -- so the fixture starts from what the parser really produces
    # rather than from what this test would like it to have produced.
    tree.annotate_paths(nodes, HIERARCHY)

    monkeypatch.setattr(review, "_act", "criminal-procedure-act")
    monkeypatch.setattr(review, "_nodes", nodes)
    monkeypatch.setattr(review, "_structure_edits", {})
    monkeypatch.setattr(review, "_hierarchy", HIERARCHY)
    monkeypatch.setattr(review, "_relabel_types", [*HIERARCHY, "repealed", "note", "definition"])
    monkeypatch.setattr(review, "_units", [list(range(len(nodes)))])
    monkeypatch.setattr(review, "_unit_of_index", {i: 0 for i in range(len(nodes))})
    monkeypatch.setattr(review, "_verified", [])
    monkeypatch.setattr(review, "_verified_by_source_index", {})
    monkeypatch.setattr(review, "_pending_edits", {})
    monkeypatch.setattr(review, "_merged_away", set())
    return nodes


def _labels():
    """What the review panel prints beside each piece."""
    unit = [review._current_node(i) for i in review._units[0]]
    return compute_unit_labels(unit)


def _edit(index, **fields):
    node = review._current_node(index)
    return edit_node_endpoint(index, EditRequest(
        type=fields.get("type", node["type"]),
        number=fields.get("number", node.get("number")),
        heading=fields.get("heading", node.get("heading")),
        text=fields.get("text", node.get("text") or ""),
    ))


def test_the_parse_starts_out_wrong_in_the_two_reported_ways(section_97):
    """Not a test of the fix -- a statement of what is being fixed. If
    the parser is ever corrected so this fails, these tests are measuring
    a section that no longer has the problem and should be re-pointed."""
    labels = _labels()

    assert labels[3] == "(a)(c)", "the subparagraph reported in issue #51"
    assert labels[8] == "(iv)"
    assert labels[9] == "(iv)(v)", "why (v) reads as (iv)"


def test_correcting_a_subparagraph_to_a_paragraph_renumbers_it(section_97):
    """Issue #51. (c) was read as a subparagraph, so it hung under (a)
    and read "(a)(c)"; corrected to a paragraph it is (c), full stop."""
    _edit(3, type="paragraph")

    assert _labels()[3] == "(c)"


def test_the_pieces_after_it_are_renumbered_too(section_97):
    """A path is inherited, so a type change is never only about the
    piece that was changed. Correcting (iv) to a subparagraph has to put
    (v) back under (d) as well -- which is the half of this the reporter
    hit second."""
    _edit(8, type="subparagraph")

    labels = _labels()
    assert labels[8] == "(d)(iv)"
    assert labels[9] == "(d)(v)", "(v) reads as itself again"


def test_the_whole_section_comes_right(section_97):
    """Both corrections together, which is what a reviewer would do."""
    _edit(3, type="paragraph")
    _edit(8, type="subparagraph")

    assert _labels() == [
        "SECTION", "(a)", "[repealed 1]", "(c)", "(d)",
        "(d)(i)", "(d)(ii)", "[repealed 2]", "(d)(iv)", "(d)(v)",
    ]


def test_changing_a_number_renumbers_what_is_nested_under_it(section_97):
    """The other half of a path: (d)'s own number is carried by every
    piece beneath it, so correcting it has to reach them."""
    _edit(4, number="e")

    labels = _labels()
    assert labels[4] == "(e)"
    assert labels[5] == "(e)(i)"


def test_the_endpoint_says_where_the_piece_ended_up(section_97):
    """The reply carries the recomputed path, as renest's does. The panel
    reloads the unit either way, but a reply that described the old
    position would be wrong on its face."""
    result = _edit(3, type="paragraph")

    assert result["path"]["paragraph"] == "c"
    assert result["path"]["subparagraph"] is None


def test_editing_only_the_text_leaves_the_numbering_alone(section_97):
    """The recompute is unconditional, so this is worth pinning: a
    correction to the words is not a correction to the structure."""
    before = _labels()

    _edit(6, text="enabling the accused to hear or read the evidence in full")

    assert _labels() == before
