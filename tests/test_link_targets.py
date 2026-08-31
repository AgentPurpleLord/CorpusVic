"""Tests for ai_pipeline/link_targets.py -- resolving a labelled span's
text to a concrete target (which Act, which definition node), independent
of link_review.py's FastAPI layer and of link_annotations.py's storage."""
from ai_pipeline.link_targets import (
    build_definition_index,
    resolve_act_citation,
    resolve_defined_term,
    resolve_link,
)

from conftest import make_node


def test_resolve_act_citation_matches_a_known_act():
    result = resolve_act_citation("Crimes Act 1958")
    assert result == {"kind": "act", "act_slug": "crimes-act", "act_title": "Crimes Act 1958"}


def test_resolve_act_citation_tolerates_a_leading_the_and_trailing_punctuation():
    assert resolve_act_citation("the Crimes Act 1958.") == {
        "kind": "act",
        "act_slug": "crimes-act",
        "act_title": "Crimes Act 1958",
    }


def test_resolve_act_citation_does_not_fuzzy_match_the_wrong_year():
    # A wrong guess is worse than none -- a citation to a *different* Act
    # of the same short name (a repealed predecessor, say) must not
    # resolve to the one we happen to have parsed.
    assert resolve_act_citation("Crimes Act 1900") is None


def test_resolve_act_citation_unknown_act_is_unresolved():
    assert resolve_act_citation("Some Made Up Act 2099") is None


def test_resolve_act_citation_falls_back_to_the_comprehensive_act_registry():
    """A real Act this pipeline hasn't parsed itself (not in
    known_acts.yaml) still resolves via the comprehensive registry (see
    ai_pipeline/act_registry.py) -- no slug to link into, but confirmed
    real, with its current in-force status."""
    result = resolve_act_citation("Sentencing Act 1991")
    assert result["kind"] == "act"
    assert result["act_slug"] is None
    assert result["act_title"] == "Sentencing Act 1991"
    assert "in_force" in result


def test_resolve_act_citation_prefers_known_acts_over_the_registry():
    # Crimes Act 1958 is in both known_acts.yaml (with a real slug) and
    # the comprehensive registry -- the parsed one wins, since it's the
    # one an eventual link can actually point into.
    result = resolve_act_citation("Crimes Act 1958")
    assert result == {"kind": "act", "act_slug": "crimes-act", "act_title": "Crimes Act 1958"}


def test_build_definition_index_finds_terms_in_a_definitions_section():
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("subsection", "1", None, 'In this Act—\ncourt means the Magistrates\' Court of Victoria;\nvehicle means a motor vehicle.'),
        make_node("section", "4", "Application"),
    ]
    index = build_definition_index(nodes)
    assert index["court"] == 1
    assert index["vehicle"] == 1
    assert "application" not in index


def test_build_definition_index_stops_at_the_next_boundary_node():
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("subsection", "1", None, "court means the relevant court."),
        make_node("section", "4", "Application"),
        make_node("subsection", "1", None, "vehicle means a motor vehicle."),
    ]
    index = build_definition_index(nodes)
    assert index["court"] == 1
    assert "vehicle" not in index  # belongs to section 4, not a Definitions section


def test_build_definition_index_follows_a_same_meaning_as_in_section_pointer():
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("subsection", "1", None, "firearm has the same meaning as in section 10."),
        make_node("section", "10", "Meaning of firearm"),
        make_node("subsection", "1", None, "firearm means a device designed to fire a projectile."),
    ]
    index = build_definition_index(nodes)
    assert index["firearm"] == 2  # the section itself, not the definitions clause pointing at it


def test_resolve_defined_term_uses_a_precomputed_index():
    nodes = [make_node("section", "3", "Definitions"), make_node("subsection", "1", None, "court means the relevant court.")]
    index = build_definition_index(nodes)
    result = resolve_defined_term("court", nodes, index)
    assert result == {"kind": "definition", "node_index": 1, "number": "1", "heading": None}


def test_resolve_defined_term_unmatched_text_is_unresolved():
    nodes = [make_node("section", "3", "Definitions"), make_node("subsection", "1", None, "court means the relevant court.")]
    assert resolve_defined_term("nonexistent term", nodes) is None


def test_resolve_link_dispatches_by_label():
    nodes = [make_node("section", "3", "Definitions"), make_node("subsection", "1", None, "court means the relevant court.")]
    assert resolve_link("act_citation", "Crimes Act 1958", nodes)["kind"] == "act"
    assert resolve_link("defined_term", "court", nodes)["kind"] == "definition"


def test_resolve_link_bill_and_em_references_are_always_unresolved():
    nodes = [make_node("section", "1", "Purpose")]
    assert resolve_link("bill_reference", "Justice Legislation Amendment Bill 2024", nodes) is None
    assert resolve_link("em_reference", "Explanatory Memorandum", nodes) is None


def test_resolve_link_other_label_is_always_unresolved():
    nodes = [make_node("section", "1", "Purpose")]
    assert resolve_link("other", "anything", nodes) is None
