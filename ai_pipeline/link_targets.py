"""
Resolves a labelled span (see link_annotations.py) to a concrete target --
which node defines a "defined_term" span, which known Act an
"act_citation" span names -- so the eventual hyperlink knows where to
point. Resolution runs at the moment a span is labelled (see
link_review.py's POST /api/links) rather than as a separate deferred
pass, since the reviewer's own context (this Act, this node) is exactly
what resolution needs and doesn't need to be reconstructed later.

Only act_citation and defined_term have a resolvable data source right
now:

  - act_citation resolves first against ai_pipeline/known_acts.yaml --
    the other Acts this pipeline has actually parsed, so the result
    carries a slug and can be linked *into*. Failing that, it falls back
    to ai_pipeline/act_registry.py's comprehensive-but-shallow Act
    registry (extracted from the OCPC's own "List of Acts in
    chronological order" -- see extract_act_registry.py): no parsed
    content behind it, so act_slug stays None, but it confirms the
    citation names a real Act and reports its current in-force status.
    Both paths are exact-or-suffix text match only, never fuzzy: linking
    to the wrong Act is worse than leaving a citation unresolved, and
    there's no ambiguity to arbitrate once the year is part of the match.

  - defined_term resolves against this same Act's own "term means ..."
    clauses, reusing definitions.py's extraction -- the same convention
    markdown_export.py already cross-links on for reading, just indexed
    by flat node position instead of a markdown file/fragment.

bill_reference and em_reference always resolve to None for now -- Bills
and Explanatory Memoranda aren't parsed by anything in this pipeline, so
there's nothing to resolve against yet. That's not a bug; a None target
just means "resolve this again once that corpus exists," not "resolution
failed."
"""
from pathlib import Path

import yaml

from ai_pipeline.act_registry import load_act_registry
from ai_pipeline.definitions import extract_section_ref_terms, extract_terms, looks_like_definitions_section
from ai_pipeline.hierarchy import UNIT_BOUNDARY_TYPES as _UNIT_BOUNDARY_TYPES
from ai_pipeline.hierarchy import UNIT_ROOT_TYPES as _UNIT_ROOT_TYPES

KNOWN_ACTS_PATH = Path(__file__).parent / "known_acts.yaml"


def load_known_acts() -> dict[str, str]:
    """slug -> canonical citation title, e.g. {"crimes-act": "Crimes Act 1958"}."""
    if not KNOWN_ACTS_PATH.exists():
        return {}
    return yaml.safe_load(KNOWN_ACTS_PATH.read_text(encoding="utf-8")) or {}


def resolve_act_citation(text: str) -> dict | None:
    """Matches a citation span's raw text against the known-Acts registry
    first, then the comprehensive Act registry (see the module
    docstring). Allows the span to have included a leading "the " or
    wrapping quotes (both common in how a reviewer might drag-select a
    citation) but otherwise requires the full title, year included."""
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
    """term (lowercase) -> the flat node_index that defines it. Walks each
    Definitions-like Section's own body (itself plus every node up to the
    next boundary-type node) looking for "term means ..." clauses, then a
    second pass for "term has the same meaning as in section N" pointers
    anywhere at all -- same two-condition approach as markdown_export.py's
    collect_definitions, just flat instead of tree/fragment-based.

    A node the rules engine already split into its own "definition" type
    (see rule_parser.py's _try_definition_start, gated on the same
    looks_like_definitions_section check as this function) carries its
    own term as `heading` directly -- used as-is rather than re-derived
    from body text, since a split definition's own text starts straight
    at "means ..."/"includes ..." with the term itself no longer inline
    for extract_terms's own pattern to find. Anything not already split
    this way (the AI-engine path, or a node whose typesetting didn't
    carry the bold+italic signal) still falls back to extract_terms."""
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
    """Best-effort target for a newly-labelled span, or None if there's
    nothing to resolve against (wrong text, or -- for bill_reference/
    em_reference -- no corpus at all yet). The annotation is saved either
    way; an unresolved target just means it can't be turned into a
    hyperlink until either the highlighted text is fixed or (for Bills/
    EMs) that corpus exists and resolution is re-run."""
    if label == "act_citation":
        return resolve_act_citation(text)
    if label == "defined_term":
        return resolve_defined_term(text, nodes, definition_index)
    return None
