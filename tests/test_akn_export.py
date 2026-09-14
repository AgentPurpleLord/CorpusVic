"""
Tests for ai_pipeline/akn_export.py, including validation against the real
OASIS Akoma Ntoso 3.0 schema (tests/fixtures/akomantoso30.xsd) -- this
project's own export was built against, and repeatedly re-validated
against, that actual schema rather than assumptions about AKN's shape, so
the test suite checks the same thing.
"""
import functools
import xml.etree.ElementTree as ET
from pathlib import Path

import xmlschema

from ai_pipeline.akn_export import build_hierarchy_tree, export_to_akn

from conftest import make_node

SCHEMA_PATH = Path(__file__).parent / "fixtures" / "akomantoso30.xsd"
FINAL_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"


@functools.lru_cache(maxsize=1)
def _schema() -> xmlschema.XMLSchema:
    return xmlschema.XMLSchema(str(SCHEMA_PATH))


def _draft_namespace(schema: xmlschema.XMLSchema) -> str | None:
    return next((ns for ns in schema.namespaces.values() if "WD17" in ns), None)


def assert_valid_akn(tree: ET.ElementTree) -> None:
    """The schema fixture uses AKN's draft namespace (.../akn/3.0/WD17);
    this project's export uses the final namespace (.../akn/3.0) -- same
    namespace swap used throughout this project's own manual validation."""
    xml_text = ET.tostring(tree.getroot(), encoding="unicode")
    schema = _schema()
    draft_ns = _draft_namespace(schema)
    if draft_ns:
        xml_text = xml_text.replace(FINAL_NS, draft_ns)
    errors = list(schema.iter_errors(xml_text))
    assert not errors, "\n".join(str(e)[:300] for e in errors[:5])


def _small_act_nodes() -> list[dict]:
    return [
        make_node("part", "I", "Offences"),
        make_node("division", "1", "Offences against the person"),
        make_node("section", "1", "Murder", ""),
        make_node("subsection", "1", None, "A person who kills another person commits murder."),
        make_node("paragraph", "a", None, "This applies regardless of intent."),
        make_node("section", "2", "Definitions", ""),
        make_node("subsection", "1", None, "weapon means any object capable of causing injury."),
    ]


def test_build_hierarchy_tree_basic_nesting():
    nodes = _small_act_nodes()
    tree_roots, collisions = build_hierarchy_tree(nodes)
    assert collisions == []
    assert len(tree_roots) == 1
    part = tree_roots[0]
    assert part["node"]["type"] == "part"
    division = part["children"][0]
    assert division["node"]["type"] == "division"
    assert [c["node"]["type"] for c in division["children"]] == ["section", "section"]
    section1 = division["children"][0]
    assert section1["children"][0]["node"]["type"] == "subsection"
    assert section1["children"][0]["children"][0]["node"]["type"] == "paragraph"


def test_build_hierarchy_tree_eid_collision_is_disambiguated_not_dropped():
    """A "Definitions" section can hold several independent unnumbered
    (a)/(b) lists directly under it (one per defined term), which the
    source text itself doesn't distinguish -- both legitimately produce
    the same eId token. This must be flagged, not silently merged or
    silently overwritten."""
    nodes = [
        make_node("part", "I", "Preliminary"),
        make_node("section", "2", "Definitions", ""),
        make_node("paragraph", "a", None, "first term's first branch"),
        make_node("paragraph", "b", None, "first term's second branch"),
        make_node("paragraph", "a", None, "second term's first branch"),
        make_node("paragraph", "b", None, "second term's second branch"),
    ]
    tree_roots, collisions = build_hierarchy_tree(nodes)
    assert collisions, "expected at least one eId collision to be flagged"
    section = tree_roots[0]["children"][0]
    eids = [c["eid"] for c in section["children"]]
    assert len(eids) == len(set(eids)), "colliding eIds must still end up unique"


def test_export_to_akn_validates_against_real_schema():
    parsed = {"nodes": _small_act_nodes(), "act": "test-act"}
    tree = export_to_akn(parsed)
    assert_valid_akn(tree)


