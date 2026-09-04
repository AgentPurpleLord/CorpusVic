"""Tests for ai_pipeline/amendments.py -- turning a margin note's bare
"No. 68/2009" into the Act it names, using the Act's own Table of
Amendments first and the general Act registry as a fallback."""
from ai_pipeline.amendments import (
    anchor_id,
    linkify_note,
    build_amendment_index,
    citations_in,
    describe,
    provision_label,
    resolve_note,
    summarise_by_act,
)

from conftest import make_node

ENDNOTES = {
    "amending_acts": [
        {
            "title": "Criminal Procedure Amendment (Consequential and Transitional Provisions) Act 2009",
            "citation": "68/2009", "act_no": "68", "year": "2009", "is_statutory_rule": False,
            "fields": {"assent_date": "24.11.09", "commencement_date": "Ss 3–58 on 25.11.09: s. 2(1)"},
        },
        {
            "title": "Crimes (Penalties) Act 1959",
            "citation": "6561/1959", "act_no": "6561", "year": "1959", "is_statutory_rule": False,
            "fields": {"assent_date": "1.12.59"},
        },
    ]
}

REGISTRY = {
    "Some Other Act 2014": {"act_no": "47", "year": "2014", "in_force": True},
    "An Ambiguous Act 1990": {"act_no": "12", "year": "1990", "in_force": True},
    "Another Ambiguous Act 1991": {"act_no": "12", "year": "1991", "in_force": False},
}


def _index():
    return build_amendment_index(ENDNOTES, REGISTRY)


def test_a_citation_resolves_from_the_acts_own_table_of_amendments():
    found = resolve_note("S. 3 def. of accused amended by No. 68/2009 s. 51(b)(i).", _index())

    assert len(found) == 1
    assert found[0]["title"].startswith("Criminal Procedure Amendment")
    assert found[0]["assent_date"] == "24.11.09"
    assert found[0]["source"] == "endnotes"


def test_a_citation_absent_from_the_table_falls_back_to_the_registry():
    found = resolve_note("S. 3 def. of infringements registrar repealed by No. 47/2014 s. 256(b).", _index())

    assert found[0]["title"] == "Some Other Act 2014"
    assert found[0]["source"] == "registry"
    # The registry knows the title, never the assent or commencement.
    assert "assent_date" not in found[0]


def test_an_ambiguous_registry_number_resolves_to_nothing():
    # Act numbers restart each year, so "No. 12" alone names two Acts here.
    # Naming the wrong one is worse than naming none.
    assert resolve_note("S. 4 amended by No. 12.", _index()) == []


def test_a_pre_1970s_number_with_no_year_still_resolves_from_the_table():
    # Old Victorian Act numbers carry no year at all, but within one Act's
    # own Table of Amendments they're unique.
    found = resolve_note("S. 5 substituted by No. 6561 s. 2.", _index())

    assert found[0]["title"] == "Crimes (Penalties) Act 1959"


def test_a_note_citing_several_acts_resolves_each_of_them():
    # The plural form writes "Nos" once and then lists bare numbers -- the
    # citation pattern used to find only the first, or with "Nos" none.
    found = resolve_note(
        "S. 3 def. of in detention amended by Nos 26/2014 s. 455(Sch. item 8.1), 68/2009 s. 3.", _index()
    )

    assert [c["label"] for c in citations_in("amended by Nos 26/2014 s. 1, 68/2009 s. 3.")] == [
        "No. 26/2014", "No. 68/2009"
    ]
    assert [f["citation"] for f in found] == ["68/2009"]  # 26/2014 is in neither source


def test_the_same_act_cited_twice_in_one_note_is_returned_once():
    found = resolve_note("S. 3 inserted by No. 68/2009 s. 3, amended by No. 68/2009 s. 51.", _index())

    assert len(found) == 1


