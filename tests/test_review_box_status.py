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
from corpus.review.review import AcceptRequest, accept_node, accept_page

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
REVIEW_CSS = Path(__file__).resolve().parent.parent / "static" / "admin" / "review.css"


def _review_page() -> str:
    """The page with its stylesheet: the rules these tests read live in
    static/admin/review.css."""
    return REVIEW_HTML.read_text(encoding="utf-8") + REVIEW_CSS.read_text(encoding="utf-8")


def test_deciding_a_piece_repaints_its_box():
    """Repainted in acceptPiece rather than in applyPieceDecision, which
    only patches the panel and does nothing when the provision decided is
    not in the unit being read -- which a box on the page often is not."""
    page = _review_page()
    body = re.search(r"async function acceptPiece\(nodeIndex, flagged\) \{(.*?)\n\}", page, re.S).group(1)
    assert "setBoxStatus(result.node_index, result.status)" in body


def test_deciding_a_whole_unit_refreshes_the_boxes():
    page = _review_page()
    body = re.search(r"async function decideUnit\(flagged\) \{(.*?)\n\}", page, re.S).group(1)
    assert "loadPageBoxes()" in body


def test_every_status_the_server_can_report_has_a_colour():
    """A status with no rule of its own would render as whatever the
    previous one left behind."""
    page = _review_page()
    coloured = set(re.findall(r'\.box-group\[data-status="(\w+)"\]', page))
    assert {"pending", "accepted", "flagged"} <= coloured


# --- deciding a whole page ----------------------------------------------

@pytest.fixture
def one_page(one_section, monkeypatch):
    """The same Section, printed on page 7, with a stub for the PDF so
    the endpoint's page-range check has something to check against."""
    monkeypatch.setattr(review, "_rects_for",
                        lambda i: [{"page": 7, "x0": 0, "y0": i * 10, "x1": 100, "y1": i * 10 + 8}])
    monkeypatch.setattr(review, "_get_pdf_doc", lambda: type("Doc", (), {"page_count": 20})())
    monkeypatch.setattr(review, "_node_is_live", lambda i: True)
    return one_section


def test_accepting_a_page_decides_everything_outstanding_on_it(one_page):
    result = accept_page(7, AcceptRequest(flagged=False))
    assert result["decided"] == 3
    assert all(review._piece_status(i) == "accepted" for i in range(3))


def test_it_says_what_every_box_on_the_page_should_now_be(one_page):
    result = accept_page(7, AcceptRequest(flagged=False))
    assert result["statuses"] == {"0": "accepted", "1": "accepted", "2": "accepted"}


def test_a_piece_already_accepted_is_left_alone(one_page):
    accept_node(1, AcceptRequest(flagged=False))
    decided_at = review._verified_by_source_index[1]["verified_at"]
    result = accept_page(7, AcceptRequest(flagged=False))
    assert result["decided"] == 2
    assert review._verified_by_source_index[1]["verified_at"] == decided_at


def test_accepting_a_page_clears_a_flag_raised_earlier(one_page):
    accept_node(1, AcceptRequest(flagged=True))
    assert review._piece_status(1) == "flagged"
    accept_page(7, AcceptRequest(flagged=False))
    assert review._piece_status(1) == "accepted"


def test_a_page_with_nothing_outstanding_says_so(one_page):
    accept_page(7, AcceptRequest(flagged=False))
    with pytest.raises(Exception) as raised:
        accept_page(7, AcceptRequest(flagged=False))
    assert "already been accepted" in str(raised.value)


def test_a_page_the_pdf_does_not_have_is_a_404(one_page):
    with pytest.raises(Exception) as raised:
        accept_page(99, AcceptRequest(flagged=False))
    assert "no page 99" in str(raised.value)


def test_a_provision_printed_elsewhere_is_untouched(one_page, monkeypatch):
    # Piece 2 is printed on page 8, so a page-7 accept must not reach it.
    monkeypatch.setattr(review, "_rects_for",
                        lambda i: [{"page": 8 if i == 2 else 7, "x0": 0, "y0": 0, "x1": 9, "y1": 9}])
    result = accept_page(7, AcceptRequest(flagged=False))
    assert result["decided"] == 2
    assert review._piece_status(2) == "pending"


# --- what the page does with it -----------------------------------------

def test_the_toolbar_offers_the_page_button():
    page = _review_page()
    assert 'id="pdf-accept-page-btn"' in page
    assert 'document.getElementById("pdf-accept-page-btn").onclick = acceptPage;' in page


def test_accepting_a_page_repaints_every_box_it_reports():
    page = _review_page()
    body = re.search(r"async function acceptPage\(\) \{(.*?)\n\}", page, re.S).group(1)
    assert "result.statuses" in body and "setBoxStatus" in body
    assert "result.units" in body and "updateUnitRowStatus" in body


