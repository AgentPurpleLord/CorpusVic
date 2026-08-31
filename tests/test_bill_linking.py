"""Tests for ai_pipeline/bill_linking.py -- Bill clause <-> Act section
matching by number + text similarity, and EM entry -> Act/section/Bill-
clause target resolution, including the "Principal Act"/"this Act" alias
tracking the OCPC's own EM-drafting guide describes."""
from ai_pipeline.bill_linking import extract_em_target, match_bill_to_act, resolve_em_links

from conftest import make_node


def test_match_bill_to_act_matches_identical_text():
    bill_nodes = [make_node("clause", "5", "How commenced", "How a criminal proceeding is commenced.")]
    act_nodes = [make_node("section", "5", "How commenced", "How a criminal proceeding is commenced.")]
    links = match_bill_to_act(bill_nodes, act_nodes)
    assert links == [
        {"clause_number": "5", "bill_node_index": 0, "act_node_index": 0, "similarity": 1.0, "status": "matched", "verified_at": None}
    ]


def test_match_bill_to_act_flags_a_same_numbered_but_divergent_section():
    bill_nodes = [make_node("clause", "12", None, "The court may adjourn the proceeding for up to 7 days.")]
    act_nodes = [make_node("section", "12", None, "Completely unrelated text about an entirely different topic here.")]
    links = match_bill_to_act(bill_nodes, act_nodes)
    assert links[0]["status"] == "flagged"
    assert links[0]["similarity"] < 0.6


def test_match_bill_to_act_reports_unmatched_when_no_section_exists():
    bill_nodes = [make_node("clause", "999", None, "text")]
    act_nodes = [make_node("section", "1", None, "text")]
    links = match_bill_to_act(bill_nodes, act_nodes)
    assert links[0]["status"] == "unmatched"
    assert links[0]["act_node_index"] is None
    assert links[0]["similarity"] is None


def test_match_bill_to_act_compares_full_nested_text_not_just_the_lead_in():
    """A clause/section's own "text" field is often near-empty, with the
    substantive content in nested subsections as separate flat nodes --
    comparing only the lead-in would give almost no signal."""
    bill_nodes = [
        make_node("clause", "2", "Commencement", ""),
        make_node("subsection", "1", None, "This Act comes into operation on a day to be proclaimed."),
    ]
    act_nodes = [
        make_node("section", "2", "Commencement", ""),
        make_node("subsection", "1", None, "This Act comes into operation on a day to be proclaimed."),
    ]
    links = match_bill_to_act(bill_nodes, act_nodes)
    assert links[0]["status"] == "matched"
    assert links[0]["similarity"] == 1.0


def test_match_bill_to_act_stops_reconstruction_at_the_next_boundary_node():
    bill_nodes = [
        make_node("clause", "1", None, "lead-in"),
        make_node("subsection", "1", None, "belongs to clause 1"),
        make_node("clause", "2", None, "belongs to clause 2"),
    ]
    act_nodes = [
        make_node("section", "1", None, "lead-in"),
        make_node("subsection", "1", None, "belongs to clause 1"),
        make_node("section", "2", None, "different text entirely for section 2"),
    ]
    links = match_bill_to_act(bill_nodes, act_nodes)
    clause_1 = next(l for l in links if l["clause_number"] == "1")
    assert clause_1["similarity"] == 1.0  # section 2's text must not have leaked in


def test_match_bill_to_act_only_considers_clause_and_section_nodes():
    bill_nodes = [make_node("clause", "1", None, "text"), make_node("subsection", "1", None, "text")]
    act_nodes = [make_node("section", "1", None, "text")]
    links = match_bill_to_act(bill_nodes, act_nodes)
    assert len(links) == 1
    assert links[0]["clause_number"] == "1"


def test_extract_em_target_finds_act_name_section_and_action_verb():
    text = "inserts new section 44A into the Confiscation Act 1997 to empower the Minister."
    found = extract_em_target(text)
    assert found == {"action": "inserts", "act_name": "Confiscation Act 1997", "act_is_self_alias": False, "section_ref": "44A"}


def test_extract_em_target_handles_a_section_range():
    text = "inserts new sections 19A to 19C in the Public Health and Wellbeing Act 2008."
    found = extract_em_target(text)
    assert found["act_name"] == "Public Health and Wellbeing Act 2008"
    assert found["section_ref"] == "19A to 19C"


def test_extract_em_target_handles_a_pinpoint_subsection_reference():
    text = "substitutes section 25(1) of the Births, Deaths and Marriages Registration Act 1996."
    found = extract_em_target(text)
    assert found["act_name"] == "Births, Deaths and Marriages Registration Act 1996"
    assert found["section_ref"] == "25(1)"


def test_extract_em_target_recognises_the_principal_act_alias():
    text = "amends section 32 of the Principal Act to enable a re-appointment."
    found = extract_em_target(text)
    assert found["act_name"] is None
    assert found["act_is_self_alias"] is True
    assert found["section_ref"] == "32"


def test_extract_em_target_recognises_this_act_alias():
    text = "repeals section 13 of the Principal Act, which provides for an automatic expiry."
    found = extract_em_target(text)
    assert found["act_is_self_alias"] is True


def test_extract_em_target_act_name_wrapped_across_a_line_break():
    """The entry's stored text has the source PDF's own line-wrap points
    as literal "\\n"s (see em_parser.py) -- an Act name that happens to
    wrap across one must still match whole."""
    text = "amends section 3(1) of the Appeal Costs\nAct 1998 to update a reference."
    found = extract_em_target(text)
    assert found["act_name"] == "Appeal Costs Act 1998"