def test_describe_states_only_what_the_record_holds():
    index = _index()
    from_endnotes = resolve_note("amended by No. 68/2009 s. 3.", index)[0]
    from_registry = resolve_note("amended by No. 47/2014 s. 1.", index)[0]

    assert describe(from_endnotes) == (
        "Criminal Procedure Amendment (Consequential and Transitional Provisions) Act 2009 "
        "— assented 24.11.09 — commenced Ss 3–58 on 25.11.09: s. 2(1)"
    )
    assert describe(from_registry) == "Some Other Act 2014"


def test_provision_label_reads_the_path_breadcrumb():
    node = make_node("paragraph", "b", None, "text")
    node["path"] = {"section": "28", "subsection": "1", "paragraph": "b"}

    assert provision_label(node) == "s. 28(1)(b)"


def test_provision_label_names_the_defined_term_it_sits_under():
    node = make_node("definition", None, "accused", "means a person who—")
    node["path"] = {"section": "3", "definition": "accused"}

    assert provision_label(node) == 's. 3 def. of "accused"'


def test_summarise_by_act_inverts_the_table_into_what_each_act_changed():
    nodes = [
        make_node("section", "3", "Definitions", "In this Act—", history=[
            {"raw": "S. 3 amended by No. 68/2009 s. 51."},
        ]),
        make_node("section", "5", "Commencement", "text", history=[
            {"raw": "S. 5 substituted by No. 68/2009 s. 4."},
        ]),
    ]
    for node in nodes:
        node["path"] = {"section": node["number"]}

    summary = summarise_by_act(nodes, _index())

    assert len(summary) == 1
    assert summary[0]["citation"] == "68/2009"
    assert summary[0]["count"] == 2
    assert [p["label"] for p in summary[0]["provisions"]] == ["s. 3", "s. 5"]
    assert [p["section_number"] for p in summary[0]["provisions"]] == ["3", "5"]


def test_summarise_by_act_counts_a_provision_once_however_often_it_was_amended():
    node = make_node("section", "3", "Definitions", "text", history=[
        {"raw": "S. 3 inserted by No. 68/2009 s. 3."},
        {"raw": "S. 3 amended by No. 68/2009 s. 51."},
    ])
    node["path"] = {"section": "3"}

    summary = summarise_by_act([node], _index())

    assert summary[0]["count"] == 1


def test_anchor_id_is_a_stable_html_id_for_a_citation():
    assert anchor_id("68/2009") == "act-68-2009"
    assert anchor_id(None) == "act-unknown"


def test_a_citation_reports_where_it_sits_in_the_note():
    # The span covers the "No."/"Nos" a reader would call part of the
    # citation -- but only where the note actually writes one: the plural
    # form writes it once and lists bare numbers after it.
    found = citations_in("S. 3 amended by Nos 26/2014 s. 455, 68/2009 s. 3.")
    raw = "S. 3 amended by Nos 26/2014 s. 455, 68/2009 s. 3."

    assert [raw[c["start"]:c["end"]] for c in found] == ["Nos 26/2014", "68/2009"]


def test_linkify_note_splits_a_note_around_its_citations():
    runs = linkify_note("S. 3 inserted by No. 68/2009 s. 3, amended by No. 6561 s. 2.", _index())

    assert "".join(r["text"] for r in runs) == "S. 3 inserted by No. 68/2009 s. 3, amended by No. 6561 s. 2."
    assert [r["text"] for r in runs if "record" in r] == ["No. 68/2009", "No. 6561"]
    assert [r["record"]["citation"] for r in runs if "record" in r] == ["68/2009", "6561/1959"]


def test_linkify_note_leaves_an_unresolvable_citation_as_plain_text():
    # Naming a link's destination it can't actually reach would be worse
    # than leaving the citation as the note prints it.
    runs = linkify_note("S. 4 amended by No. 12.", _index())

    assert runs == [{"text": "S. 4 amended by No. 12."}]


def test_linkify_note_without_an_index_is_one_plain_run():
    runs = linkify_note("S. 3 amended by No. 68/2009 s. 51.", None)

    assert runs == [{"text": "S. 3 amended by No. 68/2009 s. 51."}]
