"""Tests for the per-Act control over which words the site hyperlinks
back to where they are defined (issue #47).

corpus/definitions.py finds defined terms by drafting convention and says
in its own docstring that it is a navigation aid rather than a guarantee:
it misses a term phrased unusually, and now and then it catches a phrase
that is not a definition. Neither is fixable in general and both are
obvious to somebody reading the Act, so the answer is to let that person
say otherwise. These cover the saying, the applying, and the two ways it
could go quietly wrong -- an 'add' pointing at a Section that does not
exist, and an edit that never reaches the page because something was
still cached.
"""
import json

import pytest

from corpus.publishing import html_view
from corpus.storage import db
from corpus.exporters.markdown_export import apply_definition_overrides
from conftest import make_node


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.close_connections()
    yield
    db.close_connections()


# A document with a real Definitions section, so the matcher has
# something to find and something to miss.
def _act_nodes():
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3", "Definitions", "In this Act—"),
        make_node("definition", None, "family violence", "means behaviour that—"),
        make_node("definition", None, "affected person", "means a person against whom—"),
        make_node("section", "4", "Application", "This Act applies to a safety notice."),
        make_node("section", "5", "Safety notices", "A safety notice may be issued."),
    ]


def _parsed(overrides=None):
    return {"nodes": _act_nodes(), "hierarchy": None,
            "definition_overrides": overrides or []}


def _definitions(overrides=None):
    return html_view._build_context(_parsed(overrides), "Test Act 2020")["definitions"]


# ---------------------------------------------------------------------------
# What the matcher finds on its own
# ---------------------------------------------------------------------------

def test_the_matcher_finds_the_definitions_and_misses_the_rest():
    """The starting point, and the reason this feature exists. "safety
    notice" is used twice in this Act and defined nowhere the patterns
    recognise, so it goes unlinked."""
    found = _definitions()

    assert "family violence" in found
    assert "affected person" in found
    assert "safety notice" not in found


# ---------------------------------------------------------------------------
# Applying a decision
# ---------------------------------------------------------------------------

def test_a_removed_term_stops_being_linked():
    after = _definitions([{"term": "family violence", "action": "remove"}])

    assert "family violence" not in after
    assert "affected person" in after, "only the one named"


def test_an_added_term_links_to_the_section_it_names():
    after = _definitions([{"term": "safety notice", "action": "add", "section": "5"}])

    assert after["safety notice"]["file"] == "s5.md"
    # No fragment, exactly like the "same meaning as in section N"
    # pointers the matcher already follows: a person naming a Section is
    # making the same kind of statement, and gets the same kind of link.
    assert after["safety notice"]["fragment"] is None


def test_an_add_pointing_at_a_section_that_does_not_exist_is_dropped():
    """walk_section_refs's own rule, and the one that matters most: a
    missing hyperlink is a missing hyperlink, but a term linked to the
    wrong provision tells a reader something untrue about the law."""
    after = _definitions([{"term": "safety notice", "action": "add", "section": "99"}])

    assert "safety notice" not in after


def test_a_term_is_matched_lowercased_however_it_was_typed():
    after = _definitions([{"term": "  Family Violence  ", "action": "remove"}])

    assert "family violence" not in after


def test_an_override_can_repoint_a_term_the_matcher_already_found():
    """Not only add and remove: naming a Section for a term the matcher
    placed elsewhere moves it, which is what "it linked to the wrong
    place" needs."""
    after = _definitions([{"term": "family violence", "action": "add", "section": "4"}])

    assert after["family violence"]["file"] == "s4.md"


def test_the_list_the_dashboard_shows_keeps_both_sides():
    """A term the matcher found and a term somebody added look identical
    on the page. Telling them apart is the whole point of the view, so
    the context carries what was found as well as what is linked."""
    ctx = html_view._build_context(
        _parsed([{"term": "family violence", "action": "remove"},
                 {"term": "safety notice", "action": "add", "section": "5"}]),
        "Test Act 2020")

    assert "family violence" in ctx["definitions_found"]
    assert "family violence" not in ctx["definitions"]
    assert "safety notice" not in ctx["definitions_found"]
    assert "safety notice" in ctx["definitions"]


