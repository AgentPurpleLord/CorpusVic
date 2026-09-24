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


# --- a history note from a box drawn in the margin ------------------------

@pytest.fixture
def margin_note(s110, monkeypatch, tmp_path):
    """A note the parse missed, printed in the margin level with (viii).
    A real page, because the note is read from the PDF itself: the lines
    the parse was built from are the body only."""
    import pymupdf

    _printed(monkeypatch)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((470, 288), "S. 110(1)(d)(viii)", fontsize=6)
    page.insert_text((470, 296), "amended by No. 7/2020 s. 4.", fontsize=6)
    monkeypatch.setattr(review, "_get_pdf_doc", lambda: doc)
    return s110


MARGIN_BOX = {"page": 1, "x0": 465.0, "y0": 280.0, "x1": 590.0, "y1": 300.0}


def test_a_box_over_a_margin_note_is_offered_as_a_history_note(margin_note):
    """It used to be refused as "nothing printed inside that box"."""
    guess = read_new_box_endpoint(BoxRequest(rect=MARGIN_BOX))

    assert guess["kind"] == "note"
    assert guess["text"] == "S. 110(1)(d)(viii) amended by No. 7/2020 s. 4."
    assert guess["node_index"] == 9, "the piece printed level with it"


def test_confirming_it_attaches_the_note_read_as_the_parser_reads_one(margin_note):
    from corpus.review.review import HistoryCreateRequest, create_history_endpoint

    create_history_endpoint(HistoryCreateRequest(rect=MARGIN_BOX, node_index=9))

    [note] = review._current_node(9)["history"]
    assert note["section"] == "110" and note["confidence"] == "manual"
    assert note["rect"] == MARGIN_BOX and note["page"] == 1
    with pytest.raises(review.HTTPException):
        create_history_endpoint(HistoryCreateRequest(rect=MARGIN_BOX, node_index=10))


def test_a_box_over_body_text_is_still_a_piece(s110, monkeypatch):
    _printed(monkeypatch)

    assert read_new_box_endpoint(BoxRequest(rect=BOX))["kind"] == "piece"


def test_the_page_offers_a_margin_box_as_a_note():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")

    assert 'guess.kind === "note"' in page and '"api/history/create"' in page


def test_a_reparse_says_what_it_did_to_the_section(s110, tmp_path, monkeypatch):
    """The boxes were redrawn, or weren't, and nothing said which -- least
    of all that a flag had gone with the section's other decisions."""
    import subprocess

    accept_unit(0, AcceptRequest(flagged=True))
    reread = [dict(n) for n in s110]
    reread[8] = {**reread[8], "type": "subparagraph", "rects": [{"page": 1, "x0": 1, "y0": 2, "x1": 3, "y1": 4}]}

    def parse_again(cmd, **kwargs):
        (tmp_path / "data" / "parsed" / "act.json").write_text(
            json.dumps({"nodes": reread, "hierarchy": HIERARCHY, "fingerprint": "fp"}))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(review, "_source_pdf_path", "act.pdf")
    monkeypatch.setattr(review.subprocess, "run", parse_again)
    changes = review.reparse_unit_endpoint(0)["changes"]

    assert changes["changed"] == 1 and changes["boxes_redrawn"] == 1
    assert changes["pieces_before"] == changes["pieces_after"] == len(s110)
    assert changes["flag_cleared"] and changes["drawn_kept"] == 0


def test_the_page_redraws_the_boxes_after_a_reparse():
    """loadUnit reloads the page only on arriving at another unit, so a
    re-parse of the one in view left the old parse's boxes on it."""
    import re
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")
    handler = re.search(r'getElementById\("reparse-unit-btn"\)\.onclick = async \(\) => \{(.*?)\n\};', page, re.S).group(1)
    assert "loadPdfPage(" in handler and "showReparseNotice(result)" in handler


@pytest.fixture
def item4(tmp_path, monkeypatch):
    """CPA Schedule 2 item 4: sub-items with paragraphs, one read wrongly."""
    monkeypatch.chdir(tmp_path)
    nodes = [
        {**_n("item", "4", ""), "heading": "Indictable offences"},
        _n("subitem", "4.4", "Offences under section 74, if—"),
        _n("paragraph", "a", "the amount does not exceed $100 000; or"),
        _n("subitem", "b", "the property is a motor vehicle."),       # (b), read as a sub-item
        _n("paragraph", "4.13", "Offences under section 88, if—"),    # 4.13, read as a paragraph
        _n("paragraph", "a", "the goods are a motor vehicle; or"),
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


def test_a_schedule_item_is_placed_by_its_sub_item_reference(item4):
    b = place_node_endpoint(3, PlaceRequest(reference="4.4(b)"))
    assert (b["type"], b["reference"], b["matches"]) == ("paragraph", "4.4(b)", True)

    sub = place_node_endpoint(4, PlaceRequest(reference="4.13"))
    assert (sub["type"], sub["reference"], sub["matches"]) == ("subitem", "4.13", True)
    assert _labels()[5] == "4.13(a)", "its own (a) now sits under it"


def test_a_section_has_no_sub_items_to_name(s110):
    with pytest.raises(review.HTTPException, match="names a sub-item"):
        place_node_endpoint(8, PlaceRequest(reference="4.4(a)"))


def test_a_retyped_section_shows_its_new_type_in_the_unit_list(s110):
    """The list was built from the parse, so a retype never reached it."""
    review.edit_node_endpoint(0, review.EditRequest(type="clause", number="110", heading="Hand-up brief", text=""))

    assert review.get_meta()["units"][0]["type"] == "clause"


def test_a_box_is_labelled_with_what_its_piece_is_now(s110):
    """compute_unit_labels calls a unit's own piece SECTION whatever it
    is; on the page that outlived a retype."""
    assert review._box_label("SECTION", {"type": "clause", "number": "110"}) == "CLAUSE 110"
    assert review._box_label("(1)(d)", {"type": "paragraph", "number": "d"}) == "(1)(d)"


def test_the_page_redraws_the_boxes_after_an_edit():
    import re
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")
    body = re.search(r"async function afterMutation\(pdfPage\) \{(.*?)\n\}", page, re.S).group(1)
    assert "loadPageBoxes()" in body
