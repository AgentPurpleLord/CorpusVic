"""Putting a piece where a reference says it belongs, and making a piece
from a box drawn over nothing.

The shape is Criminal Procedure Act s 110(1): the parser read (vii) as a
paragraph, so it labelled as "(1)(vii)" and took (viii) with it, and a
continuation under (vi) sits between them."""
import json

import pytest

from corpus.parsing.extract import BodyLine
from corpus.parsing.identity import annotate_ids
from corpus.review import review
from corpus.storage import db
from corpus.review.review import (
    AcceptRequest, BoxCreateRequest, BoxRequest, PlaceRequest, accept_unit, create_from_box_endpoint,
    place_node_endpoint, read_new_box_endpoint,
)

HIERARCHY = ["schedule", "chapter", "part", "division", "subdivision", "section",
             "subsection", "paragraph", "subparagraph", "sub_subparagraph"]


def _n(type_, number, text, **extra):
    return {"type": type_, "number": number, "heading": None, "text": text, "page_start": 1, "page_end": 1, **extra}


@pytest.fixture
def s110(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nodes = [
        {**_n("section", "110", ""), "heading": "Hand-up brief"},
        _n("subsection", "1", "A hand-up brief must contain—"),
        _n("paragraph", "d", "any information—"),
        _n("subparagraph", "v", "if a person has been examined; and"),
        _n("subparagraph", "vi", "if the proceeding relates to—"),
        _n("sub_subparagraph", "A", "a sexual offence; or"),
        _n("sub_subparagraph", "B", "an assault—"),
        _n("continuation", None, "a transcript of any recording; and", depth_rank=9),
        _n("paragraph", "vii", "a legible copy of any document; and"),
        _n("subparagraph", "viii", "a list of exhibits; and"),
        _n("paragraph", "e", "any other information—"),
        _n("subparagraph", "i", "a list of the persons; and"),
        _n("subparagraph", "ii", "a copy of every statement."),
    ]
    from corpus.parsing import tree
    tree.annotate_paths(nodes, HIERARCHY)
    annotate_ids(nodes, HIERARCHY)
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True)
    (parsed / "act.json").write_text(json.dumps({"nodes": nodes, "hierarchy": HIERARCHY, "fingerprint": "fp"}))
    db.save_parse_fingerprint("act", "fp")
    review._load_state("act")
    return nodes


def _labels():
    return {p["node_index"]: p["label"] for p in review.get_unit(0)["pieces"]}


def test_a_piece_read_at_the_wrong_level_is_put_right_by_its_reference(s110):
    result = place_node_endpoint(8, PlaceRequest(reference="(1)(d)(vii)"))

    assert result["matches"] and not result["moved"] and result["type"] == "subparagraph"
    labels = _labels()
    assert labels[8] == "(1)(d)(vii)"
    # Only under (vii) because (vii) had the wrong type: now its sibling.
    assert labels[9] == "(1)(d)(viii)"
    assert labels[10] == "(1)(e)"


def test_history_review_places_a_piece_by_its_name(s110):
    """History review knows the piece by name, not by this server's
    position for it."""
    result = review.place_named(s110[8]["id"], PlaceRequest(reference="(1)(d)(vii)"))

    assert result["matches"] and _labels()[8] == "(1)(d)(vii)"
    with pytest.raises(review.HTTPException):
        review.place_named("no-such-piece", PlaceRequest(reference="(1)"))


def test_an_accepted_continuation_keeps_the_place_the_parser_gave_it(s110):
    """A reviewed row has no column for depth_rank; without the parse's,
    the continuation reset the section and every label after it lost its
    (1)(d)."""
    accept_unit(0, AcceptRequest(flagged=False))

    place_node_endpoint(8, PlaceRequest(reference="(1)(d)(vii)"))

    assert _labels()[7] == "(1)(d)(vi) continuation"
    assert _labels()[8] == "(1)(d)(vii)"


def test_a_piece_moves_with_what_is_nested_under_it(s110):
    result = place_node_endpoint(4, PlaceRequest(reference="(1)(e)(iii)"))

    assert result["moved"] and result["matches"]
    pieces = [p["node_index"] for p in review.get_unit(0)["pieces"]]
    assert pieces[-4:] == [4, 5, 6, 7], "(vi), its (A) and (B) and its continuation, at the end of (e)"
    labels = _labels()
    assert (labels[4], labels[5]) == ("(1)(e)(iii)", "(1)(e)(iii)(A)")


def test_it_goes_after_the_piece_asked_for(s110):
    place_node_endpoint(12, PlaceRequest(reference="(1)(e)(ii)", after_node_index=10))

    pieces = [p["node_index"] for p in review.get_unit(0)["pieces"]]
    assert pieces.index(12) == pieces.index(10) + 1


def test_a_reference_to_nothing_in_the_section_is_refused(s110):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        place_node_endpoint(8, PlaceRequest(reference="(1)(z)(vii)"))
    assert "(1)(z)" in e.value.detail
    with pytest.raises(HTTPException):
        place_node_endpoint(8, PlaceRequest(reference="1 d vii"))


# --- a piece from a box drawn over nothing -------------------------------

def _printed(monkeypatch):
    def line(text, y0):
        return BodyLine(text=text, x0=100.0, x1=400.0, y0=y0, y1=y0 + 10.0, page_no=1, size=10.0, bold=False)
    monkeypatch.setattr(review, "_printed_lines", lambda: [
        line("(ix) a clear photograph of any", 300.0),
        line("proposed exhibit; and", 312.0),
    ])
    # The piece printed just above where the box is drawn.
    review._node_rects[9] = [{"page": 1, "x0": 100.0, "y0": 280.0, "x1": 400.0, "y1": 292.0}]


BOX = {"page": 1, "x0": 90.0, "y0": 298.0, "x1": 410.0, "y1": 325.0}


def test_a_drawn_box_is_read_and_its_type_and_place_guessed(s110, monkeypatch):
    _printed(monkeypatch)

    guess = read_new_box_endpoint(BoxRequest(rect=BOX))

    assert guess["text"] == "(ix) a clear photograph of any proposed exhibit; and"
    assert (guess["type"], guess["number"]) == ("subparagraph", "ix")
    assert guess["after_node_index"] == 9


def test_confirming_a_box_makes_a_piece_read_from_it(s110, monkeypatch):
    _printed(monkeypatch)

    made = create_from_box_endpoint(BoxCreateRequest(rect=BOX, type="subparagraph", number="ix", after_node_index=9))

    node = review._current_node(made["node_index"])
    assert node["text"] == "a clear photograph of any proposed exhibit; and"
    assert node["source"] == "drawn-in-review"
    assert review._rects_for(made["node_index"]) == [BOX]
    pieces = [p["node_index"] for p in review.get_unit(0)["pieces"]]
    assert pieces.index(made["node_index"]) == pieces.index(9) + 1


def test_the_edit_window_and_the_page_offer_both():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")

    assert 'id="edit-reference"' in page and "api/nodes/${nodeIndex}/place" in page
    assert 'id="pdf-newbox-btn"' in page and 'beginDraw(null, "new")' in page
    assert '"api/boxes/read"' in page and '"api/boxes/create"' in page
