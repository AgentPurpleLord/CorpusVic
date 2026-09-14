"""
Works out what a labelled span (see link_annotations.py) actually points
to -- which node defines a "defined_term" span, which Act an
"act_citation" span names -- so the resulting hyperlink knows where to
go. This runs as soon as a reviewer labels a span (see link_review.py's
POST /api/links) rather than later in a separate pass, since everything
needed to resolve it -- which Act, which node -- is already at hand right
then.

Only two label types can actually be resolved right now:

  - act_citation is checked first against corpus/known_acts.yaml --
    the other Acts this pipeline has actually parsed, so the match comes
    with a slug it can link into. If that fails, it falls back to
    corpus/act_registry.py's much bigger but shallower list of Acts
    (taken from the OCPC's own "List of Acts in chronological order" --
    see extract_act_registry.py): there's no parsed content behind it,
    so act_slug stays None, but it confirms the citation names a real
    Act and says whether it's still in force. Both checks require an
    exact match (or an exact match with something extra before it,
    like "the") -- never a fuzzy guess. Linking to the wrong Act would
    be worse than leaving a citation unresolved, and since the year is
    part of the match there's no real ambiguity to weigh anyway.

  - defined_term is checked against this same Act's own "term means ..."
    clauses, reusing definitions.py's extraction -- the same convention
    markdown_export.py already cross-links on, just indexed by node
    position instead of by markdown file and fragment.

bill_reference and em_reference always resolve to None for now, because
Bills and Explanatory Memoranda aren't parsed anywhere else in this
pipeline -- there's nothing yet to check them against. That's expected,
not a bug: a None target just means "check this again once that content
exists," not "this failed."
"""
from pathlib import Path

import yaml

from corpus.act_registry import load_act_registry
from corpus.definitions import extract_section_ref_terms, extract_terms, looks_like_definitions_section
from corpus.hierarchy import UNIT_BOUNDARY_TYPES as _UNIT_BOUNDARY_TYPES
from corpus.hierarchy import UNIT_ROOT_TYPES as _UNIT_ROOT_TYPES

KNOWN_ACTS_PATH = Path(__file__).parent / "known_acts.yaml"


def load_known_acts() -> dict[str, str]:
    """slug -> canonical citation title, e.g. {"crimes-act": "Crimes Act 1958"}."""
    if not KNOWN_ACTS_PATH.exists():
        return {}
    return yaml.safe_load(KNOWN_ACTS_PATH.read_text(encoding="utf-8")) or {}


def resolve_act_citation(text: str) -> dict | None:
    """Checks a citation span's text against the known-Acts list first,
    then the full Act registry (see the module docstring). Allows for a
    leading "the " or surrounding quotes, since a reviewer's drag-select
    often catches those, but otherwise needs the full title, year
    included."""
    normalized = (text or "").strip().strip('"').rstrip(".,;:")
    for slug, title in load_known_acts().items():
        if normalized == title or normalized.endswith(f" {title}"):
            return {"kind": "act", "act_slug": slug, "act_title": title}
    for title, meta in load_act_registry().items():
        if normalized == title or normalized.endswith(f" {title}"):
            return {"kind": "act", "act_slug": None, "act_title": title, **meta}
    return None


def _find_section_by_number(nodes: list[dict], number: str) -> int | None:
    for idx, node in enumerate(nodes):
        if node["type"] in _UNIT_ROOT_TYPES and (node.get("number") or "").lower() == number.lower():
            return idx
    return None


def build_definition_index(nodes: list[dict]) -> dict[str, int]:
    """term (lowercase) -> the position of the node that defines it. Walks
    each Definitions-like Section (itself, plus every node up to the next
    Section/Part/etc.) looking for "term means ..." clauses, then makes a
    second pass for "term has the same meaning as in section N" pointers
    anywhere in the document. Same two-part approach as
    markdown_export.py's collect_definitions, just working on a flat list
    of nodes instead of a tree of markdown pages.

    A node the rules engine already split out as its own "definition"
    (see rule_parser.py's _try_definition_start) already has its term
    stored directly as `heading`, so that's used as-is instead of being
    re-extracted from the text -- a split definition's text starts
    straight at "means ..."/"includes ...", with the term itself no
    longer there for extract_terms to find. Anything not already split
    this way (usually because the term wasn't printed in bold italic)
    still falls back to extract_terms."""
    index: dict[str, int] = {}

    i = 0
    while i < len(nodes):
        node = nodes[i]
        if node["type"] in _UNIT_ROOT_TYPES and looks_like_definitions_section(node.get("heading")):
            j = i
            while j < len(nodes) and (j == i or nodes[j]["type"] not in _UNIT_BOUNDARY_TYPES):
                if nodes[j]["type"] == "definition" and nodes[j].get("heading"):
                    index.setdefault(nodes[j]["heading"].strip().lower(), j)
                else:
                    for term in extract_terms(nodes[j].get("text") or ""):
                        index.setdefault(term, j)
                j += 1
            i = j
            continue
        i += 1

    for node in nodes:
        for terms, section_num in extract_section_ref_terms(node.get("text") or ""):
            target_idx = _find_section_by_number(nodes, section_num)
            if target_idx is not None:
                for term in terms:
                    index[term] = target_idx
    return index


def resolve_defined_term(text: str, nodes: list[dict], definition_index: dict[str, int] | None = None) -> dict | None:
    normalized = (text or "").strip().strip('"').lower()
    index = definition_index if definition_index is not None else build_definition_index(nodes)
    node_index = index.get(normalized)
    if node_index is None:
        return None
    target_node = nodes[node_index]
    return {"kind": "definition", "node_index": node_index, "number": target_node.get("number"), "heading": target_node.get("heading")}


def resolve_link(label: str, text: str, nodes: list[dict], definition_index: dict[str, int] | None = None) -> dict | None:
    """Works out where a newly-labelled span should point, or returns
    None if there's nothing to check it against (wrong text, or -- for
    bill_reference/em_reference -- no Bill/EM content parsed yet). The
    label is saved either way; an unresolved target just means it can't
    become a hyperlink yet, until either the highlighted text is fixed
    or (for Bills/EMs) that content exists and this runs again."""
    if label == "act_citation":
        return resolve_act_citation(text)
    if label == "defined_term":
        return resolve_defined_term(text, nodes, definition_index)
    return None
