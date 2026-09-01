"""
Local web GUI to human-verify the AI's structural parse of an Act, and to
tag spans of its text that should become links -- one merged tool, where
link_review.py and review.py's own CLI used to be two separate ones.

Usage:
    python review.py crimes-act
    python review.py crimes-act --port 8001

Then open the printed URL (http://127.0.0.1:8000/ by default). A Section's
own lead-in text plus every Subsection/Paragraph/Subparagraph/Note/
Definition nested under it are shown together as one reviewable unit,
colour-coded by type -- a Section and its own components are what a
reviewer actually needs to see side by side to judge whether the parser
attached each piece to the right place. Standalone structural nodes that
aren't a Section's own content (Part/Division/Subdivision headings, bare
topical headings) are their own single-piece unit, same idea.

Per piece, the toolbar offers:
  - Edit -- change its type/number/heading, or its text body outright.
  - Merge -- pick a destination piece first (the one that keeps the
    combined text), then which piece(s) feed into it: another piece
    still in this unit, or an already-reviewed piece from an earlier
    one. For when the parser wrongly broke a paragraph into extra
    fragments (most often a stray heading/section boundary mistaken
    mid-paragraph, e.g. a multi-line bold Act-name citation, or a long
    comma-separated list of bracketed cross-references -- "(8A), (8B),
    (8C), (8D)..." -- that happens to wrap onto its own PDF line right at
    "(8C)", which is then indistinguishable by shape alone from a genuine
    new Subsection (8C)). Merging that piece away also repairs every
    later sibling's own stored numbering that inherited its bogus number
    (see _repair_cascaded_path) -- otherwise those pieces keep showing a
    label like "(8C)(ii)(iii)" even after the offending piece itself is
    gone, since the rule parser bakes each node's full ancestry into it
    at parse time and nothing else in this tool ever revisits it.
  - Drag-select a span of a piece's own text to either split it there
    (the tail reassigns to another piece the same way Merge's
    destination picker works) or label it as a link -- an Act citation,
    a defined term, a Bill/EM reference -- which also attempts to
    *resolve* it immediately (ai_pipeline/link_targets.py): an
    act_citation against ai_pipeline/known_acts.yaml (falling back to
    the comprehensive Act registry), a defined_term against this Act's
    own definitions. Text displays reflowed (the source PDF's own line
    wraps joined into flowing prose) for reading, independent of the
    underlying stored text a span's offsets index into.

The sidebar lists every unit with its own review status (pending/done/
flagged) and a filter to show only flagged ones (former --triage) or
jump anywhere out of order; the flat, one-node-at-a-time view former
--flat gave has no separate mode here since group_into_units already
gives a Part/Division/heading_group its own single-piece unit and every
piece within a Section is already individually addressable.

Accepting or flagging a unit writes it into data/legislation.db and logs
each decision (the AI's original guess vs. what a human approved) there
too, which future run_pipeline.py runs read back in as few-shot examples
-- so the parser is meant to get better at this over time, without any
fine-tuning step. An edit made directly to an already-reviewed piece
(browsing back to fix something) persists and logs immediately, since
there's no later Accept step to do it for. Progress is saved
continuously, so the server can be stopped and restarted from wherever
it left off (see ai_pipeline/db.py for why this data -- and only this
data, not the regenerable data/ai_parsed/<act>.json -- moved off plain
JSON files).

Labelled link spans are saved the moment they're labelled -- independent
of structural review above, since annotating a span doesn't require (or
imply) that its node has passed
review, and structural review doesn't need to know these exist.

A "Show source PDF" toggle in the header renders the actual source page
each piece came from (page_start on the piece, GET /api/pages/{n}.png --
a plain PyMuPDF rasterisation of that page, cached in memory) alongside
or in place of the parsed text, three view modes cycled by the one
button: text only, side-by-side split, PDF only. This is read-only --
purely a check against the real page, nothing here feeds back into the
parse -- available only when data/ai_parsed/<act>.json still has the
source PDF at the path it was parsed from (see load_source_pdf_path);
missing entirely otherwise rather than a toggle that always errors.
"""
import argparse
import bisect
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import fitz
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ai_pipeline import db
from ai_pipeline.examples_store import add_correction, stats
from ai_pipeline.hierarchy import UNIT_BOUNDARY_TYPES, UNIT_ROOT_TYPES, make_ranks
from ai_pipeline.link_annotations import LABELS, LinkError, add_link, delete_link, load_links
from ai_pipeline.link_targets import build_definition_index, resolve_link
from ai_pipeline.schema import NODE_TYPES

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Pure logic shared with the test suite (tests/test_review.py) -- no I/O,
# no FastAPI, unit-testable on its own.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC, ISO 8601, always "+00:00" -- so verification timestamps sort
    correctly as plain strings (used by markdown_export.py to find the most
    recent one across a Section's pieces without parsing dates)."""
    return datetime.now(timezone.utc).isoformat()


def load_parsed(act: str):
    path = Path("data/ai_parsed") / f"{act}.json"
    if not path.exists():
        raise SystemExit(f"No AI-parsed output found at {path} -- run run_pipeline.py first.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["nodes"], data.get("unattached_notes", []), data.get("hierarchy", [])


load_verified = db.load_verified
save_verified = db.save_verified


def load_diagnostics(act: str) -> list[dict]:
    path = Path("data/diagnostics") / f"{act}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_source_pdf_path(act: str) -> str | None:
    """The source PDF path run_pipeline.py/run_em_pipeline.py stamped into
    data/ai_parsed/<act>.json (relative to the repo root, since that's
    where those scripts are run from) -- used to show the actual page a
    piece came from during review (see the /api/pages/{page_no}.png
    endpoint). None if this Act's parsed output predates that field, or
    the PDF has since moved/been deleted -- the page-image endpoint 404s
    in that case rather than the server failing to start."""
    path = Path("data/ai_parsed") / f"{act}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("source")


def build_current_nodes(act: str) -> tuple[list[dict], list[dict], list[str]]:
    """The same "verified where committed, original parser output
    otherwise" merge the live review server keeps in memory via
    _current_node/_verified_by_source_index/_merged_away (see main()'s own
    reconstruction of that state at startup) -- but as a pure, one-shot
    read straight off disk, for a read-only consumer that has no reason to
    hold a whole server process open just to see the Act's current state.
    Used by the live HTML browsing view (ai_pipeline/html_view.py) so a
    reviewer's in-progress edits show up immediately, without waiting for
    an export step; akn_export.py/markdown_export.py could use this too
    instead of their own all-or-nothing verified-vs-ai_parsed choice, but
    that's a separate change from introducing it here.

    Returns (nodes, unattached_notes, hierarchy) in the same shape as
    load_parsed, with merged-away nodes simply absent, so any consumer
    that already builds a hierarchy tree from load_parsed's output works
    unchanged against this instead."""
    nodes, unattached_notes, hierarchy = load_parsed(act)
    units = group_into_units(nodes)
    verified = load_verified(act)
    verified_by_source_index = {v["_source_node_index"]: v for v in verified if "_source_node_index" in v}
    resume_unit = _resume_point(units, list(verified))

    merged_away: set[int] = set()
    for u in range(resume_unit):
        for i in units[u]:
            if i not in verified_by_source_index:
                merged_away.add(i)

    current_nodes = [verified_by_source_index.get(i, node) for i, node in enumerate(nodes) if i not in merged_away]
    return current_nodes, unattached_notes, hierarchy


_WRAP_RE = re.compile(r"\s*\n\s*")


def reflow_with_map(text: str) -> tuple[str, list[int]]:
    """The stored text's "\\n"s are just the source PDF's own line-wrap
    points, not paragraph breaks -- displaying them raw makes every piece
    look like a jagged list of half-sentences. Collapses each wrap into a
    single space for display, same transform review.py's old CLI-only
    _reflow did, but also returns the raw-offset each reflowed character
    came from (one longer than the reflowed text, for the position just
    past its last character) -- callers use this to translate a browser
    text selection made against the *displayed* string back into an
    offset into the *stored* one (what link/split actions actually index
    into), and to place an already-saved link's raw-offset span back onto
    the reflowed text for highlighting."""
    text = text or ""
    chars: list[str] = []
    offsets: list[int] = []
    pos = 0
    for m in _WRAP_RE.finditer(text):
        for j in range(pos, m.start()):
            chars.append(text[j])
            offsets.append(j)
        chars.append(" ")
        offsets.append(m.start())
        pos = m.end()
    for j in range(pos, len(text)):
        chars.append(text[j])
        offsets.append(j)
    offsets.append(len(text))

    start = 0
    while start < len(chars) and chars[start].isspace():
        start += 1
    end = len(chars)
    while end > start and chars[end - 1].isspace():
        end -= 1

    result_chars = chars[start:end]
    result_offsets = offsets[start:end] + [offsets[end]]
    return "".join(result_chars), result_offsets


def _raw_to_reflowed(raw_offset: int, offset_map: list[int]) -> int:
    return bisect.bisect_left(offset_map, raw_offset)


# A Section's own lead-in text plus everything nested under it (Subsection/
# Paragraph/Subparagraph/Note/Definition) forms one review unit; every other
# node type is a boundary that starts (and, for Part/Division/Subdivision/
# heading_group, immediately ends) its own single-node unit. This mirrors
# exactly how the rules engine's own stack nests things -- see
# ai_pipeline/rule_parser.py's HIERARCHY_ORDER -- without needing to
# reconstruct the full tree (build_hierarchy_tree in akn_export.py) just to
# find "everything under this Section": the flat node list is already in
# document order, so a single pass is enough. UNIT_ROOT_TYPES also
# includes "clause" -- a Bill's pre-enactment name for the same top-level
# provision an Act calls a "section" (same nesting rank, see
# hierarchy.py) -- so it starts a review unit the exact same way.
# hierarchy.py's UNIT_BOUNDARY_TYPES also includes "chapter", the optional
# top level above Part a profile can opt into (see its own docstring).
_UNIT_BOUNDARY_TYPES = UNIT_BOUNDARY_TYPES
_UNIT_ROOT_TYPES = UNIT_ROOT_TYPES


def group_into_units(nodes: list[dict]) -> list[list[int]]:
    units: list[list[int]] = []
    current: list[int] | None = None
    for i, node in enumerate(nodes):
        t = node["type"]
        if t in _UNIT_ROOT_TYPES:
            current = [i]
            units.append(current)
        elif t in _UNIT_BOUNDARY_TYPES:
            current = None
            units.append([i])
        elif current is not None:
            current.append(i)
        else:
            units.append([i])
    return units


def compute_unit_tree_info(unit_root_types: list[str], hierarchy_order: list[str]) -> list[dict]:
    """depth (0 = top-level) and parent_unit_no (None for top-level) for
    every review unit, computed from just its own root node's type, in
    the same open/close-stack style build_hierarchy_tree uses for
    individual nodes -- one level per *unit* here instead of per node,
    since a unit's own root is always exactly one of the boundary types
    that stack already understands (chapter/part/division/subdivision/
    section/clause) or a heading_group. Powers the sidebar's collapsible
    tree view: a Part collapses every unit nested under it, transitively,
    by parent_unit_no chaining up to it.

    heading_group is the one boundary type with no rank of its own (a
    bare topical heading, not a real container) -- it doesn't push
    anything onto the stack, so it nests at whatever depth the stack is
    currently at (the same depth a Section would have there), and a unit
    can still be *its* child if the next real container hasn't opened
    yet."""
    rank = make_ranks(hierarchy_order)
    stack: list[tuple[int, int]] = []  # (rank, unit_no), shallowest last-popped first
    info = []
    for unit_no, root_type in enumerate(unit_root_types):
        if root_type == "heading_group":
            parent = stack[-1][1] if stack else None
            info.append({"depth": len(stack), "parent_unit_no": parent})
            continue
        r = rank.get(root_type, len(hierarchy_order))
        while stack and stack[-1][0] >= r:
            stack.pop()
        parent = stack[-1][1] if stack else None
        info.append({"depth": len(stack), "parent_unit_no": parent})
        stack.append((r, unit_no))
    return info


# Renesting a piece means "make it this other piece's own direct child" --
# expressed as a type change (the child rank exactly one level deeper than
# the target), not a position change: see renest_endpoint's own docstring
# for why document order is left untouched. "subparagraph" and the non-
# hierarchy types (note/repealed/heading_group) have no entry -- nothing
# can be renested to become *their* child.
NEST_CHILD_TYPE = {
    "section": "subsection",
    "clause": "subsection",
    "subsection": "paragraph",
    "definition": "paragraph",
    "paragraph": "subparagraph",
}


def can_renest_under(unit_types: list[str], target_pos: int, node_pos: int, hierarchy_order: list[str]) -> bool:
    """Whether the piece at unit-local position node_pos can be renested
    to become the direct child of the piece at target_pos, given every
    piece's type in this unit (root first, in document order).

    Renesting only changes the dragged piece's own type; its new parent
    is *inferred* afterwards, the same way every path is -- from document
    order, by _recompute_unit_paths (which reruns tree.py's annotate_paths
    algorithm for the unit). That inference always resolves to whichever
    piece of the target's own type -- or anything shallower -- most
    recently precedes the dragged piece. So this is only unambiguous when
    nothing of that rank sits between the two: otherwise the piece would
    silently end up nested under that other, closer piece instead of the
    one actually dropped onto, which would be a confusing bait-and-switch
    for whoever just dragged it there."""
    rank = make_ranks(hierarchy_order)
    target_rank = rank.get(unit_types[target_pos])
    if target_rank is None:
        return False
    return not any(t in rank and rank[t] <= target_rank for t in unit_types[target_pos + 1 : node_pos])


def _resume_point(units: list[list[int]], verified: list[dict]) -> int:
    """Which unit to resume at. commit_unit tags the last node it appends
    for a unit with that unit's own index (`_unit_end_index`); when any
    such marker is present, the highest one is trusted directly -- this is
    the only reliable signal once a merge has been used, since merging a
    wrongly-split-off piece away means commit_unit can append *fewer*
    nodes than that unit's original size, and the plain node count below
    can no longer tell "unit N committed short because of a merge" apart
    from "review stopped partway through unit N".

    Absent any marker (verified data from a version of this tool from
    before that marker existed), fall back to the original approach:
    `verified` should hold exactly `len(verified)` nodes' worth of *whole*
    units, since a marker-free run always commits a unit at its full
    original size."""
    marked = [n["_unit_end_index"] for n in verified if "_unit_end_index" in n]
    if marked:
        return max(marked) + 1

    cumulative = 0
    boundary_units = 0
    for u in units:
        if cumulative + len(u) > len(verified):
            break
        cumulative += len(u)
        boundary_units += 1
    if cumulative != len(verified):
        del verified[cumulative:]
    return boundary_units


def compute_unit_labels(unit_nodes: list[dict]) -> list[str]:
    """One label per node in the unit (index 0 is the Section itself,
    labelled "SECTION"), shown alongside each piece for the reviewer's own
    reference. A Subsection/Paragraph/Subparagraph's own legislative
    numbering is unique by construction, so its path-derived chain
    ("(1)(a)") is used directly. A Definition has no numbering of its own
    either, but does carry its own defined term as its heading (see
    rule_parser.py's _try_definition_start) -- shown bare, since the term
    itself is exactly what a reviewer needs to pick it out by, and
    "Definitions" sections name each of theirs uniquely by construction
    (the same term can't be defined twice). Anything else (Note, a stray
    heading_group) has no numbering *or* a useful heading of its own --
    these commonly share the exact same inherited path context (several
    repealed-text "* * * *" markers in a row all sitting right after the
    same last-numbered piece), so a naive path-based label would collide
    between them and even with the real numbered piece they're attached
    to. These get a running per-type counter instead. A final
    de-duplication pass guards against a genuine collision anyway (e.g. a
    mis-parsed repeated number, or two same-named terms redefined in
    separate Definitions sections that both landed in one review unit).

    A Subsection/Paragraph/Subparagraph nested under a Definition (a
    Definitions section's own "term means— (a) ...; (b) ...;" lists) has
    no subsection number to anchor its own chain to -- path["definition"]
    (see tree.py's annotate_paths) carries the term itself instead, so
    it's prefixed onto the chain there specifically to disambiguate: a
    section with a hundred definitions each with their own bare "(a)"
    list would otherwise show a hundred identical "(a)" labels with no
    way to tell which definition any of them belongs to."""
    labels = ["SECTION"]
    counters: dict[str, int] = {}
    for node in unit_nodes[1:]:
        if node["type"] in ("subsection", "paragraph", "subparagraph") and node.get("number"):
            path = node.get("path") or {}
            chain = "".join(f"({path[level]})" for level in ("subsection", "paragraph", "subparagraph") if path.get(level))
            chain = chain or f"({node['number']})"
            if path.get("definition"):
                chain = f"{path['definition']} {chain}"
            labels.append(chain)
        elif node["type"] == "definition" and node.get("heading"):
            labels.append(node["heading"])
        else:
            counters[node["type"]] = counters.get(node["type"], 0) + 1
            labels.append(f"[{node['type']} {counters[node['type']]}]")
    seen: dict[str, int] = {}
    for i, label in enumerate(labels):
        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            labels[i] = f"{label}#{seen[label]}"
    return labels


def commit_unit(
    unit_nodes: list[dict], unit_orig: list[dict], act: str, verified: list[dict], flagged: bool = False, unit_index: int | None = None
) -> None:
    """Appends every node in the unit to `verified` and logs one correction
    per node -- same per-node granularity the correction log has always
    had, just decided on in one batch instead of one prompt per node.

    Every accepted/edited node is stamped with when a human confirmed it --
    markdown_export.py reads this back to flag human-verified content in
    each page's front matter. A flagged node explicitly isn't confirmed
    (that's what flagging means -- "not sure, revisit this"), so it's kept
    unstamped even though the reviewer looked at it.

    unit_index, when given, is this unit's own position in group_into_
    units's own list -- tagged onto the last node appended here so
    _resume_point can find exactly where review left off even when a
    merge has made this commit shorter than the unit's original size
    (unit_nodes/unit_orig can have had pieces merged away by the time
    commit_unit is called -- see the server's own merge endpoint)."""
    for original, current in zip(unit_orig, unit_nodes):
        node = dict(current)
        if flagged:
            node["needs_followup"] = True
        else:
            node["verified_at"] = _now_iso()
        verified.append(node)
        changed = any(node.get(k) != original.get(k) for k in ("type", "number", "heading", "text"))
        add_correction(act, ai_output=original, human_output=node, changed=changed)
    if unit_index is not None and verified:
        verified[-1]["_unit_end_index"] = unit_index


# ---------------------------------------------------------------------------
# Server state -- one Act per running process (see the module docstring's
# usage), so there's no per-request act parameter to plumb through.
# ---------------------------------------------------------------------------

_act: str | None = None
_nodes: list[dict] = []
_units: list[list[int]] = []
_unit_of_index: dict[int, int] = {}
_verified: list[dict] = []
_verified_by_source_index: dict[int, dict] = {}
_pending_edits: dict[int, dict] = {}
_merged_away: set[int] = set()
_merged_into_unit: dict[int, int] = {}  # source unit_no -> the unit_no its content ended up in (this session only)
_definition_index: dict[str, int] = {}
_findings_by_node: dict[int, list[dict]] = {}
_unattached_notes: list[dict] = []  # startup snapshot, plus anything detach_history_endpoint has since returned to it (this session only)
_hierarchy: list[str] = []
_relabel_types: list[str] = []
_startup_resume_unit = 0
_source_pdf_path: str | None = None
_act_title: str | None = None
_pdf_doc: "fitz.Document | None" = None
_page_image_cache: dict[int, bytes] = {}
_PAGE_RENDER_ZOOM = 1.8  # ~130 DPI -- legible after the browser scales the <img> to fit its panel


def _current_node(i: int) -> dict:
    """Node i as this session currently sees it: a pending (not yet
    Accepted/Flagged) edit first, else its already-reviewed state if the
    unit containing it has been committed, else the original AI parse."""
    if i in _pending_edits:
        return _pending_edits[i]
    if i in _verified_by_source_index:
        return _verified_by_source_index[i]
    return _nodes[i]


def _is_committed(i: int) -> bool:
    return i in _verified_by_source_index


def _mutate_node(i: int, **fields) -> dict:
    """Applies field updates to node i's current effective state. If i's
    unit has already been committed, mutates the verified entry directly,
    refreshes its verification stamp, logs a correction against its prior
    state, and persists immediately -- there's no later Accept step to do
    it for us once a piece has already been reviewed once. Otherwise
    stages the change in _pending_edits, folded into `verified` for real
    only when that unit is Accepted/Flagged."""
    current = _current_node(i)
    merged = {**current, **fields}
    if _is_committed(i):
        original_snapshot = dict(current)
        target = _verified_by_source_index[i]
        target.clear()
        target.update(merged)
        target["verified_at"] = _now_iso()
        target.pop("needs_followup", None)
        add_correction(_act, ai_output=original_snapshot, human_output=target, changed=True)
        save_verified(_act, _verified)
        return target
    _pending_edits[i] = merged
    return merged


def _append_text_to_node(i: int, addition: str) -> dict:
    if not addition:
        return _current_node(i)
    current_text = (_current_node(i).get("text") or "").strip()
    new_text = f"{current_text}\n{addition}" if current_text else addition
    return _mutate_node(i, text=new_text)


_CASCADING_PATH_LEVELS = ("subsection", "paragraph", "subparagraph")


def _patch_path_level(i: int, level: str, value: str | None) -> None:
    """Updates just one key of node i's own stored `path` dict, in
    whichever state currently represents it -- deliberately *not* routed
    through _mutate_node: see _repair_cascaded_path for why this needs to
    bypass the usual re-stamp-and-log-a-correction behaviour that applies
    to an actual reviewed change."""
    if i in _pending_edits:
        node = _pending_edits[i]
    elif i in _verified_by_source_index:
        node = _verified_by_source_index[i]
    else:
        node = _nodes[i]
    node["path"] = {**(node.get("path") or {}), level: value}


def _repair_cascaded_path(removed_index: int, removed_node: dict, target_path: dict) -> None:
    """A line the rules engine wrongly classifies as opening a new
    Subsection/Paragraph/Subparagraph (most often a bracketed citation
    like "(8C)" that only looks like one because a long comma-separated
    list happened to wrap onto its own PDF line -- see the "8C" example
    in this module's own docstring) doesn't just misfile *that* line: the
    rule parser stores each node's full ancestry as a `path` dict at parse
    time, and every sibling parsed *after* the bogus boundary inherits its
    wrong number at that same level in their own stored `path`, all the
    way until a genuinely different value at that level closes the run --
    which is exactly what turns into a confusing "(8C)(ii)(iii)"-style
    label on pieces that come *after* the offending one, not just on it.

    Merging the offending piece away (see merge_endpoint) undoes its own
    presence but leaves every inheriting sibling's `path` uncorrected on
    its own -- this walks forward from it, within the same unit, fixing
    exactly the nodes whose `path[level]` still says the removed piece's
    own number, restoring what it should be instead (the surviving
    target's own value at that level). Stops at the first node that
    doesn't match: that's either a value the rules engine actually got
    right on its own, or the corrupted run was never there to begin with
    (an ordinary merge of two already-correctly-labelled pieces, say) --
    either way, nothing to touch.

    Deliberately not run through _mutate_node/add_correction: this
    repairs internal bookkeeping a parsing mistake left behind, not a
    reviewed change to any of these pieces' actual content, so it
    shouldn't re-stamp verified_at or add noise to the correction log."""
    level = removed_node["type"]
    removed_number = removed_node.get("number")
    if level not in _CASCADING_PATH_LEVELS or removed_number is None:
        return
    restore_value = target_path.get(level)
    unit_no = _unit_of_index.get(removed_index)
    if unit_no is None:
        return
    touched_committed = False
    for i in _units[unit_no]:
        if i <= removed_index or i in _merged_away:
            continue
        path = _current_node(i).get("path") or {}
        if path.get(level) != removed_number:
            break
        _patch_path_level(i, level, restore_value)
        touched_committed = touched_committed or i in _verified_by_source_index
    if touched_committed:
        save_verified(_act, _verified)


_NESTABLE_LEVELS = ("subsection", "paragraph", "subparagraph", "definition")


def _recompute_unit_paths(unit_no: int) -> None:
    """Rebuilds path[level] for subsection/paragraph/subparagraph/
    definition across every (non-merged-away) piece in this unit, in
    current document order -- the exact algorithm tree.py's annotate_paths
    runs once for the whole document at parse time, just re-run here for
    one unit after renest_endpoint changes a piece's type (a type change
    is exactly the kind of thing annotate_paths needs to see to place a
    piece -- and everything *after* it in the unit -- under the right
    parent). The unit's own shallower levels (chapter/part/.../section)
    never change from a renest, so the root's own already-correct path is
    carried forward unmodified; only the four nestable levels are reset
    and replayed.

    "definition" gets its own explicit reset, same as tree.py's
    annotate_paths does and for the same reason: it's aliased onto
    subsection's own rank rather than holding a literal slot in
    _hierarchy, so the deeper-levels loop below never names it as one of
    the keys it clears. Without this, a Definitions section followed
    later in the *same* unit by a genuine numbered subsection would leak
    the last term's own heading into that subsection's path forever."""
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    if len(indices) < 2:
        return
    rank = make_ranks(_hierarchy)
    definition_rank = rank.get("definition")
    current = {**(_current_node(indices[0]).get("path") or {}), **dict.fromkeys(_NESTABLE_LEVELS)}
    touched_committed = False
    for i in indices[1:]:
        node = _current_node(i)
        t = node.get("type")
        if t in rank:
            if t == "definition":
                current["definition"] = node.get("heading")
            else:
                current[t] = node.get("number")
                if definition_rank is not None and rank[t] <= definition_rank:
                    current["definition"] = None
            for deeper in _hierarchy[rank[t] + 1 :]:
                current[deeper] = None
        node["path"] = dict(current)
        touched_committed = touched_committed or i in _verified_by_source_index
    if touched_committed:
        save_verified(_act, _verified)


def _unit_status(unit_no: int) -> str:
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    if not indices:
        return "done"  # every node in it ended up merged away into elsewhere
    if not all(_is_committed(i) for i in indices):
        return "pending"
    if any(_verified_by_source_index[i].get("needs_followup") for i in indices):
        return "flagged"
    return "done"


def _maybe_mark_unit_complete(unit_no: int) -> None:
    """If every node in this unit (that wasn't merged away) is now
    committed, stamps _unit_end_index on the last one -- the same marker
    a whole-unit Accept/Flag stamps via commit_unit, so _resume_point can
    trust it exactly the same way regardless of whether this unit was
    finished by one whole-unit action or by a reviewer individually
    accepting/flagging each of its pieces one at a time (see
    accept_node/accept_unit) with the last one landing here."""
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    if indices and all(_is_committed(i) for i in indices):
        _verified_by_source_index[indices[-1]]["_unit_end_index"] = unit_no


def _accept_node(i: int, flagged: bool) -> dict:
    """Commits node i's current state (a pending edit if any, else the
    original parse) as individually reviewed -- the same per-node
    stamping commit_unit's own loop does, just for one piece at a time so
    a reviewer isn't forced to accept/flag a whole Section's worth of
    Subsections in one all-or-nothing action. Safe to call again on an
    already-committed node (e.g. flagging it after having accepted it, or
    vice versa): updates its verification status in place rather than
    appending a duplicate entry to `verified`."""
    original = _nodes[i]
    node = dict(_current_node(i))
    if flagged:
        node["needs_followup"] = True
        node.pop("verified_at", None)
    else:
        node["verified_at"] = _now_iso()
        node.pop("needs_followup", None)
    node["_source_node_index"] = i

    if _is_committed(i):
        target = _verified_by_source_index[i]
        target.clear()
        target.update(node)
    else:
        _verified.append(node)
        _verified_by_source_index[i] = node
    _pending_edits.pop(i, None)

    changed = any(node.get(k) != original.get(k) for k in ("type", "number", "heading", "text"))
    add_correction(_act, ai_output=original, human_output=node, changed=changed)
    _maybe_mark_unit_complete(_unit_of_index[i])
    save_verified(_act, _verified)
    return node


def _history_key(note: dict) -> tuple:
    """A history note's stable identity, independent of which node's (or
    which list's) history it currently sits in: its own page plus its raw
    citation text, exactly as history_notes.py's collect_page_notes
    produced it, which never changes across a review session. Used to
    tell whether a note from the original unattached_notes pool has since
    been manually linked somewhere, without needing a separate table just
    to track that -- "does this note's key appear in any node's current
    history" already answers it directly from state that exists anyway."""
    return (note.get("page"), note.get("raw"))


def _attached_history_keys() -> set[tuple]:
    keys: set[tuple] = set()
    for i in range(len(_nodes)):
        if i in _merged_away:
            continue
        for h in _current_node(i).get("history") or []:
            keys.add(_history_key(h))
    return keys


def _currently_unattached_indices() -> list[int]:
    """Indices into _unattached_notes that haven't (yet, or any more) been
    manually linked to a node (see _history_key). detach_history_endpoint
    only ever *appends* to this list (for a note that started out
    auto-attached, so it wasn't already in it) and never removes from it,
    so existing indices stay stable session-long identifiers a reviewer's
    own attach/move action can reference even as the list grows."""
    attached = _attached_history_keys()
    return [i for i, note in enumerate(_unattached_notes) if _history_key(note) not in attached]


def _links_by_node(act: str) -> dict[int, list[dict]]:
    by_node: dict[int, list[dict]] = {}
    for link in load_links(act):
        by_node.setdefault(link["node_index"], []).append(link)
    return by_node


def _is_elevated_risk(node_index: int, node: dict) -> bool:
    """Whether this piece is at elevated risk of automation blindness --
    a reviewer anchoring on whatever classification is already sitting
    there instead of actually forming their own view of the text. Two
    concrete signals, both already computed elsewhere for other reasons:
    node["source"] == "ai" (the probabilistic model backend, rather than
    the deterministic rules engine most nodes come from -- see
    ai_pipeline/structure.py), or a diagnostics finding already attached
    to this specific node (duplicate numbering, an empty leaf, a
    low-confidence history match). Deliberately narrow: gating every one
    of an Act's thousands of unambiguous, cleanly-parsed pieces the same
    way would just make rote friction reviewers click through without
    reading, which is the exact failure mode this is meant to prevent."""
    return node.get("source") == "ai" or bool(_findings_by_node.get(node_index))


def _blind_review_gate_indices(indices: list[int]) -> list[int]:
    """Which of these node indices are elevated-risk and still missing
    their own recorded independent assessment (see
    blind_guess_endpoint) -- Accept is refused for any of these until
    that's done. Never gates Flag: flagging is a reviewer's own honest
    "I'm not confident, this needs follow-up", which is already the
    opposite of blindly agreeing -- adding friction to it would only
    punish exactly the caution this whole mechanism exists to encourage."""
    return [i for i in indices if _is_elevated_risk(i, _current_node(i)) and db.get_blind_review(_act, i) is None]


def _build_piece(node_index: int, label: str, node: dict, links_by_node: dict[int, list[dict]]) -> dict:
    raw_text = node.get("text") or ""
    reflowed, offset_map = reflow_with_map(raw_text)
    piece_links = []
    for link in links_by_node.get(node_index, []):
        piece_links.append({
            **link,
            "start_reflowed": _raw_to_reflowed(link["start"], offset_map),
            "end_reflowed": _raw_to_reflowed(link["end"], offset_map),
        })
    return {
        "node_index": node_index,
        "label": label,
        "type": node["type"],
        "number": node.get("number"),
        "heading": node.get("heading"),
        "text": raw_text,
        "reflowed": reflowed,
        "offset_map": offset_map,
        "findings": _findings_by_node.get(node_index, []),
        "links": piece_links,
        "history": node.get("history") or [],
        "source": node.get("source"),
        "elevated_risk": _is_elevated_risk(node_index, node),
        "blind_review": db.get_blind_review(_act, node_index),
        "verified_at": node.get("verified_at"),
        "needs_followup": bool(node.get("needs_followup")),
        "page_start": node.get("page_start"),
        "page_end": node.get("page_end"),
    }


def _unit_payload(unit_no: int) -> dict:
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    unit_nodes = [_current_node(i) for i in indices]
    labels = compute_unit_labels(unit_nodes) if unit_nodes and unit_nodes[0]["type"] in _UNIT_ROOT_TYPES else ["" for _ in unit_nodes]
    links_by_node = _links_by_node(_act)
    return {
        "unit_no": unit_no,
        "unit_count": len(_units),
        "status": _unit_status(unit_no),
        "root_type": _nodes[_units[unit_no][0]]["type"],
        "pieces": [_build_piece(i, lbl, n, links_by_node) for i, n, lbl in zip(indices, unit_nodes, labels)],
        # Only known within this same server session -- a merge doesn't
        # persist "where did this go" anywhere reconstructible from disk,
        # so this is None (not an error) after a restart. See merge_endpoint.
        "merged_into_unit": _merged_into_unit.get(unit_no) if not indices else None,
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Legislation review")


class EditRequest(BaseModel):
    type: str
    number: str | None = None
    heading: str | None = None
    text: str


class SplitRequest(BaseModel):
    node_index: int
    split_at_reflowed: int
    target_node_index: int


class MergeRequest(BaseModel):
    target_node_index: int
    source_node_indices: list[int]


class RenestRequest(BaseModel):
    node_index: int
    target_node_index: int


class AcceptRequest(BaseModel):
    flagged: bool = False


class LinkRequest(BaseModel):
    node_index: int
    start: int
    end: int
    label: str


class HistoryAttachRequest(BaseModel):
    unattached_id: int
    node_index: int


class HistoryMoveRequest(BaseModel):
    node_index: int
    history_index: int
    target_node_index: int


class HistoryDetachRequest(BaseModel):
    node_index: int
    history_index: int


class BlindGuessRequest(BaseModel):
    type: str
    number: str | None = None
    heading: str | None = None
    reasoning: str


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "review.html")


@app.get("/api/meta")
def get_meta():
    tree_info = compute_unit_tree_info([_nodes[indices[0]]["type"] for indices in _units], _hierarchy)
    units_summary = []
    for u, indices in enumerate(_units):
        root = _nodes[indices[0]]
        units_summary.append({
            "unit_no": u,
            "type": root["type"],
            "number": root.get("number"),
            "heading": root.get("heading"),
            "status": _unit_status(u),
            "flagged_pieces": sum(1 for i in indices if i in _findings_by_node),
            "depth": tree_info[u]["depth"],
            "parent_unit_no": tree_info[u]["parent_unit_no"],
        })
    return {
        "act": _act,
        "unit_count": len(_units),
        "resume_unit": _startup_resume_unit,
        "labels": LABELS,
        "node_types": _relabel_types,
        "corrections": stats(),
        "unattached_notes": len(_currently_unattached_indices()),
        "blind_review_stats": db.blind_review_stats(_act),
        "units": units_summary,
        "has_source_pdf": bool(_source_pdf_path and Path(_source_pdf_path).exists()),
        "act_title": _act_title,
    }


@app.get("/api/units/{unit_no}")
def get_unit(unit_no: int):
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    return _unit_payload(unit_no)


def _get_pdf_doc() -> fitz.Document:
    global _pdf_doc
    if _pdf_doc is None:
        if not _source_pdf_path or not Path(_source_pdf_path).exists():
            raise HTTPException(404, "No source PDF available for this Act")
        _pdf_doc = fitz.open(_source_pdf_path)
    return _pdf_doc


@app.get("/api/pages/{page_no}.png")
def get_page_image(page_no: int):
    """Renders one page of this Act's source PDF as a PNG, so a reviewer
    can check a piece's text against the real page it came from (see
    page_start/page_end on each piece from _build_piece) -- side by side
    with, or in place of, the parsed text. page_no is 1-indexed and refers
    to the *original* PDF's own page numbering (the same numbers
    page_start/page_end already use), not the Act-body-only slice
    run_pipeline.py may have started extraction from. Rendered once per
    page per server run and cached in memory -- an Act's page count is
    small enough (typically well under a thousand) that caching every
    page ever requested costs at most a few tens of MB, far cheaper than
    re-rendering on every click as a reviewer moves between pieces on the
    same page."""
    if page_no in _page_image_cache:
        return Response(content=_page_image_cache[page_no], media_type="image/png")
    doc = _get_pdf_doc()
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, f"This Act's source PDF has pages 1-{doc.page_count}; no page {page_no}")
    pixmap = doc[page_no - 1].get_pixmap(matrix=fitz.Matrix(_PAGE_RENDER_ZOOM, _PAGE_RENDER_ZOOM))
    png_bytes = pixmap.tobytes("png")
    _page_image_cache[page_no] = png_bytes
    return Response(content=png_bytes, media_type="image/png")


@app.get("/api/verified/recent")
def get_recent_verified(limit: int = 8, q: str = ""):
    """The most recently reviewed pieces (most recent last), for the
    "merge/split into an earlier piece" target picker -- optionally
    narrowed by a case-insensitive substring of type/number/heading/text,
    since "recent" alone can miss a piece from well before the current
    unit that a reviewer still remembers by name."""
    pool = _verified
    if q:
        needle = q.lower()
        pool = [
            v for v in pool
            if needle in f"{v['type']} {v.get('number') or ''} {v.get('heading') or ''} {v.get('text') or ''}".lower()
        ]
    recent = pool[-limit:]
    return [
        {
            "node_index": v.get("_source_node_index"),
            "type": v["type"],
            "number": v.get("number"),
            "heading": v.get("heading"),
            "preview": (v.get("text") or "")[:120],
        }
        for v in recent
        if v.get("_source_node_index") is not None and v["_source_node_index"] not in _merged_away
    ]


@app.get("/api/history/unattached")
def get_unattached_history(limit: int = 30, q: str = ""):
    """Amendment-history margin notes attach_history (ai_pipeline/tree.py)
    couldn't confidently match to a node at parse time, for the review
    panel's history sidebar to offer a reviewer as manual-link candidates.
    Narrowed by a case-insensitive substring of the note's own citation
    text/section when `q` is given -- the sidebar defaults this to the
    open unit's own section number, since that's overwhelmingly where a
    note actually belongs, but leaves it a free search since a note can
    just as easily cite a Part/Division instead."""
    pool = [(i, n) for i in _currently_unattached_indices() for n in [_unattached_notes[i]]]
    if q:
        needle = q.lower()
        pool = [
            (i, n) for i, n in pool
            if needle in f"{n.get('raw', '')} {n.get('section') or ''} {n.get('division') or ''} {n.get('part') or ''}".lower()
        ]
    return [{"id": i, **n} for i, n in pool[:limit]]


@app.post("/api/history/attach")
def attach_history_endpoint(req: HistoryAttachRequest):
    """Manually links one of the sidebar's unattached notes to a piece --
    a reviewer confirming what attach_history's own regex-based matching
    (ai_pipeline/history_notes.py) couldn't work out on its own. Tagged
    "manual" rather than "high"/"low" (see diagnostics.py's own
    history-low-confidence check, which only ever flags "low") so it
    reads, later, as a human's own decision rather than another guess."""
    if not (0 <= req.unattached_id < len(_unattached_notes)):
        raise HTTPException(404, "No such note")
    if not (0 <= req.node_index < len(_nodes)) or req.node_index in _merged_away:
        raise HTTPException(404, "No such node")
    note = dict(_unattached_notes[req.unattached_id])
    if _history_key(note) in _attached_history_keys():
        raise HTTPException(400, "This note is already linked to a provision")
    note["confidence"] = "manual"
    note["linked_at"] = _now_iso()
    history = [*(_current_node(req.node_index).get("history") or []), note]
    _mutate_node(req.node_index, history=history)
    return {"node_index": req.node_index, "history": _current_node(req.node_index)["history"]}


@app.post("/api/history/move")
def move_history_endpoint(req: HistoryMoveRequest):
    """Re-targets a note already attached to one piece onto another --
    covers both correcting a wrong auto-match (move to the right piece)
    and simply confirming a "low" confidence guess in place
    (target_node_index == node_index), since both are "a human looked at
    this and this is where it belongs" and get the same "manual" stamp
    either way."""
    if not (0 <= req.node_index < len(_nodes)) or req.node_index in _merged_away:
        raise HTTPException(404, "No such node")
    if not (0 <= req.target_node_index < len(_nodes)) or req.target_node_index in _merged_away:
        raise HTTPException(404, "No such target node")
    history = list(_current_node(req.node_index).get("history") or [])
    if not (0 <= req.history_index < len(history)):
        raise HTTPException(404, "No such history note on this piece")
    note = history.pop(req.history_index)
    note = {**note, "confidence": "manual", "linked_at": _now_iso()}
    if req.target_node_index == req.node_index:
        history.append(note)
        _mutate_node(req.node_index, history=history)
    else:
        _mutate_node(req.node_index, history=history)
        target_history = [*(_current_node(req.target_node_index).get("history") or []), note]
        _mutate_node(req.target_node_index, history=target_history)
    return {"node_index": req.node_index, "target_node_index": req.target_node_index}


@app.post("/api/history/detach")
def detach_history_endpoint(req: HistoryDetachRequest):
    """Removes a wrongly-attached note from a piece entirely, back into
    the sidebar's unattached pool. If the note started out in that pool
    (it was a manual link, or a move/confirm of one), it's already back
    there the moment it's gone from every node's history -- see
    _currently_unattached_indices, which derives "unattached" from
    absence rather than tracking it as its own flag. But a note that
    arrived here via attach_history's own auto-matching (a "high"/"low"
    confidence note straight from parsing) was *never* in that pool, so
    without this it would just vanish from the review entirely on
    detach -- a real historical citation silently dropped, which is
    exactly what diagnostics.py's own module docstring says this tool
    never does. Appending it here, once, keeps it discoverable and
    re-attachable instead."""
    if not (0 <= req.node_index < len(_nodes)) or req.node_index in _merged_away:
        raise HTTPException(404, "No such node")
    history = list(_current_node(req.node_index).get("history") or [])
    if not (0 <= req.history_index < len(history)):
        raise HTTPException(404, "No such history note on this piece")
    note = history.pop(req.history_index)
    _mutate_node(req.node_index, history=history)
    if _history_key(note) not in {_history_key(n) for n in _unattached_notes}:
        _unattached_notes.append(note)
    return {"node_index": req.node_index, "history": history}


@app.post("/api/nodes/{node_index}/edit")
def edit_node_endpoint(node_index: int, req: EditRequest):
    if not (0 <= node_index < len(_nodes)) or node_index in _merged_away:
        raise HTTPException(404, "No such node")
    if req.type not in _relabel_types:
        raise HTTPException(400, f"Unknown type {req.type!r}")
    updated = _mutate_node(node_index, type=req.type, number=req.number or None, heading=req.heading or None, text=req.text)
    return {"node_index": node_index, "type": updated["type"], "number": updated.get("number"), "heading": updated.get("heading")}


@app.post("/api/split")
def split_endpoint(req: SplitRequest):
    i = req.node_index
    if not (0 <= i < len(_nodes)) or i in _merged_away:
        raise HTTPException(404, "No such node")
    if not (0 <= req.target_node_index < len(_nodes)) or req.target_node_index in _merged_away or req.target_node_index == i:
        raise HTTPException(400, "Invalid split target")

    node = _current_node(i)
    raw_text = node.get("text") or ""
    _, offset_map = reflow_with_map(raw_text)
    if not (0 < req.split_at_reflowed < len(offset_map) - 1):
        raise HTTPException(400, "Invalid split position")
    raw_split = offset_map[req.split_at_reflowed]
    head, tail = raw_text[:raw_split].strip(), raw_text[raw_split:].strip()
    if not tail:
        raise HTTPException(400, "Nothing after that position to split off")

    _mutate_node(i, text=head)
    _append_text_to_node(req.target_node_index, tail)
    return {"ok": True}


@app.post("/api/merge")
def merge_endpoint(req: MergeRequest):
    target = req.target_node_index
    sources = req.source_node_indices
    if not sources:
        raise HTTPException(400, "No source piece(s) given")
    for i in [target, *sources]:
        if not (0 <= i < len(_nodes)) or i in _merged_away:
            raise HTTPException(404, f"No such node: {i}")
    if target in sources:
        raise HTTPException(400, "A piece can't be merged into itself")

    source_unit_no = _unit_of_index[sources[0]]
    if any(_unit_of_index[i] != source_unit_no for i in sources):
        raise HTTPException(400, "Source pieces must all belong to the same unit")
    root_index = _units[source_unit_no][0]
    remaining = [j for j in _units[source_unit_no] if j not in _merged_away and j not in sources and j != target]
    if root_index in sources and remaining:
        raise HTTPException(400, "Can't merge the section itself away while it still has pieces nested under it.")

    combined = "\n".join(
        (_current_node(j).get("text") or "").strip() for j in sorted(sources) if (_current_node(j).get("text") or "").strip()
    )
    _append_text_to_node(target, combined)
    target_path = _current_node(target).get("path") or {}
    for j in sources:
        # _nodes[j], not _current_node(j): the corruption pattern this
        # looks for was set by whatever the rule parser originally opened
        # j as, not whatever j's type/number may since have been edited
        # to -- see _repair_cascaded_path.
        _repair_cascaded_path(j, _nodes[j], target_path)
        _merged_away.add(j)
        _pending_edits.pop(j, None)

    target_unit_no = _unit_of_index.get(target)
    source_unit_emptied = all(j in _merged_away for j in _units[source_unit_no])
    if target_unit_no != source_unit_no and source_unit_emptied:
        # So the source unit's own (now pieceless) view can point a
        # reviewer at where its content actually went instead of just
        # reading "(empty -- fully merged away)" -- see _unit_payload.
        _merged_into_unit[source_unit_no] = target_unit_no
        if _is_committed(target):
            # Every node in the source unit is now gone and the
            # destination lives in a different, already-committed unit --
            # nothing left here for a later Accept to ever tag with
            # _unit_end_index, so tag the destination instead
            # (_resume_point only ever needs the *highest* tagged index,
            # so marking this now-fully-handled unit here is exactly as
            # good as tagging one of its own nodes).
            _verified_by_source_index[target]["_unit_end_index"] = source_unit_no
            save_verified(_act, _verified)
    return {"ok": True}


@app.post("/api/renest")
def renest_endpoint(req: RenestRequest):
    """Drag-to-nest in the review panel: makes `node_index` the direct
    child of `target_node_index` by changing only its type (to whatever
    rank sits one level deeper than the target's own -- see
    NEST_CHILD_TYPE), never its position. Document order is left alone
    deliberately: a legislative Act's own text is already in the right
    reading order, so the actual bug this fixes is almost always "this
    piece was classified one level too shallow/deep", not "this piece is
    physically in the wrong place" -- and reordering pieces would mean
    renumbering node_index everywhere it's used as a stable identifier
    (verified rows, links, merged_away, the correction log), which a pure
    type change avoids entirely.

    Restricted to two pieces already in the same review unit: nesting
    only ever happens among the pieces already grouped together under one
    Section (see group_into_units) -- renesting across Sections would be
    a much bigger restructuring this isn't meant to cover."""
    i, target = req.node_index, req.target_node_index
    for idx in (i, target):
        if not (0 <= idx < len(_nodes)) or idx in _merged_away:
            raise HTTPException(404, f"No such node: {idx}")
    if i == target:
        raise HTTPException(400, "A piece can't be nested under itself")
    unit_no = _unit_of_index.get(i)
    if unit_no is None or _unit_of_index.get(target) != unit_no:
        raise HTTPException(400, "Can only nest a piece under another piece in the same review unit")

    unit_indices = [j for j in _units[unit_no] if j not in _merged_away]
    target_pos, node_pos = unit_indices.index(target), unit_indices.index(i)
    if target_pos >= node_pos:
        raise HTTPException(400, "Can only nest a piece under one that already precedes it")

    target_type = _current_node(target)["type"]
    new_type = NEST_CHILD_TYPE.get(target_type)
    if new_type is None:
        raise HTTPException(400, f"A {target_type} can't have nested pieces under it")

    unit_types = [_current_node(j)["type"] for j in unit_indices]
    if not can_renest_under(unit_types, target_pos, node_pos, _hierarchy):
        raise HTTPException(400, f"Can't nest here -- another {target_type} (or shallower) opens between them first")

    _mutate_node(i, type=new_type)
    _recompute_unit_paths(unit_no)
    updated = _current_node(i)
    return {"node_index": i, "type": updated["type"], "path": updated.get("path")}


@app.post("/api/nodes/{node_index}/blind-guess")
def blind_guess_endpoint(node_index: int, req: BlindGuessRequest):
    """Records a reviewer's own classification of an elevated-risk piece,
    made from its text alone, before the review panel reveals what the
    parser actually produced (see _is_elevated_risk and the review
    panel's blind-review gate). This is what unblocks Accept on such a
    piece -- see _blind_review_gate_indices -- so a reviewer can't just
    skip past forming their own view and rubber-stamp whatever's already
    there. Comparison is exact-match on type, and on number normalised
    the same light way a human would read it (case/bracket-insensitive:
    "(A)" and "a" count as the same answer) -- this is reported back to
    the reviewer, not judged; disagreeing with the parser is a fine,
    useful outcome, not an error."""
    if not (0 <= node_index < len(_nodes)) or node_index in _merged_away:
        raise HTTPException(404, "No such node")
    if not req.reasoning.strip():
        raise HTTPException(400, "A short reason for this assessment is required")
    if req.type not in _relabel_types:
        raise HTTPException(400, f"Unknown type {req.type!r}")
    actual = _current_node(node_index)

    def normalize(s: "str | None") -> str:
        return (s or "").strip("() ").lower()

    matched_type = req.type == actual["type"]
    matched_number = normalize(req.number) == normalize(actual.get("number"))
    record = db.save_blind_review(
        _act, node_index, guessed_type=req.type, guessed_number=(req.number or None),
        guessed_heading=(req.heading or None), reasoning=req.reasoning.strip(),
        matched_type=matched_type, matched_number=matched_number,
    )
    return {
        "node_index": node_index,
        "review": record,
        "actual": {"type": actual["type"], "number": actual.get("number"), "heading": actual.get("heading")},
    }


@app.post("/api/nodes/{node_index}/accept")
def accept_node(node_index: int, req: AcceptRequest):
    """Accepts or flags exactly one piece, independent of the rest of its
    unit -- unlike /api/units/{unit_no}/accept, this never requires the
    whole unit to be ready at once. A unit's own status (and its sidebar
    dot) still only turns done/flagged once *every* one of its pieces has
    been decided one way or another, whether that happened here one at a
    time or via that whole-unit endpoint; see _unit_status."""
    if not (0 <= node_index < len(_nodes)) or node_index in _merged_away:
        raise HTTPException(404, "No such node")
    if not req.flagged and _blind_review_gate_indices([node_index]):
        raise HTTPException(400, "This piece needs your own independent assessment before it can be accepted -- see the form above its text.")
    node = _accept_node(node_index, req.flagged)
    return {
        "node_index": node_index,
        "verified_at": node.get("verified_at"),
        "needs_followup": bool(node.get("needs_followup")),
        "unit_status": _unit_status(_unit_of_index[node_index]),
    }


@app.post("/api/units/{unit_no}/accept")
def accept_unit(unit_no: int, req: AcceptRequest):
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    if _unit_status(unit_no) != "pending":
        raise HTTPException(400, "This unit has already been reviewed -- edit its pieces directly instead.")

    # Skip whatever's already been individually accepted/flagged via
    # accept_node above -- this is "accept everything still outstanding
    # in this unit", not "redo the whole unit and overwrite decisions
    # already made piece by piece".
    indices = [i for i in _units[unit_no] if i not in _merged_away and not _is_committed(i)]
    if not req.flagged:
        blocked = _blind_review_gate_indices(indices)
        if blocked:
            raise HTTPException(
                400,
                f"{len(blocked)} piece(s) in this unit need your own independent assessment before the unit can be accepted.",
            )
    if indices:
        unit_orig = [_nodes[i] for i in indices]
        unit_nodes = [_current_node(i) for i in indices]
        before = len(_verified)
        commit_unit(unit_nodes, unit_orig, _act, _verified, flagged=req.flagged, unit_index=unit_no)
        for i, v in zip(indices, _verified[before:]):
            v["_source_node_index"] = i
            _verified_by_source_index[i] = v
            _pending_edits.pop(i, None)
        save_verified(_act, _verified)
    return {"unit_no": unit_no, "status": _unit_status(unit_no)}


@app.get("/api/links")
def get_links():
    return load_links(_act)


@app.post("/api/links")
def post_link(req: LinkRequest):
    if not (0 <= req.node_index < len(_nodes)) or req.node_index in _merged_away:
        raise HTTPException(404, "No such node")
    node_text = _current_node(req.node_index).get("text") or ""
    span_text = node_text[req.start : req.end]
    target = resolve_link(req.label, span_text, _nodes, _definition_index)
    try:
        return add_link(_act, req.node_index, req.start, req.end, req.label, node_text, target=target)
    except LinkError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/links/{link_id}")
def remove_link(link_id: str):
    if not delete_link(_act, link_id):
        raise HTTPException(404, "No such link")
    return {"ok": True}


def main():
    global _act, _nodes, _units, _unit_of_index, _verified, _definition_index
    global _findings_by_node, _unattached_notes, _hierarchy, _relabel_types, _startup_resume_unit
    global _source_pdf_path, _act_title

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--restart", action="store_true", help="ignore existing progress and start from the beginning")
    args = ap.parse_args()

    _act = args.act
    _nodes, _unattached_notes, _hierarchy = load_parsed(args.act)
    _source_pdf_path = load_source_pdf_path(args.act)
    # Computed once here, not per-request: _detect_act_citation re-reads
    # and re-extracts the *whole* source PDF via PyMuPDF just to find the
    # title on its first couple of pages (see dashboard.py's own
    # _act_title_cache, added after that exact cost showed up per page
    # view there -- one Act per process here, so once at startup is enough).
    from ai_pipeline.akn_export import _detect_act_citation

    _act_title = _detect_act_citation(_source_pdf_path).get("title") or args.act
    _units = group_into_units(_nodes)
    for u, indices in enumerate(_units):
        for i in indices:
            _unit_of_index[i] = u
    _definition_index = build_definition_index(_nodes)
    # This Act's own hierarchy levels first when relabelling a node (a
    # custom top level like "chapter" won't be in the built-in list).
    _relabel_types[:] = list(dict.fromkeys([*_hierarchy, *NODE_TYPES]))

    for finding in load_diagnostics(args.act):
        if finding.get("node_index") is not None:
            _findings_by_node.setdefault(finding["node_index"], []).append(finding)

    _verified = [] if args.restart else load_verified(args.act)
    for v in _verified:
        if "_source_node_index" in v:
            _verified_by_source_index[v["_source_node_index"]] = v
    _startup_resume_unit = _resume_point(_units, _verified)
    # Reconstruct which nodes were merged away in a prior session: any
    # index belonging to an already-fully-processed unit (before the
    # resume point) that never made it into `verified` at all -- a
    # node merged away is simply never appended there (see the merge
    # endpoint) -- must have been merged into something else rather
    # than just not-yet-reached.
    for u in range(_startup_resume_unit):
        for i in _units[u]:
            if i not in _verified_by_source_index:
                _merged_away.add(i)

    import uvicorn

    print(f"Serving {args.act}: {len(_nodes)} nodes, {len(_units)} units.")
    print(f"Corrections logged so far across all Acts: {stats()}")
    if _unattached_notes:
        print(f"{len(_unattached_notes)} amendment-history note(s) couldn't be auto-linked to a node.")
    print(f"Open http://127.0.0.1:{args.port}/ in a browser.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
