"""Tests for tree.py's annotate_paths -- the per-node path breadcrumb that
review.py's compute_unit_labels, akn_export.py, and history-note
attachment (attach_history) all read back to know what a node nests
under."""
from ai_pipeline.hierarchy import HIERARCHY_ORDER
from ai_pipeline.tree import annotate_paths

from conftest import make_node


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
    custom = ["chapter", *HIERARCHY_ORDER]
    nodes = [make_node("chapter", "1", "Preliminary"), make_node("section", "1", "Purposes")]
    annotate_paths(nodes, custom)
    assert nodes[1]["path"]["chapter"] == "1"
