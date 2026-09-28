"""Tests for corpus/definitions.py's defined-term extraction."""
from corpus.domain.definitions import (
    extract_section_ref_terms,
    extract_terms,
    looks_like_definitions_section,
    split_definition_clauses,
)


def test_looks_like_definitions_section():
    assert looks_like_definitions_section("Definitions")
    assert looks_like_definitions_section("Interpretation")
    assert not looks_like_definitions_section("Punishment for murder")
    assert not looks_like_definitions_section(None)


def test_extract_terms_basic_means_clause():
    assert extract_terms("weapon means any object capable of causing injury.") == ["weapon"]


def test_extract_terms_has_same_meaning():
    assert extract_terms("police officer has the same meaning as in the Victoria Police Act 2013.") == ["police officer"]


def test_extract_terms_multiple_terms_one_clause():
    text = "custodial officer, emergency worker on duty and emergency worker have the same meanings as in section 10AA."
    terms = extract_terms(text)
    assert terms == ["custodial officer", "emergency worker on duty", "emergency worker"]


def test_by_means_of_idiom_is_not_read_as_defining_by():
    """Regression: "by means of X" is a common idiom, not a definition of
    the word "by" -- means\\b(?!\\s+of\\b) guards against it."""
    assert extract_terms("A person may act by means of an agent.") == []


def test_extract_section_ref_terms_basic():
    text = "firearm has the same meaning as in section 3."
    results = extract_section_ref_terms(text)
    assert results == [(["firearm"], "3")]


def test_extract_section_ref_terms_excludes_external_act_citation():
    """Regression: "firearm has the same meaning as in section 3(1) of the
    Firearms Act 1996" points at a *different* Act's section, not this
    Act's -- must not be treated as an internal cross-reference."""
    text = "firearm has the same meaning as in section 3(1) of the Firearms Act 1996."
    assert extract_section_ref_terms(text) == []


def test_split_definition_clauses_separates_terms_joins_wraps():
    text = "\n".join(
        [
            "aircraft means every type of machine or structure",
            "used for navigation of the air;",
            "drug of addiction means a drug of dependence",
            "within the meaning of the Drugs Act 1981;",
        ]
    )
    clauses = split_definition_clauses(text)
    assert len(clauses) == 2
    assert clauses[0] == "aircraft means every type of machine or structure used for navigation of the air;"
    assert clauses[1] == "drug of addiction means a drug of dependence within the meaning of the Drugs Act 1981;"


# ---------------------------------------------------------------------
# Scope (issue #72)
# ---------------------------------------------------------------------

def test_a_scope_phrase_is_never_a_term():
    """"In this section, disclosure requirement means" (Evidence Act
    s131A): split on the comma, "in this section" came out as a term and
    was linked wherever it appeared."""
    from corpus.domain.definitions import extract_terms

    assert extract_terms("In this section, disclosure requirement means a process.") == ["disclosure requirement"]
    assert extract_terms("In this Division, a reference to loss includes a reference to harm") == []


def test_a_lead_in_names_its_scope():
    from corpus.domain.definitions import definition_scope

    assert definition_scope("In this Act—") == "act"
    assert definition_scope("(1) In this Division—") == "division"
    assert definition_scope("In this Division, request means a request") == "division"
    assert definition_scope("(2A) In subsection (1)—") == "section"
    assert definition_scope("In this section—") == "section"
    assert definition_scope("A person who assaults another") is None


def _scoped_act():
    from conftest import make_node

    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3", "Definitions", "In this Act—"),
        make_node("definition", None, "party", "means a party to a proceeding;"),
        make_node("note", None, None, "The Commonwealth Act includes a definition of this term."),
        make_node("part", "2", "Evidence"),
        make_node("division", "1", "Privilege"),
        make_node("section", "10", "Definitions", ""),
        make_node("subsection", "1", None, "In this Division—"),
        make_node("definition", None, "party", "includes an employee of a party;"),
        make_node("section", "11", "Privilege", "A party may object."),
        make_node("division", "2", "Witnesses"),
        make_node("section", "20", "Calling witnesses", "A party may call a witness."),
    ]


def _links(section_slug):
    import re
    from corpus.publishing.html_view import render_section

    body = render_section({"nodes": _scoped_act(), "hierarchy": None}, "Test Act", "/browse/t", section_slug)
    return re.findall(r'href="/browse/t/section/([^"#]+)[^"]*">([^<]+)</a>', body.split('<div class="provisions">')[1])


def test_a_term_defined_for_a_division_links_there_only_inside_it():
    """Evidence Act s117 defines "party" for its own Division; read as the
    Act's, it beat the Act-wide definition everywhere (issue #72)."""
    assert ("s10", "party") in _links("s11")
    assert ("s3", "party") in _links("s20") and ("s10", "party") not in _links("s20")


def test_a_note_is_never_a_definition():
    """"The Commonwealth Act includes a definition of this term" -- a Note
    in the Evidence Act's Dictionary, shaped exactly like "X includes"."""
    from corpus.publishing.html_view import _build_context

    ctx = _build_context({"nodes": _scoped_act(), "hierarchy": None}, "Test Act")
    assert "the commonwealth act" not in ctx["definitions"]
