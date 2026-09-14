"""Tests for tree.py's annotate_paths -- the per-node path breadcrumb that
review.py's compute_unit_labels, akn_export.py, and history-note
attachment (attach_history) all read back to know what a node nests
under."""
from corpus.hierarchy import HIERARCHY_ORDER
from corpus.extract import PageText
from corpus.tree import annotate_paths, attach_history

from conftest import make_node


def _page_with_notes(notes: list[str], page_no: int = 1) -> PageText:
    """A page carrying only margin notes -- attach_history reads nothing
    else off the page, so the body can stay empty."""
    return PageText(page_no=page_no, body="", margin_notes=list(notes))


def test_annotate_paths_tracks_the_current_number_at_each_level():
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "1", "Murder"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[3]["path"]["part"] == "I"
    assert nodes[3]["path"]["section"] == "1"
    assert nodes[3]["path"]["subsection"] == "1"
    assert nodes[3]["path"]["paragraph"] == "a"


def test_annotate_paths_resets_deeper_levels_on_a_new_section():
    nodes = [
        make_node("section", "1", "Murder"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
        make_node("section", "2", "Manslaughter"),
        make_node("subsection", "1", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[4]["path"]["section"] == "2"
    assert nodes[4]["path"]["paragraph"] is None  # not still "a" from section 1


def test_annotate_paths_uses_the_heading_for_a_definition_not_a_number():
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("definition", None, "injury"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[2]["path"]["definition"] == "injury"
    # a definition has no number of its own to occupy the subsection slot
    assert nodes[2]["path"]["subsection"] is None


def test_annotate_paths_does_not_leak_a_definition_into_a_later_section():
    """Regression: path["definition"] used to have no reset rule of its
    own (see annotate_paths's own docstring) because "definition" is
    aliased onto subsection's rank rather than holding a literal slot in
    HIERARCHY_ORDER, so the deeper-levels reset loop never named it. A
    Definitions section anywhere in the Act would leak its last term into
    every later section's own subsections/paragraphs for the rest of the
    document."""
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("definition", None, "injury"),
        make_node("section", "4", "Meaning of consent"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[3]["path"]["definition"] is None
    assert nodes[4]["path"]["definition"] is None


def test_annotate_paths_does_not_leak_a_definition_into_a_later_subsection_in_the_same_section():
    # A section that opens with some definitions and later switches to
    # genuinely numbered subsections (rarer, but not impossible) -- the
    # numbered subsection's own paragraphs shouldn't still be attributed
    # to whichever term was last defined before it.
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("definition", None, "injury"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[3]["path"]["definition"] is None
    assert nodes[3]["path"]["subsection"] == "1"


def test_annotate_paths_respects_a_custom_hierarchy_order():
    custom = ["volume", *HIERARCHY_ORDER]
    nodes = [make_node("volume", "1", "Preliminary"), make_node("section", "1", "Purposes")]
    annotate_paths(nodes, custom)
    assert nodes[1]["path"]["volume"] == "1"


def test_annotate_paths_places_schedule_and_sub_subparagraph_by_default():
    """Both new default levels (see hierarchy.py) participate in the same
    path-tracking with no special-casing needed -- "schedule" resets
    every deeper level the same way "section" does, and
    "sub_subparagraph" is just one level past "subparagraph"."""
    nodes = [
        make_node("section", "1", "Purposes"),
        make_node("schedule", "1", "Forms"),
        make_node("section", "1", "Form of charge-sheet"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
        make_node("subparagraph", "i", None, "text"),
        make_node("sub_subparagraph", "A", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[1]["path"]["section"] is None  # the schedule closed out the earlier section
    assert nodes[2]["path"]["schedule"] == "1"
    assert nodes[6]["path"]["sub_subparagraph"] == "A"
    assert nodes[6]["path"]["subparagraph"] == "i"


def test_a_schedule_note_attaches_to_its_schedule():
    nodes = [
        make_node("section", "1", "Purposes", "text"),
        make_node("schedule", "2", "Forms"),
        make_node("section", "1", "A form", "text"),
    ]
    pages = [_page_with_notes(["Sch. 2 repealed by No. 6958 s. 8(4)(d)."])]

    unattached = attach_history(nodes, pages)

    assert unattached == []
    assert nodes[1]["history"][0]["raw"].startswith("Sch. 2 repealed")


def test_a_schedule_clause_note_does_not_land_on_the_body_section():
    # A Schedule numbers its own clauses from 1 again, so "Sch. 2 cl. 1"
    # and the body's own section 1 share a number.
    nodes = [
        make_node("section", "1", "Purposes", "text"),
        make_node("schedule", "2", "Forms"),
        make_node("section", "1", "A form", "text"),
    ]
    pages = [_page_with_notes(["Sch. 2 cl. 1 amended by No. 47/2016 s. 37."])]

    attach_history(nodes, pages)

    assert "history" not in nodes[0]
    assert nodes[2]["history"][0]["raw"].startswith("Sch. 2 cl. 1")


def test_a_chapter_note_attaches_to_its_chapter():
    nodes = [
        make_node("chapter", "10", "Repeals"),
        make_node("section", "439", "Repeal", "text"),
    ]
    pages = [_page_with_notes(["Ch. 10 (Heading and s. 439) inserted by No. 68/2009 s. 55."])]

    unattached = attach_history(nodes, pages)

    assert unattached == []
    assert nodes[0]["history"][0]["raw"].startswith("Ch. 10")


def test_a_provenance_note_is_kept_but_never_attached():
    # It names section 15 of the *previous consolidation*, not a provision
    # of this Act -- there is nothing here for it to attach to.
    nodes = [make_node("section", "15", "Murder", "text")]
    pages = [_page_with_notes(["No. 6103 s. 15."])]

    unattached = attach_history(nodes, pages)

    assert "history" not in nodes[0]
    assert [n["kind"] for n in unattached] == ["provenance"]
