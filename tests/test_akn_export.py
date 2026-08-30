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
