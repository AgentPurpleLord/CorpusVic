"""Linking an Explanatory Memorandum's clause notes to the Act provisions
they explain.

run_bill_linking guesses each link into data/bill_links/; nobody could
confirm or correct one. A reviewer now settles them in the EM's review,
and what they settle is what the Act's "Explained in" chips are built
from.
"""
import json

import pytest
from fastapi import HTTPException

from corpus.domain import commentary
from corpus.review import review

BILL_DOC = {"bill_slug": "x-bill", "act_slug": "x-act-v1", "links": [
    {"clause_number": "1", "schedule": None, "act_section_number": "1", "act_schedule": None, "status": "matched"},
    {"clause_number": "2", "schedule": None, "act_section_number": "3", "act_schedule": None, "status": "matched"},
]}
EM_DOC = {"em_slug": "x-bill-em", "bill_slug": "x-bill", "act_slug": "x-act-v1", "links": [
    {"em_node_index": 0, "clause_number": "1", "schedule": None,
     "target": {"kind": "bill_clause", "act_slug": "x-act-v1", "clause_number": "1", "schedule": None}},
    {"em_node_index": 1, "clause_number": "2", "schedule": None,
     "target": {"kind": "bill_clause", "act_slug": "x-act-v1", "clause_number": "2", "schedule": None}},
]}


def test_each_note_is_offered_the_provision_its_clause_became():
    suggestions = commentary.em_link_suggestions("x-bill-em", [BILL_DOC], [EM_DOC])
    assert suggestions[0]["section"] == "1" and suggestions[1]["section"] == "3"
    assert suggestions[1]["act"] == "x-act", "the work, so the link holds for every reprint"


def test_another_acts_section_is_not_offered_as_this_acts():
    em_doc = {**EM_DOC, "links": [{"em_node_index": 5, "clause_number": "3", "schedule": None, "target": {
        "kind": "act_section", "act_slug": "juries-act", "act_title": "Juries Act 2000", "section_ref": "22"}}]}
    suggestion = commentary.em_link_suggestions("x-bill-em", [BILL_DOC], [em_doc])[5]
    assert suggestion["section"] is None and suggestion["act_title"] == "Juries Act 2000"


def test_a_settled_link_replaces_the_guess_behind_the_chips():
    reviewed = {"x-bill-em": {
        1: {"target": "x-act", "schedule": None, "section": "4", "clause_number": "2"},
        0: {"target": "x-act", "schedule": None, "section": None, "clause_number": "1"},
    }}
    index = commentary.build_commentary_index("x-act-v2", [BILL_DOC],
                                              commentary.with_reviewed_links([EM_DOC], reviewed))
    em_on = lambda n: [e["em_node_index"] for e in (index.get(commentary.provision_key(None, n)) or {}).get("em", [])]
    assert em_on("4") == [1]
    assert em_on("3") == [], "the guess it replaced is gone"
    assert em_on("1") == [], "a note settled as explaining nothing has no chip"


@pytest.fixture
def em_review(tmp_path, monkeypatch):
    """review.py serving an EM of two clause notes, with the Bill linked
    to a parsed Act of three sections."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "bill_links").mkdir(parents=True)
    (tmp_path / "data" / "bill_links" / "x-bill-to-x-act-v1.json").write_text(json.dumps(BILL_DOC))
    (tmp_path / "data" / "bill_links" / "x-bill-em-links.json").write_text(json.dumps(EM_DOC))
    (tmp_path / "data" / "parsed").mkdir()
    (tmp_path / "data" / "parsed" / "x-act-v1.json").write_text(json.dumps({"nodes": [
        {"type": "section", "number": n, "heading": f"Heading {n}", "text": ""} for n in ("1", "3", "4")]}))

    nodes = [{"type": "clause", "number": n, "heading": None, "text": f"Clause {n} does a thing.", "id": f"cl{n}"}
             for n in ("1", "2")]
    monkeypatch.setattr(review, "BASE_DIR", tmp_path)
    monkeypatch.setattr(review, "_act", "x-bill-em")
    monkeypatch.setattr(review, "_document_type", "em")
    monkeypatch.setattr(review, "_nodes", nodes)
    monkeypatch.setattr(review, "_node_ids", {i: n["id"] for i, n in enumerate(nodes)})
    monkeypatch.setattr(review, "_structure_edits", {})
    monkeypatch.setattr(review, "_pending_edits", {})
    monkeypatch.setattr(review, "_verified_by_source_index", {})
    monkeypatch.setattr(review, "_merged_away", set())
    monkeypatch.setattr(review, "_node_is_live", lambda i: True)
    review._em_link_context.clear()
    yield
    review._em_link_context.clear()


def test_a_note_starts_on_the_guess(em_review):
    links = review._em_link_payload([0, 1])
    assert links[1] == {"suggested": {"act": "x-act", "act_title": None, "schedule": None, "section": "3"},
                        "settled": None}
    assert [s["section"] for s in review.em_link_sections()["sections"]] == ["1", "3", "4"]


def test_changing_a_link_is_remembered_and_undone(em_review):
    settled = review.settle_em_link(1, review.EmLinkRequest(section="4"))["settled"]
    assert settled["section"] == "4" and settled["target"] == "x-act"
    assert review._em_link_payload([1])[1]["settled"]["section"] == "4"

    assert review.settle_em_link(1, review.EmLinkRequest(section=""))["settled"]["section"] is None

    assert review.clear_em_link(1)["settled"] is None


def test_a_provision_the_act_does_not_have_is_refused(em_review):
    with pytest.raises(HTTPException) as refused:
        review.settle_em_link(1, review.EmLinkRequest(section="99"))
    assert refused.value.status_code == 400


def test_only_an_em_offers_links(em_review, monkeypatch):
    monkeypatch.setattr(review, "_document_type", "act")
    assert review._em_link_payload([0, 1]) == {}