def test_applying_nothing_changes_nothing():
    definitions = {"a": {"file": "s1.md", "fragment": None, "display": "a"}}

    assert apply_definition_overrides(definitions, None, {}) == {
        "a": {"file": "s1.md", "fragment": None, "display": "a"}}


# ---------------------------------------------------------------------------
# The cache, which is where this would fail silently
# ---------------------------------------------------------------------------

def test_a_decision_is_not_served_from_a_stale_context():
    """The structure derived from a document is cached on the identity of
    its node list, and recording a decision does not change that list.
    Without the overrides in the cache key, the page built before the
    decision goes on being served -- the edit appears to work, the site
    does not change, and nothing says so."""
    nodes = _act_nodes()
    before = html_view._build_context(
        {"nodes": nodes, "hierarchy": None, "definition_overrides": []}, "Test Act 2020")
    after = html_view._build_context(
        {"nodes": nodes, "hierarchy": None,
         "definition_overrides": [{"term": "family violence", "action": "remove"}]},
        "Test Act 2020")

    assert "family violence" in before["definitions"], "same list object, so this is the cached one"
    assert "family violence" not in after["definitions"]


def test_recording_the_same_decision_twice_is_not_a_different_page():
    """created_at is deliberately out of the key: re-saving a decision
    that says the same thing should not throw away a cached build."""
    nodes = _act_nodes()
    one = {"term": "family violence", "action": "remove", "created_at": "2026-01-01T00:00:00+00:00"}
    two = {"term": "family violence", "action": "remove", "created_at": "2026-09-19T00:00:00+00:00"}

    first = html_view._build_context({"nodes": nodes, "hierarchy": None,
                                      "definition_overrides": [one]}, "Test Act 2020")
    second = html_view._build_context({"nodes": nodes, "hierarchy": None,
                                       "definition_overrides": [two]}, "Test Act 2020")

    assert first is second


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_a_decision_round_trips():
    db.set_definition_override("test-act", "Safety Notice", "add", "5")

    [row] = db.load_definition_overrides("test-act")
    assert (row["term"], row["action"], row["section"]) == ("safety notice", "add", "5")


def test_a_second_decision_about_one_term_replaces_the_first():
    db.set_definition_override("test-act", "safety notice", "add", "5")
    db.set_definition_override("test-act", "safety notice", "remove")

    [row] = db.load_definition_overrides("test-act")
    assert (row["action"], row["section"]) == ("remove", None)


def test_clearing_hands_the_term_back_to_the_matcher():
    db.set_definition_override("test-act", "safety notice", "remove")

    assert db.clear_definition_override("test-act", "Safety Notice") is True
    assert db.load_definition_overrides("test-act") == []
    assert db.clear_definition_override("test-act", "safety notice") is False


def test_decisions_do_not_leak_between_documents():
    """Per document, not per work: a Section number means different
    provisions in two reprints of the same Act, so a decision naming one
    cannot be carried across."""
    db.set_definition_override("act-v1", "safety notice", "add", "5")

    assert db.load_definition_overrides("act-v2") == []


@pytest.mark.parametrize("action, section", [
    ("add", None),        # nowhere to point it
    ("sideways", "5"),    # not a thing this can record
])
def test_a_decision_that_cannot_mean_anything_is_refused(action, section):
    with pytest.raises(ValueError):
        db.set_definition_override("test-act", "safety notice", action, section)


def test_a_removal_needs_no_section():
    """The other side of the rule above: "this is not a definition" says
    nothing about where anything points."""
    db.set_definition_override("test-act", "safety notice", "remove")

    assert db.load_definition_overrides("test-act")[0]["section"] is None


def test_an_empty_term_is_refused():
    with pytest.raises(ValueError):
        db.set_definition_override("test-act", "   ", "remove")


def test_the_decisions_travel_with_the_review_work():
    """The table is exported by corpus/review/review_sync.py without being named
    there -- its registry is read from the schema. That is what stops a
    new table silently not syncing, and it is worth one test that the
    mechanism actually covers this one."""
    from corpus.review import review_sync

    db.set_definition_override("test-act", "safety notice", "add", "5")
    review_sync.export(".")

    written = review_sync.review_dir(".") / "test-act" / "definition_overrides.jsonl"
    assert written.exists()
    assert json.loads(written.read_text())["term"] == "safety notice"