def test_export_to_akn_with_history_and_notes_validates():
    """History events and notes exercise <lifecycle>/<passiveModifications>
    and the note-as-inline-annotation path -- both needed real schema
    fixes during development (missing FRBRdate/FRBRauthor, <note> not
    supporting href), so keep validating them together."""
    nodes = _small_act_nodes()
    nodes[2]["history"] = [{"raw": "S. 1 amended by No. 52/2014 s. 11."}]
    nodes.append(make_node("note", "1", None, "See also section 5 for related provisions."))
    parsed = {"nodes": nodes, "act": "test-act"}
    tree = export_to_akn(parsed)
    assert_valid_akn(tree)


def test_export_to_akn_with_a_reviewer_defined_custom_type_validates():
    """A type a reviewer added in the GUI (see review.py's node-type
    endpoints) isn't a hierarchy level and has no native AKN element, so
    it must fall through to the generic <hcontainer name="..."> escape
    hatch rather than producing an element the schema doesn't know."""
    nodes = _small_act_nodes()
    nodes.append(make_node("penalty", None, None, "Level 1 imprisonment."))
    parsed = {"nodes": nodes, "act": "test-act"}
    tree = export_to_akn(parsed)
    assert_valid_akn(tree)
    assert tree.find(f".//{{{FINAL_NS}}}hcontainer[@name='penalty']") is not None


def test_export_to_akn_of_a_bill_validates():
    """A Bill's top-level provisions are "clause" nodes, not "section"
    (see run_pipeline.py's top_level_type) -- which had no eId prefix and
    no element at all, so exporting a parsed Bill raised outright."""
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("clause", "1", "Purposes", "The purposes of this Bill are—"),
        make_node("paragraph", "a", None, "to do a thing; and"),
        make_node("clause", "2", "Commencement", "This Bill comes into operation on Royal Assent."),
    ]
    tree = export_to_akn({"nodes": nodes, "act": "test-bill"})
    assert_valid_akn(tree)
    assert tree.find(f".//{{{FINAL_NS}}}clause") is not None


# ---------------------------------------------------------------------
# Penalties attach to the provision that creates the offence
# ---------------------------------------------------------------------

def _tree_eids(tree_roots):
    found = []
    def walk(node):
        found.append((node["node"]["type"], node["eid"]))
        for child in node["children"]:
            walk(child)
    for root in tree_roots:
        walk(root)
    return found


def test_a_penalty_attaches_to_its_section_not_to_the_list_above_it():
    """A penalty is printed after the whole provision, so whatever was
    open when the parser reached it is the deepest thing in the list
    above. Left alone, s 1's penalty came out under paragraph (b), where
    nothing looking for the section's penalty would find it."""
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "1", "Murder", "A person who—"),
        make_node("paragraph", "a", None, "does this; or"),
        make_node("paragraph", "b", None, "does that;"),
        make_node("continuation", None, None, "is guilty of an indictable offence."),
        make_node("penalty", None, None, "Penalty: Level 2 imprisonment (25 years maximum)."),
    ]
    tree_roots, _collisions = build_hierarchy_tree(nodes)

    eid = next(e for t, e in _tree_eids(tree_roots) if t == "penalty")
    assert eid == "part_i__sec_1__pnlty_1"


def test_a_penalty_stays_under_the_subsection_that_creates_the_offence():
    """It pops back past list items and the wrap-up, and no further: a
    penalty under subsection (1) belongs to subsection (1), not to the
    section as a whole."""
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "9", "Wilful damage", ""),
        make_node("subsection", "1", None, "A person who—"),
        make_node("paragraph", "a", None, "does this;"),
        # depth_rank is where the parser actually put it -- a wrap-up
        # closing a subsection's (a)/(b) list sits at the list's own
        # depth, inside that subsection (see rule_parser's
        # _consume_as_continuation).
        dict(make_node("continuation", None, None, "shall be guilty of an offence."), depth_rank=7),
        make_node("penalty", None, None, "Penalty: 25 penalty units."),
        make_node("subsection", "1A", None, "In any proceedings for an offence against subsection (1)..."),
    ]
    tree_roots, _collisions = build_hierarchy_tree(nodes)

    eid = next(e for t, e in _tree_eids(tree_roots) if t == "penalty")
    assert eid == "part_i__sec_9__subsec_1__pnlty_1"


