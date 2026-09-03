"""Tests for ai_pipeline/commentary.py -- inverting run_bill_linking.py's
per-Bill-clause / per-EM-entry link records into "what explains this Act
section?"."""
from ai_pipeline.commentary import build_commentary_index, section_numbers_in_ref


def _bill_doc(bill_slug="my-bill", act_slug="my-act", links=None):
    return {"bill_slug": bill_slug, "act_slug": act_slug, "links": links or []}


def _em_doc(em_slug="my-bill-em", bill_slug="my-bill", act_slug="my-act", links=None):
    return {"em_slug": em_slug, "bill_slug": bill_slug, "act_slug": act_slug, "links": links or []}


def _bill_link(clause, section, status="matched", similarity=1.0):
    return {
        "clause_number": clause, "bill_node_index": 0, "act_node_index": 0,
        "act_section_number": section, "status": status, "similarity": similarity, "verified_at": None,
    }


def test_section_numbers_in_ref_reads_the_shapes_an_em_actually_uses():
    assert section_numbers_in_ref("44A") == ["44A"]
    assert section_numbers_in_ref("3(1)") == ["3"]          # a pinpoint belongs to its section
    assert section_numbers_in_ref("22 and 23") == ["22", "23"]
    assert section_numbers_in_ref("5, 6 and 7") == ["5", "6", "7"]


def test_section_numbers_in_ref_keeps_only_a_ranges_first_endpoint():
    # "19A to 19C" names two sections; 19B is real but unnamed, and
    # attaching commentary to it would be inventing a link.
    assert section_numbers_in_ref("19A to 19C") == ["19A"]


def test_section_numbers_in_ref_ignores_a_reference_that_is_not_a_number():
    # extract_em_target's section_ref is whatever followed "section" in
    # running prose, which is sometimes not a number at all.
    assert section_numbers_in_ref("in") == []
    assert section_numbers_in_ref(None) == []


def test_an_em_note_on_a_bill_clause_lands_on_the_section_that_clause_became():
    bill = _bill_doc(links=[_bill_link("5", "7")])
    em = _em_doc(links=[{
        "em_node_index": 12, "clause_number": "5",
        "target": {"kind": "bill_clause", "act_slug": "my-act", "clause_number": "5"},
    }])

    index = build_commentary_index("my-act", [bill], [em])

    assert index["7"]["em"] == [
        {"em_slug": "my-bill-em", "em_node_index": 12, "clause_number": "5", "schedule": None, "via": "bill_clause"}
    ]
    assert index["7"]["bill"] == [
        {"bill_slug": "my-bill", "clause_number": "5", "status": "matched", "similarity": 1.0}
    ]


def test_an_em_note_naming_this_act_explicitly_lands_on_the_section_it_names():
    # How another Bill's EM reaches this Act: a consequential-amendment
    # note that names it rather than going through a clause match.
    em = _em_doc(em_slug="other-bill-em", bill_slug="other-bill", act_slug="other-act", links=[{
        "em_node_index": 3, "clause_number": "11",
        "target": {"kind": "act_section", "act_slug": "my-act", "act_title": "My Act 2000", "section_ref": "44A"},
    }])

    index = build_commentary_index("my-act", [], [em])

    assert index["44a"]["em"][0]["em_node_index"] == 3
    assert index["44a"]["em"][0]["via"] == "act_section"


def test_link_documents_about_other_acts_are_ignored():
    bill = _bill_doc(act_slug="someone-elses-act", links=[_bill_link("5", "7")])
    em = _em_doc(act_slug="someone-elses-act", links=[{
        "em_node_index": 1, "clause_number": "5",
        "target": {"kind": "bill_clause", "act_slug": "someone-elses-act", "clause_number": "5"},
    }])

    assert build_commentary_index("my-act", [bill], [em]) == {}


def test_a_repeated_clause_number_keeps_only_the_best_match():
    # A Bill numbers its Schedules' own clauses from 1 again, so several
    # records can carry clause "5" and land on the same section.
    bill = _bill_doc(links=[
        _bill_link("5", "5", status="flagged", similarity=0.03),
        _bill_link("5", "5", status="matched", similarity=0.98),
        _bill_link("5", "5", status="flagged", similarity=0.26),
    ])

    index = build_commentary_index("my-act", [bill], [])

    assert index["5"]["bill"] == [
        {"bill_slug": "my-bill", "clause_number": "5", "status": "matched", "similarity": 0.98}
    ]


def test_an_unmatched_clause_contributes_nothing():
    bill = _bill_doc(links=[{
        "clause_number": "99", "bill_node_index": 0, "act_node_index": None,
        "act_section_number": None, "status": "unmatched", "similarity": None, "verified_at": None,
    }])

    assert build_commentary_index("my-act", [bill], []) == {}


def test_an_unresolved_em_entry_contributes_nothing():
    em = _em_doc(links=[{"em_node_index": 4, "clause_number": None, "target": None}])

    assert build_commentary_index("my-act", [], [em]) == {}


def test_several_em_notes_can_reach_the_same_section():
    # An EM commonly has both a note on "clause 6" and a pinpoint note on
    # "clause 6(4)"; both genuinely explain the same section.
    bill = _bill_doc(links=[_bill_link("6", "6"), _bill_link("6(4)", "6")])
    em = _em_doc(links=[
        {"em_node_index": 8, "clause_number": "6",
         "target": {"kind": "bill_clause", "act_slug": "my-act", "clause_number": "6"}},
        {"em_node_index": 9, "clause_number": "6(4)",
         "target": {"kind": "bill_clause", "act_slug": "my-act", "clause_number": "6(4)"}},
    ])

    index = build_commentary_index("my-act", [bill], [em])

    assert [e["em_node_index"] for e in index["6"]["em"]] == [8, 9]


def test_an_em_note_carries_the_schedule_it_sits_under():
    # A Bill's Schedule numbers its own clauses from 1 again, so an Act
    # section can end up with two EM notes both calling themselves
    # "clause 11". Which Schedule each sits under is what tells them
    # apart -- see dashboard's own "EM on Schedule 1 clause 11" chip.
    bill = _bill_doc(links=[_bill_link("11", "11")])
    em = _em_doc(links=[
        {"em_node_index": 20, "clause_number": "11", "schedule": None,
         "target": {"kind": "bill_clause", "act_slug": "my-act", "clause_number": "11"}},
        {"em_node_index": 90, "clause_number": "11", "schedule": "1",
         "target": {"kind": "bill_clause", "act_slug": "my-act", "clause_number": "11"}},
    ])

    index = build_commentary_index("my-act", [bill], [em])

    assert [e["schedule"] for e in index["11"]["em"]] == [None, "1"]
