"""Naming a provision by what it is.

The names below are the ones a lawyer would use -- "s97/d/i" is section
97(d)(i) -- and the point of them is that they survive a re-parse, which
a position in the node list does not.
"""
import pytest

from corpus.parsing.identity import (annotate_ids, disambiguate, inserted_id,
                                     inserted_ids,
                                     node_ids)
from corpus.parsing.tree import annotate_paths


def node(node_type, number=None, heading=None, text=""):
    return {"type": node_type, "number": number, "heading": heading, "text": text}


def names(nodes, hierarchy=None):
    annotate_paths(nodes, hierarchy) if hierarchy else annotate_paths(nodes)
    return node_ids(nodes, hierarchy)


# --- how a name reads ----------------------------------------------------

def test_a_provision_is_named_the_way_it_is_cited():
    got = names([
        node("part", "2", "Committal proceeding"),
        node("division", "1", "General"),
        node("section", "97", "Purposes of a committal proceeding"),
        node("paragraph", "d", text="to ensure a fair trial, by—"),
        node("subparagraph", "i", text="ensuring that the prosecution case"),
    ])
    assert got == ["pt2", "pt2/div1", "pt2/div1/s97",
                   "pt2/div1/s97/d", "pt2/div1/s97/d/i"]


def test_a_dotted_part_number_survives():
    assert names([node("part", "2.1", "Ways in which a criminal proceeding")]) == ["pt2.1"]


def test_a_bill_clause_is_named_like_a_section():
    assert names([node("clause", "31", "Charge-sheet")]) == ["cl31"]


def test_a_defined_term_is_named_by_the_term():
    got = names([
        node("section", "15", "Definitions"),
        node("definition", heading="injury"),
        node("paragraph", "a", text="physical injury; or"),
    ])
    assert got == ["s15", "s15/definition-injury", "s15/definition-injury/a"]


def test_two_definitions_own_lists_do_not_share_a_name():
    got = names([
        node("section", "15", "Definitions"),
        node("definition", heading="female genital mutilation"),
        node("paragraph", "a", text="infibulation;"),
        node("definition", heading="injury"),
        node("paragraph", "a", text="physical injury; or"),
    ])
    assert got[2] != got[4]
    assert got[2].endswith("definition-female-genital-mutilation/a")
    assert got[4].endswith("definition-injury/a")


def test_a_provision_with_no_number_or_heading_is_named_by_its_wording():
    got = names([node("section", "97", "Purposes"),
                 node("note", text="See section 6(1)."),
                 node("note", text="Section 102 provides for a filing hearing.")])
    assert got[1] != got[2]
    assert got[1].startswith("s97/note~")


# --- names are unique, and stay put --------------------------------------

def test_a_duplicate_keeps_the_first_and_qualifies_the_rest():
    # A citation that wrapped onto its own line and was read as a fresh
    # subsection: two (1)s in one section, neither the Act's doing.
    got = names([
        node("section", "31D", "Intimidation"),
        node("subsection", "1", text="A person commits an offence if—"),
        node("subsection", "1", text="it is a defence to the charge"),
    ])
    assert got[1] == "s31d/1"
    assert got[2].startswith("s31d/1~")
    assert got[1] != got[2]


def test_identical_wording_as_well_as_an_identical_name_falls_back_to_order():
    got = names([node("section", "1", "Short title"),
                 node("note", text="* * *"), node("note", text="* * *")])
    assert len(set(got)) == 3
    assert got[2].endswith("-2")


def test_the_same_nodes_always_get_the_same_names():
    def build():
        return [node("section", "97", "Purposes"), node("paragraph", "a", text="one;")]
    assert names(build()) == names(build())


def test_a_name_survives_a_provision_being_inserted_above_it():
    before = names([node("section", "97", "Purposes"),
                    node("paragraph", "a", text="one;")])
    after = names([node("section", "96", "Something new"),
                   node("section", "97", "Purposes"),
                   node("paragraph", "a", text="one;")])
    # Every position has moved; both names have not.
    assert before == after[1:]


def test_a_name_survives_the_wording_being_edited():
    before = names([node("section", "97", "Purposes"), node("paragraph", "a", text="one;")])
    after = names([node("section", "97", "Purposes"),
                   node("paragraph", "a", text="one, as amended by Act 12 of 2024;")])
    assert before == after


def test_annotate_ids_puts_the_name_on_the_node():
    nodes = [node("section", "97", "Purposes")]
    annotate_paths(nodes)
    annotate_ids(nodes)
    assert nodes[0]["id"] == "s97"


def test_paths_are_annotated_when_they_are_missing():
    # node_ids is given a bare node list, with no breadcrumb on it.
    assert node_ids([node("section", "97", "Purposes"),
                     node("subsection", "1", text="text")]) == ["s97", "s97/1"]


# --- provisions a reviewer added -----------------------------------------

def test_an_inserted_provision_is_named_against_what_it_follows():
    assert inserted_id("s97/d", node("note", "1AA")) == "s97/d+note-1aa"


def test_an_insert_after_an_insert_nests():
    first = inserted_id("s97/d", node("note", "1AA"))
    assert inserted_id(first, node("note", "2")) == "s97/d+note-1aa+note-2"


def test_an_unnumbered_insert_is_named_by_its_wording():
    name = inserted_id("s97", node("note", text="See section 6(1)."))
    assert name.startswith("s97+note~")


def test_two_identical_inserts_after_one_provision_are_told_apart():
    added = node("note", "1AA")
    first = inserted_id("s97/d", added)
    second = disambiguate(inserted_id("s97/d", added), {first}, added)
    assert first != second
    assert second.startswith(first + "~")


def test_disambiguate_leaves_a_name_nothing_has_claimed():
    assert disambiguate("s97/d+note-1aa", set(), node("note", "1AA")) == "s97/d+note-1aa"


def test_an_insert_is_named_by_what_it_follows_not_by_when_it_was_made():
    """A reviewer who adds a provision and then adds another one above it
    gives the second a higher index and the first a higher anchor. Taking
    them in index order left the first hanging off nothing."""
    names = ["s38"]
    added_second = node("definition", heading="ASC law")
    added_first = node("definition", heading="ASIC law")
    got = inserted_ids(names, [
        (859, 862, added_first),   # made first, follows the one below
        (862, 0, added_second),    # made second, follows the parse
    ])
    assert got[862] == "s38+definition-asc-law"
    assert got[859] == "s38+definition-asc-law+definition-asic-law"


def test_an_anchor_that_does_not_exist_falls_back_to_the_front():
    got = inserted_ids(["s38"], [(900, 7777, node("note", "1"))])
    assert got[900] == "inserted+note-1"


def test_two_inserts_anchored_to_each_other_still_get_names():
    a, b = node("note", "1"), node("note", "2")
    got = inserted_ids(["s38"], [(900, 901, a), (901, 900, b)])
    assert len(set(got.values())) == 2
    assert set(got) == {900, 901}


def test_a_schedule_clause_answers_to_its_former_name():
    """Schedule provisions were typed sections until issue #72: review
    work recorded against "sch1/s2" belongs to what is now "sch1/cl2"."""
    from corpus.parsing.identity import name_index

    index_of = name_index([(0, "s2/a"), (1, "sch1/cl2"), (2, "sch1/cl2/a"), (3, "sch2/pt1/item4"), (4, "cl7")])

    assert (index_of["sch1/s2"], index_of["sch1/s2/a"], index_of["sch2/pt1/s4"]) == (1, 2, 3)
    assert index_of["s2/a"] == 0 and "s7" not in index_of, "outside a Schedule, nothing is renamed"