def test_a_penalty_does_not_swallow_what_comes_after_it():
    """Popping the stack for the penalty must not leave the next
    subsection nested inside it."""
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "9", "Wilful damage", ""),
        make_node("subsection", "1", None, "An offence."),
        make_node("penalty", None, None, "Penalty: 25 penalty units."),
        make_node("subsection", "2", None, "Another thing."),
    ]
    tree_roots, _collisions = build_hierarchy_tree(nodes)

    section = tree_roots[0]["children"][0]
    assert [c["node"].get("number") for c in section["children"]] == ["1", "2"]


def test_an_act_with_a_penalty_still_validates_against_the_real_schema():
    nodes = _small_act_nodes() + [
        make_node("penalty", None, None, "Penalty: Level 2 imprisonment (25 years maximum)."),
    ]
    assert_valid_akn(export_to_akn({"nodes": nodes, "act": "test-act"}))


def test_a_table_exports_as_a_real_table():
    """Its rows are stored as text, and reflow would join them into one
    paragraph -- throwing away the one thing a table is, and putting the
    rows back into exactly the state they were recovered from."""
    nodes = _small_act_nodes() + [
        make_node("table", None, "Table", "Column 1 | Column 2\nan offence | a defence"),
    ]
    tree = export_to_akn({"nodes": nodes, "act": "test-act"})
    assert_valid_akn(tree)

    root = tree.getroot()
    assert "an offence | a defence" not in ET.tostring(root, encoding="unicode")
    assert [e.tag.rsplit("}", 1)[-1] for e in root.iter() if e.tag.endswith("}tr")] == ["tr", "tr"]
    cells = [e.text for e in root.iter() if e.tag.endswith("}p")]
    assert "Column 1" in cells and "a defence" in cells


def test_a_continuation_becomes_its_provisions_wrap_up():
    """A continuation is not a thing of its own: it is the rest of the
    provision's sentence, resumed after the list it broke into. AKN says
    so with <wrapUp> -- the counterpart of the <intro> the provision
    opened with. As a sibling <hcontainer name="continuation"> it read as
    a separate provision that happened to sit beside the paragraphs.
    Criminal Procedure Act s 11(1) is the shape."""
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "11", "Place of hearing", ""),
        make_node("subsection", "1", None, "is to be heard at the venue nearest to-"),
        make_node("paragraph", "a", None, "the place where the offence was committed; or"),
        make_node("paragraph", "b", None, "the place of residence of the accused-"),
        dict(make_node("continuation", None, None, "except where otherwise provided."), depth_rank=7),
    ]
    tree = export_to_akn({"nodes": nodes, "act": "test-act"})
    assert_valid_akn(tree)

    root = tree.getroot()
    subsection = next(e for e in root.iter() if e.tag.endswith("}subsection"))
    assert [c.tag.rsplit("}", 1)[-1] for c in subsection] == [
        "num", "intro", "paragraph", "paragraph", "wrapUp",
    ]
    assert subsection[-1][0].text == "except where otherwise provided."
    assert 'name="continuation"' not in ET.tostring(root, encoding="unicode")


def test_a_provision_holding_nothing_but_a_continuation_keeps_it():
    """<wrapUp> is only legal after at least one nested hierarchy
    element. The parser cannot produce this, but a reviewer's merges and
    deletions can, and losing the text would be worse than an odd
    element."""
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "11", "Place of hearing", "lead-in-"),
        make_node("continuation", None, None, "the tail that is all that is left."),
    ]
    tree = export_to_akn({"nodes": nodes, "act": "test-act"})
    assert_valid_akn(tree)
    assert "the tail that is all that is left." in ET.tostring(tree.getroot(), encoding="unicode")
