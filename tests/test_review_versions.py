"""The review tool on one version of a work held in several: only what
differs from the version it is compared with is left to review."""
import json

import pytest

from corpus.parsing.identity import annotate_ids
from corpus.review import review
from corpus.review.review import AcceptRequest, accept_page
from corpus.storage import db

HIERARCHY = ["part", "section", "subsection"]


def _parse(tmp_path, slug, version, sections):
    nodes = [{"type": "part", "number": "1", "heading": "Preliminary", "text": ""}]
    for number, text in sections:
        nodes.append({"type": "section", "number": number, "heading": f"Heading {number}", "text": ""})
        nodes.append({"type": "subsection", "number": "1", "heading": None, "text": text})
    annotate_ids(nodes, HIERARCHY)
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / f"{slug}.json").write_text(json.dumps({
        "nodes": nodes, "hierarchy": HIERARCHY, "fingerprint": f"fp-{slug}",
        "version": {"version": version, "as_at_printed": f"{version} May 2026"},
    }))
    return nodes


@pytest.fixture
def two_versions(tmp_path, monkeypatch):
    """Version 2 is current and fully reviewed, with a typo fixed in s 1;
    version 1 differs from it only in s 2."""
    monkeypatch.chdir(tmp_path)
    _parse(tmp_path, "act-v1", 1, [("1", "the acused"), ("2", "old words")])
    current = _parse(tmp_path, "act-v2", 2, [("1", "the acused"), ("2", "new words")])
    rows = []
    for i, node in enumerate(current):
        row = {**node, "_node_id": node["id"], "_source_node_index": i, "verified_at": "2026-05-01"}
        if node["type"] == "subsection" and node["text"] == "the acused":
            row["text"] = "the accused"
        rows.append(row)
    rows[0]["_unit_end_index"], rows[2]["_unit_end_index"], rows[4]["_unit_end_index"] = 0, 1, 2
    db.save_verified("act-v2", rows)
    db.save_parse_fingerprint("act-v2", "fp-act-v2")
    review._load_state("act-v1")
    return tmp_path


def _unit(number):
    return next(u for u in review.get_meta()["units"] if u["number"] == number)


def test_an_unchanged_section_is_vouched_for_by_the_reviewed_version(two_versions):
    assert _unit("1")["status"] == "inherited"
    assert review.get_unit(_unit("1")["unit_no"])["lineage"]["source"] == 2


def test_only_the_changed_section_is_left_to_review(two_versions):
    info = review.get_meta()["version_info"]

    assert _unit("2")["status"] == "pending"
    assert info["to_review"] == 1 and info["reference"] == 2


def test_a_changed_section_shows_how_its_words_differ(two_versions):
    compare = review.get_unit(_unit("2")["unit_no"])["lineage"]["compare"]

    assert '<del class="d-del">old</del>' in compare
    assert '<ins class="d-ins">new</ins>' in compare


def test_the_browse_view_reads_an_inherited_section_as_reviewed(two_versions):
    from corpus.review import inheritance

    state = inheritance.work_review("act-v1")
    nodes = inheritance.effective_nodes("act-v1", state, 1)

    assert [n["text"] for n in nodes if n["type"] == "subsection"] == ["the accused", "old words"]


def test_a_lent_piece_is_drawn_as_such_and_left_out_of_accepting_a_page(two_versions, monkeypatch):
    unit = _unit("1")["unit_no"]
    lent = [i for i in review._units[unit]]
    assert {review._piece_status(i) for i in lent} == {"inherited"}

    class _Doc:
        page_count = 1
    monkeypatch.setattr(review, "_get_pdf_doc", lambda: _Doc())
    monkeypatch.setattr(review, "_indices_on_page", lambda page: lent)
    with pytest.raises(Exception, match="already been accepted"):
        accept_page(1, AcceptRequest(flagged=False))


def test_a_document_in_one_version_has_no_version_bar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _parse(tmp_path, "act-v1", 1, [("1", "words")])
    review._load_state("act-v1")

    assert review.get_meta()["version_info"] is None
    assert _unit("1")["status"] == "pending"


def test_a_moved_provision_can_be_recorded_as_carried_from_the_version_before(tmp_path, monkeypatch):
    from corpus.review.review import CarriedFromRequest, carried_from_endpoint

    monkeypatch.chdir(tmp_path)
    _parse(tmp_path, "act-v1", 1, [("1", "a"), ("2", "moved words")])
    _parse(tmp_path, "act-v2", 2, [("1", "a"), ("3", "moved words")])
    review._load_state("act-v2")
    unit = _unit("3")["unit_no"]
    candidates = review.get_unit(unit)["lineage"]["carry_candidates"]
    assert [c["label"] for c in candidates] == ["Section 2"]

    payload = carried_from_endpoint(unit, CarriedFromRequest(key=candidates[0]["key"]))

    assert payload["lineage"]["carried_from"] == candidates[0]["key"]
    assert db.load_provision_links("act-v2") == {("provision", None, "3"): ("provision", None, "2")}