def test_extract_em_target_does_not_swallow_a_sentence_leading_up_to_the_real_title():
    """An ordinary sentence-initial capital (and everything up to the
    next "Act YYYY" it happens to reach) is not itself an Act name --
    only genuine Title Case, plus a small set of lower-case connectors,
    counts."""
    text = "As the note to the clause indicates, the Electronic Transactions Act 2000 applies."
    found = extract_em_target(text)
    assert found["act_name"] == "Electronic Transactions Act 2000"


def test_extract_em_target_lowercase_connector_words_in_act_title():
    text = "adopts the meaning used in the Interpretation of Legislation Act 1984."
    found = extract_em_target(text)
    assert found["act_name"] == "Interpretation of Legislation Act 1984"


def test_extract_em_target_returns_all_none_for_a_purely_explanatory_note():
    text = "sets out the purposes of the Bill."
    found = extract_em_target(text)
    assert found == {"action": None, "act_name": None, "act_is_self_alias": False, "section_ref": None}


def test_resolve_em_links_resolves_a_named_act_and_tracks_it_as_current_scope():
    known_acts = {"confiscation-act": "Confiscation Act 1997"}
    em_nodes = [
        make_node("em_entry", "11", None, "inserts new section 44A into the Confiscation Act 1997 to empower the Minister."),
        make_node("em_entry", "12", None, "substitutes section 44B of the Principal Act to clarify the procedure."),
    ]
    links = resolve_em_links(em_nodes, "some-bill", "some-bill-act", bill_to_act=[], known_acts=known_acts)

    assert links[0]["target"] == {
        "kind": "act_section",
        "act_slug": "confiscation-act",
        "act_title": "Confiscation Act 1997",
        "section_ref": "44A",
    }
    # Second entry names no Act at all -- "Principal Act" means whichever
    # Act was just named, i.e. the Confiscation Act, not the Bill itself.
    assert links[1]["target"] == {
        "kind": "act_section",
        "act_slug": "confiscation-act",
        "act_title": "Confiscation Act 1997",
        "section_ref": "44B",
    }


def test_resolve_em_links_defaults_to_the_bills_own_act_before_any_act_is_named():
    bill_to_act = [{"clause_number": "1"}]
    em_nodes = [make_node("em_entry", "1", None, "sets out the purposes of this Act, which are to consolidate the law.")]
    links = resolve_em_links(em_nodes, "my-bill", "my-bill-act", bill_to_act=bill_to_act, known_acts={})
    assert links[0]["target"] == {"kind": "act_section", "act_slug": "my-bill-act", "act_title": None, "section_ref": None}


def test_resolve_em_links_switches_scope_when_a_new_act_is_named():
    known_acts = {"water-act": "Water Act 2000", "roads-act": "Roads Act 2001"}
    em_nodes = [
        make_node("em_entry", "5", None, "amends section 1 of the Water Act 2000 to update a reference."),
        make_node("em_entry", "6", None, "amends section 2 of the Roads Act 2001 to update a reference."),
        make_node("em_entry", "7", None, "amends section 3 of the Principal Act to update a reference."),
    ]
    links = resolve_em_links(em_nodes, "some-bill", "some-bill-act", bill_to_act=[], known_acts=known_acts)
    assert links[0]["target"]["act_slug"] == "water-act"
    assert links[1]["target"]["act_slug"] == "roads-act"
    assert links[2]["target"]["act_slug"] == "roads-act"  # most recently named, not water-act


def test_resolve_em_links_resolves_a_bare_clause_explanation_to_the_bill_itself():
    """"sets out the purposes of the Bill" names no Act, no alias
    ("Act" never appears -- only "Bill"), and no section -- the only
    signal left is that entry 1's own number matches a real Bill clause,
    which is enough to resolve it as explaining that clause of the Bill
    itself."""
    bill_to_act = [{"clause_number": "1"}]
    em_nodes = [make_node("em_entry", "1", None, "sets out the purposes of the Bill.")]
    links = resolve_em_links(em_nodes, "my-bill", "my-bill-act", bill_to_act=bill_to_act, known_acts={})
    assert links[0]["target"] == {"kind": "bill_clause", "act_slug": "my-bill-act", "clause_number": "1"}

    # A note with literally nothing -- no alias, no section, no act name,
    # and its own number isn't a known Bill clause -- resolves to nothing.
    em_nodes2 = [make_node("em_entry", None, None, "General remarks about the policy background.")]
    links2 = resolve_em_links(em_nodes2, "my-bill", "my-bill-act", bill_to_act=[], known_acts={})
    assert links2[0]["target"] is None


def test_resolve_em_links_unrecognised_act_name_keeps_title_without_a_slug():
    em_nodes = [make_node("em_entry", "3", None, "amends section 1 of the Obscure Made Up Act 2099 to clarify a term.")]
    links = resolve_em_links(em_nodes, "some-bill", "some-bill-act", bill_to_act=[], known_acts={})
    assert links[0]["target"]["act_slug"] is None
    assert links[0]["target"]["act_title"] == "Obscure Made Up Act 2099"


def test_resolve_em_links_skips_non_entry_nodes():
    em_nodes = [make_node("heading_group", None, "CHAPTER 1", "CHAPTER 1"), make_node("em_entry", "1", None, "sets out the purposes.")]
    links = resolve_em_links(em_nodes, "my-bill", "my-bill-act", bill_to_act=[], known_acts={})
    assert len(links) == 1
    assert links[0]["em_node_index"] == 1