def test_each_box_carries_an_accept_and_a_flag():
    page = _review_page()
    body = re.search(r"function boxActions\(box, rect\) \{(.*?)\n\}\n", page, re.S).group(1)
    assert "acceptPiece(box.node_index, false)" in body
    assert "acceptPiece(box.node_index, true)" in body


def test_a_decided_box_shows_its_verdict_instead_of_buttons():
    page = _review_page()
    body = re.search(r"function boxActions\(box, rect\) \{(.*?)\n\}\n", page, re.S).group(1)
    assert 'box.status !== "pending"' in body
    assert "box-verdict" in body


def test_a_decision_from_the_page_updates_the_unit_it_belongs_to():
    """Not the unit open in the panel: a box on the page can be any
    provision printed on it."""
    page = _review_page()
    body = re.search(r"async function acceptPiece\(nodeIndex, flagged\) \{(.*?)\n\}", page, re.S).group(1)
    assert "updateUnitRowStatus(result.unit_no, result.unit_status)" in body


def test_the_tool_opens_with_the_page_showing():
    page = _review_page()
    assert 'localStorage.getItem("reviewViewMode") || "split"' in page


# --- serving it without letting it change anything -----------------------

def test_a_read_only_server_refuses_a_write(monkeypatch):
    """Driving the real interface against the real corpus is the only
    way to see that it works, and doing it without this wrote decisions
    nobody made into a reviewer's own review."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(review, "_READ_ONLY", True)
    response = TestClient(review.app).post("/api/nodes/1/accept", json={"flagged": False})
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


def test_a_read_only_server_still_serves_the_page(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(review, "_READ_ONLY", True)
    assert TestClient(review.app).get("/").status_code == 200


def test_writes_are_allowed_when_it_is_not_read_only(one_section):
    """The guard is off by default -- the tool's whole job is writing."""
    from fastapi.testclient import TestClient

    assert review._READ_ONLY is False
    response = TestClient(review.app).post("/api/nodes/1/accept", json={"flagged": False})
    assert response.status_code != 403


# --- pointing at a box, and choosing one --------------------------------
# Hover and selection used to share one variable with a "sticky" flag, and
# the first click set it. Every hover after that was discarded, so for the
# rest of the session a provision's label could only be brought up by
# clicking it -- and hovering a piece in the panel stopped lighting its
# box. Two states, so a choice outlasts the pointer without silencing it.

def test_hover_and_selection_are_no_longer_the_same_thing():
    page = _review_page()

    assert "boxIsSticky" not in page, "the sticky flag is what swallowed every hover"
    assert "let selectedNodeIndex" in page and "let hoverNodeIndex" in page


def test_a_box_says_what_it_is_when_pointed_at_not_only_when_clicked():
    """The label is drawn to the left of the box; what was missing was
    anything revealing it on hover once something had been clicked."""
    page = _review_page()

    assert re.search(r"\.box-hot \.box-label[^{]*\{[^}]*opacity: 1", page)


def test_the_hover_style_is_actually_reachable_now():
    """`.box-hot` sat in the stylesheet with nothing in the JS setting it."""
    page = _review_page()

    assert 'classList.toggle("box-hot"' in page


def test_the_tick_and_flag_are_on_every_box():
    """They were opacity: 0 until a box was selected, so there was no way
    to see from the page which provisions could be decided from it."""
    page = _review_page()
    rule = re.search(r"\n\s*\.box-action \{([^}]*)\}", page).group(1)

    assert "opacity: 0;" not in rule, "still hidden until selected"
    assert "pointer-events: auto" in rule, "visible but not clickable is worse than hidden"


def test_but_held_back_until_the_box_is_pointed_at():
    """Two buttons beside every provision at full contrast is a margin
    louder than the Act."""
    page = _review_page()
    resting = float(re.search(r"\n\s*\.box-action \{[^}]*opacity: ([\d.]+)", page).group(1))

    assert 0 < resting < 1
    assert re.search(r"\.box-(hot|selected) \.box-action[^{]*\{[^}]*opacity: 1", page)


def test_choosing_a_piece_shows_its_box_on_the_page():
    """The reverse of clicking a box, which has always chosen the piece."""
    page = _review_page()
    body = re.search(r"function selectPieceOnPage\(piece\) \{(.*?)\n\}", page, re.S).group(1)

    assert "setSelected(piece.node_index)" in body
    assert "jumpToPiecePage(piece.page_start)" in body, "a box on another page cannot be shown without turning to it"


def test_selecting_text_in_a_piece_is_not_a_click_on_it():
    """A piece's text is drag-selected to mark a correction (the #pieces
    mouseup handler). Treating that as "show me this box" would turn every
    correction into a page turn."""
    page = _review_page()
    handler = re.search(r'div\.addEventListener\("click", \(e\) => \{(.*?)\n    \}\);', page, re.S).group(1)

    assert "getSelection" in handler and "isCollapsed" in handler
    assert "button" in handler, "a piece's own controls have their own jobs"
