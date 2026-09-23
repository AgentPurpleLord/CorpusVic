"""Tests for corpus/review/inheritance.py -- a reviewed version's rows
lent to another version whose raw words are the same."""
from corpus.domain import lineage
from corpus.domain.hierarchy import group_into_units
from corpus.parsing.identity import annotate_ids
from corpus.review import inheritance

from conftest import make_node

S1 = ("provision", None, "1")


def _parse(text: str, extra: str = "") -> list[dict]:
    nodes = [make_node("part", "1", "Preliminary"),
             make_node("section", "1", "Appeals", ""),
             make_node("subsection", "1", None, text)]
    if extra:
        nodes.append(make_node("subsection", "2", None, extra))
    return annotate_ids(nodes)


def _row(node: dict, **changes) -> dict:
    return {**{k: node[k] for k in ("type", "number", "heading", "text")},
            "_node_id": node["id"], "verified_at": "2026-01-01", **changes}


def _state(nodes, rows=None, edits=None):
    units = group_into_units(nodes)
    rows = rows or {}
    finished = {u for u, unit in enumerate(units) if all(i in rows for i in unit)}
    return inheritance.unit_state(nodes, units, rows, finished, edits)


def test_an_inherited_unit_reads_as_the_reviewer_corrected_it():
    old, new = _parse("the acused may appeal"), _parse("the acused may appeal")
    rows = {1: _row(new[1]), 2: _row(new[2], text="the accused may appeal")}
    states = {1: _state(old), 2: _state(new, rows)}

    status = inheritance.resolve([1, 2], states)
    shown = inheritance.overlay(old, states[1], status[1], states)

    assert status[1][S1]["status"] == lineage.INHERITS
    assert shown[2]["text"] == "the accused may appeal"
    assert shown[2]["_inherited_from"] == 2
    assert S1 in inheritance.checked_keys(shown)


def test_the_borrowing_versions_own_page_and_notes_are_kept():
    old, new = _parse("words"), _parse("words")
    old[2]["page_start"] = 40
    old[2]["history"] = [{"raw": "S. 1(1) amended by No. 1/2020 s. 2."}]
    states = {1: _state(old), 2: _state(new, {1: _row(new[1]), 2: _row(new[2])})}

    shown = inheritance.overlay(old, states[1], inheritance.resolve([1, 2], states)[1], states)

    assert shown[2]["page_start"] == 40
    assert shown[2]["history"] == old[2]["history"]


def test_a_restructured_unit_is_not_lent():
    old, new = _parse("words"), _parse("words")
    states = {1: _state(old), 2: _state(new, {1: _row(new[1]), 2: _row(new[2])},
                                        edits={2: {"deleted": True}})}

    assert inheritance.resolve([1, 2], states)[1][S1]["status"] == lineage.TO_REVIEW


def test_a_unit_whose_pieces_differ_is_not_lent_even_with_the_same_words():
    """Same words split differently: rows would have nowhere to land."""
    old = _parse("the accused may", "appeal")
    new = _parse("the accused may appeal")
    states = {1: _state(old), 2: _state(new, {1: _row(new[1]), 2: _row(new[2])})}

    assert inheritance.resolve([1, 2], states)[1][S1]["status"] == lineage.TO_REVIEW


def test_a_piece_the_source_merged_away_is_left_out():
    old, new = _parse("words", "stray"), _parse("words", "stray")
    units = group_into_units(new)
    rows = {1: _row(new[1]), 2: _row(new[2], text="words stray")}
    source = inheritance.unit_state(new, units, rows, finished={1})
    states = {1: _state(old), 2: source}

    shown = inheritance.overlay(old, states[1], inheritance.resolve([1, 2], states)[1], states)

    assert [n["text"] for n in shown][1:] == ["", "words stray"]


def test_a_unit_with_a_row_of_its_own_is_the_versions_own():
    old, new = _parse("words"), _parse("words")
    own = [old[0], old[1], {**_row(old[2], text="mine")}]
    states = {1: _state(old, {2: own[2]}), 2: _state(new, {1: _row(new[1]), 2: _row(new[2])})}

    shown = inheritance.overlay(own, states[1], inheritance.resolve([1, 2], states)[1], states)

    assert shown[2]["text"] == "mine"
