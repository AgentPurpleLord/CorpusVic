"""The colour of a box on the page, and where that colour comes from.

A piece's status is drawn in four places: its own row in the panel, the
unit footer, the sidebar, and the box over it on the PDF. Accepting a
piece updated three of them, so the box stayed blue over a provision that
had just gone green (issue #53). The fix is that the accept endpoint says
what the piece's status now is, rather than the browser working it out
again from verified_at and needs_followup.
"""
import re
from pathlib import Path

import pytest

from corpus.review import review
from corpus.review.review import AcceptRequest, accept_node

HIERARCHY = ["section", "subsection", "paragraph"]


def _node(type_, number=None, text=""):
    return {"type": type_, "number": number, "heading": None, "text": text}


@pytest.fixture
def one_section(tmp_path, monkeypatch):
    """review.py's state for one Section, as _load_state would leave it."""
    monkeypatch.chdir(tmp_path)
    from corpus.parsing import tree

    nodes = [_node("section", "11", "Place of hearing"),
             _node("subsection", "1", "A proceeding must be heard—"),
             _node("paragraph", "a", "in the place where the offence occurred.")]
    tree.annotate_paths(nodes, HIERARCHY)
    from corpus.parsing.identity import annotate_ids
    annotate_ids(nodes, HIERARCHY)

    monkeypatch.setattr(review, "_act", "test-act")
    monkeypatch.setattr(review, "_nodes", nodes)
    monkeypatch.setattr(review, "_node_ids", {i: n["id"] for i, n in enumerate(nodes)})
    monkeypatch.setattr(review, "_structure_edits", {})
    monkeypatch.setattr(review, "_unplaced_edits", {})
    monkeypatch.setattr(review, "_unplaced_verified", [])
    monkeypatch.setattr(review, "_hierarchy", HIERARCHY)
    monkeypatch.setattr(review, "_units", [list(range(len(nodes)))])
    monkeypatch.setattr(review, "_unit_of_index", {i: 0 for i in range(len(nodes))})
    monkeypatch.setattr(review, "_verified", [])
    monkeypatch.setattr(review, "_verified_by_source_index", {})
    monkeypatch.setattr(review, "_pending_edits", {})
    monkeypatch.setattr(review, "_merged_away", set())
    monkeypatch.setattr(review, "_blind_review_gate_indices", lambda indices: [])
    return nodes


# --- the endpoint says what the box should now be ------------------------

def test_a_piece_starts_pending(one_section):
    assert review._piece_status(1) == "pending"


def test_accepting_says_the_piece_is_accepted(one_section):
    result = accept_node(1, AcceptRequest(flagged=False))
    assert result["status"] == "accepted"


def test_flagging_says_the_piece_is_flagged(one_section):
    result = accept_node(1, AcceptRequest(flagged=True))
    assert result["status"] == "flagged"


def test_the_status_it_reports_is_the_one_the_box_is_drawn_with(one_section):
    """Both come from _piece_status, which is the point -- the browser
    working it out a second time is how the two came to disagree."""
    for flagged in (False, True):
        result = accept_node(1, AcceptRequest(flagged=flagged))
        assert result["status"] == review._piece_status(1)


def test_deciding_one_piece_leaves_the_others_alone(one_section):
    accept_node(1, AcceptRequest(flagged=False))
    assert review._piece_status(0) == "pending"
    assert review._piece_status(2) == "pending"


# --- and the page acts on it --------------------------------------------

REVIEW_HTML = Path(__file__).resolve().parent.parent / "static" / "review.html"


def test_the_panel_repaints_the_box_when_a_piece_is_decided():
    """applyPieceDecision updates the piece row, the footer and the
    sidebar; the box was the one it forgot."""
    page = REVIEW_HTML.read_text(encoding="utf-8")
    body = re.search(r"function applyPieceDecision\(result\) \{(.*?)\n\}", page, re.S).group(1)
    assert "setBoxStatus(result.node_index, result.status)" in body


def test_deciding_a_whole_unit_refreshes_the_boxes():
    page = REVIEW_HTML.read_text(encoding="utf-8")
    body = re.search(r"async function decideUnit\(flagged\) \{(.*?)\n\}", page, re.S).group(1)
    assert "loadPageBoxes()" in body


def test_every_status_the_server_can_report_has_a_colour():
    """A status with no rule of its own would render as whatever the
    previous one left behind."""
    page = REVIEW_HTML.read_text(encoding="utf-8")
    coloured = set(re.findall(r'\.box-group\[data-status="(\w+)"\]', page))
    assert {"pending", "accepted", "flagged"} <= coloured
