"""Tests for corpus/definitions.py's defined-term extraction."""
from corpus.definitions import (
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
