"""
Local web GUI to human-verify the structural parse of an Act, and to
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
  - Structure -- add, move or remove a piece outright
    (corpus/structure.py). Edit, Split, Merge and Nest all fix a
    piece that is *wrong*; none of them fixes one that is missing (a
    heading the PDF set as an image, a provision the extractor dropped),
    one that is in the document twice (a running header read as a
    provision), or one the parser attached three Sections from where it
    belongs. These do, which is why a structural fault no longer means
    re-parsing the document and losing the review along with it.
    Deleting is recorded, not destructive: a deleted piece is listed
    under the Section it came out of and can be restored to exactly
    where it was. None of it renumbers anything -- an inserted piece
    takes an index above every parse position and order is held
    separately, so every stored decision, link span and finding still
    names the provision it always did.
  - Drag-select a span of a piece's own text to either split it there
    (the tail reassigns to another piece the same way Merge's
    destination picker works) or label it as a link -- an Act citation,
    a defined term, a Bill/EM reference -- which also attempts to
    *resolve* it immediately (corpus/link_targets.py): an
    act_citation against corpus/known_acts.yaml (falling back to
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
each decision (what the parser produced vs. what a human approved) there
too -- a record of where the parser actually gets things wrong, which is
the signal to add a profile override for that Act
(corpus/domain/rules.py) rather than correcting the same pattern by hand
for the rest of the Act. An edit made directly to an already-reviewed piece
(browsing back to fix something) persists and logs immediately, since
there's no later Accept step to do it for. Progress is saved
continuously, so the server can be stopped and restarted from wherever
it left off (see corpus/db.py for why this data -- and only this
data, not the regenerable data/parsed/<act>.json -- moved off plain
JSON files).

Labelled link spans are saved the moment they're labelled -- independent
of structural review above, since annotating a span doesn't require (or
imply) that its node has passed
review, and structural review doesn't need to know these exist.

The header's "Types" button manages the set of types a piece can be
labelled with. The built-in ones (schema.NODE_TYPES plus whatever levels
this Act's profile declares) are fixed -- the parser emits them,
hierarchy.py ranks them and akn_export.py maps them to real AkomaNtoso
elements -- but a reviewer can add extra labels of their own for this Act
("penalty", say). Those are labels only: they take no place in the
hierarchy, so nothing nests under them, and they export as a generic
<hcontainer name="...">. Removing one is guarded -- a type still in use
can only go if the reviewer names an existing type to move those pieces
across to first, so a type can never be deleted out from under the
pieces carrying it.

A "Show source PDF" toggle in the header renders the actual source page
each piece came from (page_start on the piece, GET /api/pages/{n}.png --
a PyMuPDF rasterisation of that page at the panel's own zoom level,
cached in memory) alongside or in place of the parsed text, three view
modes cycled by the one button: text only, side-by-side split, PDF only.
Available only when data/parsed/<act>.json still has the source PDF at
the path it was parsed from (see load_source_pdf_path); missing entirely
otherwise rather than a toggle that always errors.

The page is not a picture to check the text against. It is the second
place the document can be worked on, and for some things the only one.
"Boxes" draws the parse onto the page: a box round every provision
printed there, at the coordinates its lines were read from
(rule_parser.add_rect, GET /api/pages/{n}/boxes). Point at a box and its
piece lights up in the text panel, and the other way round. Right-click
one for everything the text panel offers -- edit it, insert a piece below
it, move it, delete it -- and for the two things only the page can say:

  - Where a provision actually is. Redraw a box, drag its corner, or add
    a second one, and that is stored as the reviewer's own
    (POST /api/nodes/{i}/rects, db.node_rects) and wins over the
    parser's. More than one box is the ordinary case for a continuation,
    which resumes its provision's sentence somewhere further down the
    page. Drawing a box says nothing about the piece's text and does not
    touch it: a box can be corrected long before the piece is decided,
    and correcting one is not a decision.
  - Which provision an amendment note belongs to. The Act answers this
    by printing the note in the margin beside the provision, which is
    the only answer there is when the note's own text names none -- so
    the note is drawn where it prints, joined by a line to the provision
    it is attached to, and re-attached by pointing at a different box.

The text view stays exactly as it was, and is still where most of the
work happens. What the page adds is everything that is about *position*,
which text beside a picture could only ever be guessed at.
"""
from corpus import PROJECT_ROOT
import argparse
import bisect
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from corpus.review import inheritance, structure
from corpus.domain import amendments, diffing, lineage
from corpus.publishing import html_view
from corpus.storage import db
from corpus.ai.assist import build_suggestion
from corpus.review.corrections import add_correction, stats
from corpus.ai.backend import OllamaUnavailable
from corpus.parsing import identity
from corpus.parsing.identity import annotate_ids, name_index
from corpus.parsing.extract import BodyLine, join_printed_line, lines_in_rects
from corpus.parsing.history_notes import parse_note
from corpus.domain.hierarchy import UNIT_BOUNDARY_TYPES, UNIT_ROOT_TYPES, group_into_units, make_ranks
from corpus.domain.profiles import load_profile, profile_for
from corpus.parsing.rule_parser import read_box
from corpus.review.link_annotations import LABELS, LinkError, add_link, delete_link, load_links
from corpus.review.link_targets import build_definition_index, resolve_link
from corpus.domain.schema import NODE_TYPES, types_for_document

BASE_DIR = PROJECT_ROOT
STATIC_DIR = BASE_DIR / "static"

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
    """(nodes, unattached_notes, hierarchy, fingerprint). The fingerprint
    identifies the parse itself (see corpus/reparse.py) and is None
    for output written before run_pipeline.py recorded one."""
    path = Path("data/parsed") / f"{act}.json"
    if not path.exists():
        # Not SystemExit: the dashboard calls this in-process, and there it
        # stopped the whole server.
        raise FileNotFoundError(f"No AI-parsed output found at {path} -- run run_pipeline.py first.")
    from corpus.storage import parsed
    data = parsed.load(path)   # a slim version, put back together
    # Named here as well as in run_pipeline, so a parse written before
    # names existed carries the same ones a re-parse would give it. The
    # names are derived, so both routes agree.
    annotate_ids(data["nodes"], data.get("hierarchy") or None)
    return data["nodes"], data.get("unattached_notes", []), data.get("hierarchy", []), data.get("fingerprint")


load_verified = db.load_verified


def save_verified(act: str, verified: list[dict], base_dir: "str | Path | None" = None) -> None:
    """Store the accepted pieces, including the ones this parse could not
    place (see _unplaced_verified). db.save_verified replaces everything
    stored for the Act, so anything left out here is deleted."""
    db.save_verified(act, list(verified) + _unplaced_verified, base_dir)


def positions_are_trustworthy(act: str, parse_fingerprint: "str | None") -> bool:
    """Whether this Act's stored review rows can still be read as
    positions into the parse `parse_fingerprint` identifies.

    Every verified row is keyed by `_source_node_index` -- an index into
    data/parsed/<act>.json -- and a re-parse that adds, drops or
    re-splits a single node shifts every index after it. run_pipeline.py
    records which parse a set of rows belongs to and re-anchors them onto
    the new one when it changes (see corpus/reparse.py), so normally
    this is True. It is False for rows stored before fingerprints
    existed, or if the parse was replaced by something that didn't
    re-anchor them -- and the callers below then decline to *infer*
    anything from a position rather than guess wrong. Rows still show
    against whatever node they name; what stops is treating a node with
    no row as one the reviewer deliberately merged away."""
    return parse_fingerprint is not None and db.load_parse_fingerprint(act) == parse_fingerprint


def load_diagnostics(act: str) -> list[dict]:
    path = Path("data/diagnostics") / f"{act}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def load_source_pdf_path(act: str) -> str | None:
    """The source PDF path run_pipeline.py/run_em_pipeline.py stamped into
    data/parsed/<act>.json (relative to the repo root, since that's
    where those scripts are run from) -- used to show the actual page a
    piece came from during review (see the /api/pages/{page_no}.png
    endpoint). None if this Act's parsed output predates that field, or
    the PDF has since moved/been deleted -- the page-image endpoint 404s
    in that case rather than the server failing to start."""
    path = Path("data/parsed") / f"{act}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("source")


def load_parse_profile(act: str) -> "str | None":
    """The pattern profile this document was parsed with. Reading a box
    needs the same patterns the parse used -- how an Act numbers its
    Parts is a fact about the Act, and a box read against the wrong
    profile is read wrongly.

    profiles.profile_for, not the name the parse recorded, because a
    versioned document records a versioned name: the Criminal Procedure
    Act's parse says "criminal-procedure-act-v114" and the profile file
    is "criminal-procedure-act.yaml", one profile serving every reprint.
    Passing the recorded name straight to load_profile raised on the
    largest document in the corpus, which is deliberately loud but here
    it was raised at the wrong person -- profile_for already resolves a
    reprint back to its Act."""
    return profile_for(act)


def load_printed_lines(act: str, pdf_path: "str | None") -> list:
    """Every printed line of the source document, with its geometry.

    Prefers what the pipeline already wrote (data/extracted/<act>.json,
    the exact lines the parse was built from) and falls back to reading
    the PDF again. The fallback matters for a clone that has the parse
    but not the extraction -- data/extracted is regenerable, so it isn't
    committed -- and costs one pass over the PDF, once per session."""
    path = Path("data/extracted") / f"{act}.json"
    if path.exists():
        pages = json.loads(path.read_text(encoding="utf-8"))
        return [BodyLine(**line) for page in pages for line in page.get("body_lines", [])]
    if not pdf_path or not Path(pdf_path).exists():
        return []
    from corpus.parsing.extract import extract_pages

    return [line for page in extract_pages(pdf_path) for line in page.body_lines]


def load_document_type(act: str) -> "str | None":
    """Whether this is an Act, a Bill or an Explanatory Memorandum, as
    run_pipeline.py/run_em_pipeline.py recorded it. None for a parse from
    before that field existed -- see schema.types_for_document, which
    treats that as "offer everything" rather than guessing."""
    path = Path("data/parsed") / f"{act}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("document_type")


def _node_at(parse_nodes: list[dict], edits: dict[int, dict]):
    """index -> the node it names, before any review decision about it:
    the parse's own node, or, above the parse, the one a reviewer
    inserted there."""
    def at(index: int) -> dict:
        if 0 <= index < len(parse_nodes):
            return parse_nodes[index]
        edit = edits.get(index)
        if edit is not None and edit.get("node") is not None:
            return edit["node"]
        raise KeyError(f"no node at index {index}")
    return at


def _was_inserted(edits: dict[int, dict], index: int) -> bool:
    """Whether this index names a node a reviewer added rather than one
    the parser produced.

    Worth its own name because of what it guards: a node in a finished
    unit with no verified row is taken to have been merged away, which is
    sound for a parse node and exactly wrong for an inserted one -- a
    piece someone typed into an already-reviewed Section has no row
    because nobody has reviewed it yet, and inferring it away would make
    it vanish on the next startup."""
    edit = edits.get(index)
    return edit is not None and edit.get("node") is not None


def place_edits(nodes: list[dict], stored: dict) -> tuple[dict, dict]:
    """Structural edits, stored by name, placed against this parse.

    Returns ({index: edit}, {name: edit}), the second holding the ones
    that could not be placed -- an edit to a provision this parse does
    not contain. Those are handed back rather than dropped, because
    saving replaces the whole set and a dropped edit would be gone.

    A provision a reviewer inserted is not in the parse at all, so it is
    given an index above it, in the order the inserts were made. "after"
    is a name too, resolved once everything has a position.
    """
    index_of = name_index((index, node["id"]) for index, node in enumerate(nodes) if node.get("id"))
    next_index = len(nodes)
    placed, unplaced = {}, {}

    ordered = sorted(stored.items(),
                     key=lambda pair: (pair[1].get("node_index") is None,
                                       pair[1].get("node_index") or 0))
    for node_id, edit in ordered:
        if node_id in index_of:
            index = index_of[node_id]
        elif edit.get("node") is not None:
            index = next_index
            next_index += 1
            index_of[node_id] = index
        else:
            unplaced[node_id] = edit
            continue
        placed[index] = {**edit, "node_id": node_id}

    for edit in placed.values():
        after = edit.get("after")
        if after is None:
            continue
        edit["after"] = structure.DOCUMENT_START if after == "" else index_of.get(after)
    return placed, unplaced


def stored_edits(edits: dict, names: dict, unplaced: "dict | None" = None) -> dict:
    """The inverse of place_edits: positions back into names, ready to
    store. `names` is {index: name} for this parse and its inserts."""
    out = dict(unplaced or {})
    for index, edit in edits.items():
        node_id = edit.get("node_id") or names.get(index)
        if node_id is None:
            continue
        after = edit.get("after")
        out[node_id] = {
            **edit,
            "node_index": index,
            "after_index": after,
            "after": "" if after == structure.DOCUMENT_START else names.get(after) if after is not None else None,
        }
    return out


def names_by_index(nodes: list[dict], edits: dict) -> dict:
    """{index: name} for a parse and the provisions inserted into it."""
    names = {index: node["id"] for index, node in enumerate(nodes) if node.get("id")}
    names.update({index: edit["node_id"] for index, edit in edits.items() if edit.get("node_id")})
    return names


def verified_by_index(verified: list[dict], names: dict) -> tuple[dict, list[dict]]:
    """A reviewer's rows, against the positions of this parse.

    Returns ({index: row}, unplaced). Each row names the provision it is
    about, so a re-parse that moved the provision still finds it. A row
    whose provision this parse does not contain goes into `unplaced`: it
    is never attached to whatever now happens to sit at its old position,
    and it is never dropped either, because saving replaces the whole
    list and a dropped row would be gone.
    """
    index_of = name_index(names.items())
    placed, unplaced = {}, []
    for row in verified:
        index = index_of.get(row.get("_node_id"))
        if index is None:
            unplaced.append(row)
        else:
            placed[index] = row
    return placed, unplaced


def load_structure_edits(act: str, parse_fingerprint: "str | None",
                         base_dir: "str | Path | None" = None,
                         nodes: "list[dict] | None" = None) -> dict:
    """This document's structural edits (corpus/structure.py), placed
    against the parse `nodes` -- or, without `nodes`, exactly as stored.

    Each edit names the provision it is about, so a re-parse re-attaches
    it wherever that provision now sits. The fingerprint guard stays for
    the moves and deletions whose meaning is positional even so: an edit
    that says "put this before that one" is about an order this parse may
    no longer have."""
    if not positions_are_trustworthy(act, parse_fingerprint):
        return {}
    stored = db.load_structure_edits(act, base_dir)
    if nodes is None:
        return stored
    return place_edits(nodes, stored)[0]


def order_and_units(
    parse_len: int, edits: dict[int, dict], node_at
) -> tuple[list[int], list[list[int]]]:
    """(document order, units) after applying structural edits, both in
    terms of node *index* rather than list position.

    group_into_units works on a list and answers in positions into that
    list, which is the same thing as an index only while the two agree.
    Once a reviewer has inserted or moved something they don't, so the
    positions it gives back are translated straight home through the
    order they were read from -- every unit still names its nodes by the
    index everything else in this tool keys on."""
    order = structure.document_order(parse_len, edits)
    groups = group_into_units([node_at(i) for i in order])
    return order, [[order[position] for position in group] for group in groups]


def build_current_nodes(act: str) -> tuple[list[dict], list[dict], list[str]]:
    """The same "verified where committed, original parser output
    otherwise" merge the live review server keeps in memory via
    _current_node/_verified_by_source_index/_merged_away (see main()'s own
    reconstruction of that state at startup) -- but as a pure, one-shot
    read straight off disk, for a read-only consumer that has no reason to
    hold a whole server process open just to see the Act's current state.
    Used by the live HTML browsing view (corpus/html_view.py) so a
    reviewer's in-progress edits show up immediately, without waiting for
    an export step; akn_export.py/markdown_export.py could use this too
    instead of their own all-or-nothing verified-vs-parsed choice, but
    that's a separate change from introducing it here.

    Returns (nodes, unattached_notes, hierarchy), the first three of
    load_parsed's own return, with merged-away nodes simply absent, so
    any consumer that already builds a hierarchy tree from load_parsed's
    output works unchanged against this instead. A reviewer's structural
    edits are applied too, so what comes back is in the order they put it
    in, with what they inserted present and what they deleted gone."""
    nodes, unattached_notes, hierarchy, fingerprint = load_parsed(act)
    edits = load_structure_edits(act, fingerprint, nodes=nodes)
    node_at = _node_at(nodes, edits)
    order, units = order_and_units(len(nodes), edits, node_at)
    verified = load_verified(act)
    verified_by_source_index, _unplaced = verified_by_index(verified, names_by_index(nodes, edits))

    # "Merged away" is an inference, not a record: nothing marks a node
    # the reviewer folded into another, so it's deduced from the node
    # having no verified row despite sitting in an already-finished unit.
    # That deduction is only as good as the positions it reads, and
    # against a parse the rows don't belong to it silently deletes real
    # provisions from the browse view and from both exports. So when the
    # positions can't be vouched for, infer nothing and show every node.
    merged_away: set[int] = set()
    if positions_are_trustworthy(act, fingerprint):
        for u in finished_units(units, list(verified), markers_are_complete=True):
            for i in units[u]:
                if i not in verified_by_source_index and not _was_inserted(edits, i):
                    merged_away.add(i)

    current_nodes = [verified_by_source_index.get(i, node_at(i)) for i in order if i not in merged_away]
    # Named as in build_effective_nodes_indexed: History review hands a
    # piece's name back to this server to put it right (place_named).
    names = names_by_index(nodes, edits)
    for i, node in zip((i for i in order if i not in merged_away), current_nodes):
        if not node.get("id") and names.get(i):
            node["id"] = names[i]
    return current_nodes, unattached_notes, hierarchy


def build_effective_nodes_indexed(act: str) -> tuple[list["dict | None"], list[list[int]], "str | None"]:
    """The same "verified where committed, original parser output
    otherwise" merge as build_current_nodes, but keyed by *original*
    data/parsed/<act>.json position instead of dropping merged-away
    nodes and reindexing the rest: a merged-away position holds None
    rather than disappearing, so every other position keeps the same
    node_index it has everywhere else this tool keys things by position
    (diagnostics findings, blind_reviews, ai_suggestions, and
    ai_scan_findings below all key this way -- see
    positions_are_trustworthy). run_ai_review.py needs exactly that: a
    scan's findings are only useful if they land back on the same
    node_index a reviewer sees in the live server.

    Returns (nodes, units, fingerprint) -- units from group_into_units
    over the *original* nodes, so a unit's own indices are also stable
    node_index values a caller can hand straight to db.save_ai_scan_finding."""
    nodes, _unattached_notes, _hierarchy, fingerprint = load_parsed(act)
    edits = load_structure_edits(act, fingerprint, nodes=nodes)
    node_at = _node_at(nodes, edits)
    order, units = order_and_units(len(nodes), edits, node_at)
    verified = load_verified(act)
    verified_by_source_index, _unplaced = verified_by_index(verified, names_by_index(nodes, edits))

    merged_away: set[int] = set()
    if positions_are_trustworthy(act, fingerprint):
        for u in finished_units(units, list(verified), markers_are_complete=True):
            for i in units[u]:
                if i not in verified_by_source_index and not _was_inserted(edits, i):
                    merged_away.add(i)

    # Long enough to be indexed by every live node, inserted ones
    # included -- their indices sit above the parse by construction (see
    # structure.next_index). A deleted node's slot holds None for the
    # same reason a merged-away one does: the position still exists, the
    # provision doesn't.
    live = set(order)
    names = names_by_index(nodes, edits)
    effective_nodes = [
        None if i in merged_away or i not in live else verified_by_source_index.get(i, node_at(i))
        for i in range(max([len(nodes) - 1, *edits], default=-1) + 1)
    ]
    # Every node that comes back knows its own name, whether it came from
    # the parse or from a reviewer's own accepted version of it -- a
    # caller storing something against one (run_ai_review) needs the name,
    # and a verified row carries it as _node_id rather than as id.
    for i, node in enumerate(effective_nodes):
        if node is not None and not node.get("id") and names.get(i):
            node["id"] = names[i]
    return effective_nodes, units, fingerprint


_WRAP_RE = re.compile(r"\s*\n\s*")


def reflow_with_map(text: str) -> tuple[str, list[int]]:
    """The stored text's "\\n"s are just the source PDF's own line-wrap
    points, not paragraph breaks -- displaying them raw makes every piece
    look like a jagged list of half-sentences. Collapses each wrap into a
    single space for display -- the same transform every renderer and
    exporter applies (corpus.extract.reflow) -- but also returns the
    raw-offset each reflowed character came from (one longer than the
    reflowed text, for the position just past its last character) -- callers use this to translate a browser
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


# Local aliases for the unit-layout constants group_into_units (now in
# corpus/hierarchy.py, so the pipeline can group units without
# importing this FastAPI app) is built on. Kept because endpoints below
# ask the same questions of individual nodes.
_UNIT_BOUNDARY_TYPES = UNIT_BOUNDARY_TYPES
_UNIT_ROOT_TYPES = UNIT_ROOT_TYPES


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
# for why document order is left untouched. "sub_subparagraph" and the
# non-hierarchy types (note/repealed/example/heading_group) have no entry
# -- nothing can be renested to become *their* child.
NEST_CHILD_TYPE = {
    "section": "subsection",
    "clause": "subsection",
    "item": "subsection",
    "subsection": "paragraph",
    "subclause": "paragraph",
    "subitem": "paragraph",
    "definition": "paragraph",
    "paragraph": "subparagraph",
    "subparagraph": "sub_subparagraph",
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


# A custom node type's name has to survive being written into a parsed
# node's "type" field, matched against hierarchy.py's rank tables, and
# turned into an eId prefix by akn_export.py -- all of which assume the
# same shape the built-in types have. So the same shape is required here
# rather than accepting arbitrary display text.
_CUSTOM_TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def validate_custom_type_name(name: str, existing: list[str]) -> str:
    """Returns the cleaned name, or raises ValueError with a message meant
    to be shown to the reviewer as-is."""
    cleaned = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not cleaned:
        raise ValueError("A type name is required.")
    if not _CUSTOM_TYPE_NAME_RE.match(cleaned):
        raise ValueError(
            "A type name must start with a letter and use only lowercase letters, "
            "digits and underscores (up to 40 characters)."
        )
    if cleaned in existing:
        raise ValueError(f"{cleaned!r} already exists.")
    return cleaned


def finished_units(units: list[list[int]], verified: list[dict],
                   markers_are_complete: bool = False) -> "set[int] | range":
    """Which units the reviewer actually committed.

    Not the same question as _resume_point, which answers *how far they
    got* -- and using that one for this one is what deleted two thirds of
    the Crimes Act. A reviewer who opens a single section in the middle
    of an Act has a resume point of 548 and exactly one finished unit;
    treating units 0..547 as finished inferred 4,767 provisions to have
    been merged away and dropped them from the browse view, the search
    index and both exports.

    commit_unit tags the last node it appends with that unit's own index,
    so a marker *is* a record that the unit was committed. Reading the
    set of markers asks what was done; reading the highest one and
    counting down from it assumes review runs front to back, which it
    does not -- least of all now the review GUI makes opening one section
    the natural thing to do.

    Without markers at all, the caller is looking at rows written before
    markers existed. A marker-free run always committed whole units in
    order, so there the contiguous range _resume_point computes really is
    the set of finished units, and it is kept.

    One case this gives up: a unit whose every node was merged away
    appends nothing, so it carries no marker and is no longer inferred.
    Its text comes back as its own provision as well as inside the one it
    was merged into. That is duplicated text rather than missing text,
    it is visible rather than silent, and it is rare -- all three of
    which the alternative is not."""
    marked = {n["_unit_end_index"] for n in verified if "_unit_end_index" in n}
    if marked:
        return marked
    return range(_resume_point(units, verified, markers_are_complete))


def _resume_point(units: list[list[int]], verified: list[dict], markers_are_complete: bool = False) -> int:
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
    original size.

    `markers_are_complete` says that fallback doesn't apply: the markers
    present are the whole truth, and none present means no unit is
    finished. Pass it whenever the rows are known to belong to this exact
    parse (see positions_are_trustworthy) -- re-anchoring after a
    re-parse rebuilds the markers from the new unit layout, so a row
    count that no longer lines up with whole units is normal there, and
    the fallback would both guess a resume point out of thin air and
    *delete* the rows past it."""
    marked = [n["_unit_end_index"] for n in verified if "_unit_end_index" in n]
    if marked:
        return max(marked) + 1
    if markers_are_complete:
        return 0

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

    A Continuation is named after whatever it continues -- "(1)
    continuation", from its own path. It carries no number because it
    isn't a provision in its own right: it is the rest of subsection
    (1)'s sentence, resumed after that subsection's list has finished
    (see rule_parser's _consume_as_continuation, and s 11(1) of the
    Criminal Procedure Act for the shape). A running counter made it read
    as a separate provision that happened to land there.

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
        path = node.get("path") or {}
        # A sub-item ("4.4") is the first level of its unit, where a
        # section would have a subsection: a chain without it read "(a)"
        # under every sub-item, and "(a) #2" from the second on.
        sub_item = path.get("subitem") or path.get("subclause")
        if node["type"] in ("subitem", "subclause") and node.get("number"):
            labels.append(node["number"])
        elif node["type"] in ("subsection", "paragraph", "subparagraph", "sub_subparagraph") and node.get("number"):
            chain = "".join(
                f"({path[level]})" for level in ("subsection", "paragraph", "subparagraph", "sub_subparagraph") if path.get(level)
            )
            chain = chain or f"({node['number']})"
            if sub_item:
                chain = f"{sub_item}{chain}"
            if path.get("definition"):
                chain = f"{path['definition']} {chain}"
            labels.append(chain)
        elif node["type"] == "definition" and node.get("heading"):
            labels.append(node["heading"])
        elif node["type"] == "continuation":
            # Named after the provision it continues, because it is not a
            # thing of its own -- "(1) continuation" is the rest of
            # subsection (1)'s sentence, resumed after (a) and (b)
            # (Criminal Procedure Act s 11(1) is the shape). Labelled
            # "[continuation 1]", it read as a separate provision that
            # happened to land there, which is exactly what it isn't.
            chain = "".join(
                f"({path[level]})" for level in ("subsection", "paragraph", "subparagraph", "sub_subparagraph") if path.get(level)
            )
            if sub_item:
                chain = f"{sub_item}{chain}"
            if path.get("definition"):
                chain = f"{path['definition']} {chain}".strip()
            labels.append(f"{chain} continuation" if chain else "SECTION continuation")
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
        add_correction(act, parser_output=original, human_output=node, changed=changed)
    if unit_index is not None and verified:
        verified[-1]["_unit_end_index"] = unit_index


# ---------------------------------------------------------------------------
# Server state -- one Act per running process (see the module docstring's
# usage), so there's no per-request act parameter to plumb through.
# ---------------------------------------------------------------------------

_act: str | None = None
_nodes: list[dict] = []
# A reviewer's structural edits, keyed by node index -- what they
# inserted, deleted or moved (see corpus/structure.py). Applied on
# top of _nodes to give _order, which is the document's actual reading
# order; _nodes itself is never reordered, because a node's index is its
# name everywhere else in this tool.
_structure_edits: dict[int, dict] = {}
# Each index's name (see corpus/parsing/identity.py), for the parse's own
# nodes and for the provisions a reviewer inserted above them. Everything
# stored is keyed by the name; everything held here is keyed by the
# index, and this is what turns one into the other.
_node_ids: dict[int, str] = {}
# Structural edits about provisions this parse does not contain. Held so
# that saving, which replaces the whole set, puts them back rather than
# deleting somebody's work over a parse that moved on.
_unplaced_edits: dict[str, dict] = {}
# Accepted pieces about provisions this parse does not contain. Saving
# replaces the whole list, so these are carried through it untouched --
# a provision that comes back in a later reprint gets its review back
# with it. Reported at startup, never silently discarded.
_unplaced_verified: list[dict] = []
# Boxes a reviewer has drawn or adjusted, keyed by node index. The parser
# puts its own on every node it builds (rule_parser.add_rect); these win
# where a person has said otherwise. See db.node_rects.
_node_rects: dict[int, list[dict]] = {}
# Every printed line of the source, with its own place on the page --
# what a box is read against. Loaded the first time a box is read rather
# than at startup, because most sessions never ask. See _printed_lines.
_printed_lines_cache: "list[BodyLine] | None" = None
_order: list[int] = []
# False when the stored positions can't be vouched for against this
# parse, which is when a structural edit could move or delete the wrong
# provision. The endpoints refuse in that state rather than write rows
# that name nodes they don't mean -- see load_structure_edits.
_structure_editable = True
_units: list[list[int]] = []
_unit_of_index: dict[int, int] = {}
_verified: list[dict] = []
_verified_by_source_index: dict[int, dict] = {}
_pending_edits: dict[int, dict] = {}
_merged_away: set[int] = set()
_merged_into_unit: dict[int, int] = {}  # source unit_no -> the unit_no its content ended up in (this session only)
_renest_history: list[dict] = []  # LIFO undo stack for renest_endpoint (this session only) -- see undo_renest_endpoint
_definition_index: dict[str, int] = {}
_findings_by_node: dict[int, list[dict]] = {}
_unattached_notes: list[dict] = []  # startup snapshot, plus anything detach_history_endpoint has since returned to it (this session only)
_hierarchy: list[str] = []
_profile_name: str | None = None
_relabel_types: list[str] = []
_startup_resume_unit = 0
_source_pdf_path: str | None = None
_document_type: str | None = None
_act_title: str | None = None
_pdf_doc: "pymupdf.Document | None" = None
_page_image_cache: "dict[tuple[int, float], bytes]" = {}
# This document as one version of a work (inheritance.work_review), and
# what that says about each of its units -- None and empty for a
# document held in one version. See _load_versions.
_work_review: "dict | None" = None
_unit_lineage: dict[int, dict] = {}
_reference_nodes: "list[dict] | None" = None
# {version: (its Table of Amendments' citations, amendments index)}, read
# once per load -- the endnote a changed unit's margin note points at.
_amending_acts: dict = {}
# This work's instructions from the amending Acts fetched for it
# (corpus/amending), read once per load: None until asked for.
_instructions: "dict | None" = None
_PAGE_RENDER_ZOOM = 1.8  # ~130 DPI -- legible with the page scaled to fit its panel
# The zoom levels the panel's own +/- control steps through, as multiples
# of _PAGE_RENDER_ZOOM. The page is re-rendered at the level being shown
# rather than the browser stretching one raster, so text stays as sharp
# zoomed in as it is at fit-to-width -- which is the whole point of the
# control on a small screen. Fixed ladder, not a free-form number, so the
# cache below can only ever hold a handful of renderings per page.
_PAGE_ZOOM_STEPS = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
# Roughly a hundred A4 pages at the largest step. Rendering is fast but
# not free and a reviewer revisits the same handful of pages constantly,
# so caching pays for itself; a bound keeps a long session over a
# 500-page Act from growing without limit.
_PAGE_CACHE_BUDGET_BYTES = 120 * 1024 * 1024


def _parse_node(i: int) -> dict:
    """What sits at index i before any review decision about it: the
    parse's own node, or, for an index above the parse, the node a
    reviewer inserted there. The baseline an edit is a change *from*, and
    what "reset to parse" goes back to -- for an inserted node that is
    the node as it was inserted, since no parser ever had an opinion
    about it."""
    return _node_at(_nodes, _structure_edits)(i)


def _index_limit() -> int:
    """One past the highest index this document can name -- the parse's
    own length, extended by any inserted node (see structure.next_index).
    For the few callers that genuinely want a position-keyed array rather
    than document order."""
    return max([len(_nodes) - 1, *_structure_edits], default=-1) + 1


def _node_is_live(i: int) -> bool:
    """Whether index i names a node the document currently has -- not
    deleted, not merged away, and actually a node at all."""
    return structure.is_live(i, len(_nodes), _structure_edits) and i not in _merged_away


def _require_live(i: int) -> None:
    if not _node_is_live(i):
        raise HTTPException(404, f"No such node: {i}")


def _rects_for(i: int) -> list[dict]:
    """Where node i is printed: what a reviewer drew if they drew
    anything, else what the parser recorded when it read the page."""
    if i in _node_rects:
        return _node_rects[i]
    try:
        return _parse_node(i).get("rects") or []
    except KeyError:
        return []


def _current_node(i: int) -> dict:
    """Node i as this session currently sees it: a pending (not yet
    Accepted/Flagged) edit first, else its already-reviewed state if the
    unit containing it has been committed, else the original parse."""
    if i in _pending_edits:
        return _pending_edits[i]
    if i in _verified_by_source_index:
        return _verified_by_source_index[i]
    return _parse_node(i)


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
        add_correction(_act, parser_output=original_snapshot, human_output=target, changed=True)
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


_CASCADING_PATH_LEVELS = ("subsection", "paragraph", "subparagraph", "sub_subparagraph")


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
        node = _parse_node(i)
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


_NESTABLE_LEVELS = ("subsection", "subitem", "subclause", "paragraph", "subparagraph", "sub_subparagraph",
                    "definition")


def _depth_rank(i: int) -> "int | None":
    """Where the parser put a continuation. A reviewed row has no column
    for it, so once one was accepted its type's default rank stood in --
    which clears the levels it sits inside, and a continuation under
    s 110(1)(d)(vi) reset the whole section, relabelling every piece after
    it on the next edit."""
    rank = _current_node(i).get("depth_rank")
    if rank is None and 0 <= i < len(_nodes):
        rank = _parse_node(i).get("depth_rank")
    return rank


def _recompute_unit_paths(unit_no: int) -> None:
    """Rebuilds path[level] for subsection/paragraph/subparagraph/
    sub_subparagraph/definition across every (non-merged-away) piece in
    this unit, in current document order -- the exact algorithm tree.py's
    annotate_paths runs once for the whole document at parse time, just
    re-run here for one unit after renest_endpoint changes a piece's type
    (a type change is exactly the kind of thing annotate_paths needs to
    see to place a piece -- and everything *after* it in the unit --
    under the right parent). The unit's own shallower levels (chapter/
    part/.../section) never change from a renest, so the root's own
    already-correct path is carried forward unmodified; only the five
    nestable levels are reset and replayed.

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
        if t == "continuation":
            # Inherits the context it resumes rather than starting one --
            # the same rule tree.annotate_paths applies, replayed here so
            # a renest gives the same answer a re-parse would.
            effective = _depth_rank(i)
            if effective is None:
                effective = rank[t]
            for deeper in _hierarchy[effective:]:
                current[deeper] = None
        elif t in rank:
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


def _rebuild_structure() -> None:
    """Recomputes document order and the unit layout after a structural
    edit, and persists the edits that produced it.

    A unit is not a stored thing: group_into_units derives it from the
    node list, so inserting a Section splits a unit in two and deleting
    one folds two into one. That renumbers units -- and _unit_end_index,
    the marker saying "review got this far", is stored as a unit *number*
    on a verified row. Left alone it would point at a different unit
    after every structural edit, and at startup that marker is what
    decides which nodes are treated as deliberately merged away. So each
    marker is carried across by identity instead: the unit it named is
    found again by a node that was in it, and the marker is rewritten to
    wherever that node now lives. A marker for a unit that no longer
    exists at all is dropped rather than left pointing somewhere
    arbitrary."""
    global _order
    old_units = [list(indices) for indices in _units]
    _order, new_units = order_and_units(len(_nodes), _structure_edits, _parse_node)
    _units[:] = new_units
    _unit_of_index.clear()
    for unit_no, indices in enumerate(_units):
        for i in indices:
            _unit_of_index[i] = unit_no

    remap: dict[int, int] = {}
    for old_no, indices in enumerate(old_units):
        for i in indices:
            if i in _unit_of_index:
                remap[old_no] = _unit_of_index[i]
                break
    for row in _verified:
        if "_unit_end_index" in row:
            moved_to = remap.get(row["_unit_end_index"])
            if moved_to is None:
                row.pop("_unit_end_index")
            else:
                row["_unit_end_index"] = moved_to
    _name_new_inserts()
    save_verified(_act, _verified)
    _save_structure_edits()


def _node_id(index: int) -> "str | None":
    """This index's name, in this parse. None only for an index nothing
    has placed, which a caller should treat as "nothing to store"."""
    return _node_ids.get(index)


def _name_new_inserts() -> None:
    """Give a newly inserted provision its name, and refresh the map.

    An insert is named against the provision it follows, so it has to
    wait until it has one -- which is here, once the edit is adopted.
    Named once and then left alone: moving a provision later does not
    make it a different provision.
    """
    _node_ids.clear()
    for index, node in enumerate(_nodes):
        if node.get("id"):
            _node_ids[index] = node["id"]
    for index, edit in sorted(_structure_edits.items()):
        if edit.get("node_id"):
            _node_ids[index] = edit["node_id"]

    taken = set(_node_ids.values()) | set(_unplaced_edits)
    for index, edit in sorted(_structure_edits.items()):
        if edit.get("node_id") or edit.get("node") is None:
            continue
        after = edit.get("after")
        anchor = _node_ids.get(after, "inserted") if after != structure.DOCUMENT_START else "inserted"
        name = identity.disambiguate(identity.inserted_id(anchor, edit["node"]), taken, edit["node"])
        taken.add(name)
        edit["node_id"] = name
        _node_ids[index] = name


def _save_structure_edits() -> None:
    db.save_structure_edits(_act, stored_edits(_structure_edits, _node_ids, _unplaced_edits))


def _require_structure_editable() -> None:
    if not _structure_editable:
        raise HTTPException(
            409,
            "This document's stored review positions no longer match its parse, so a structural "
            "edit here could move or delete the wrong provision. Re-parse it (which re-anchors the "
            "stored rows) before restructuring it.",
        )


# A unit this version borrows its review for, rather than reviewing
# itself: "inherited" is vouched for by another version already,
# "follows" will be once the next version toward current is reviewed.
_LENT = {lineage.INHERITS: "inherited", lineage.FOLLOWS: "follows"}


def _lent_status(unit_no: int) -> "str | None":
    entry = _unit_lineage.get(unit_no)
    return _LENT.get(entry["status"]) if entry else None


def _unit_status(unit_no: int) -> str:
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    if not indices:
        return "done"  # every node in it ended up merged away into elsewhere
    lent = _lent_status(unit_no)
    if lent and not any(_is_committed(i) for i in indices):
        return lent
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
    original = _parse_node(i)
    node = dict(_current_node(i))
    if flagged:
        node["needs_followup"] = True
        node.pop("verified_at", None)
    else:
        node["verified_at"] = _now_iso()
        node.pop("needs_followup", None)
    node["_source_node_index"] = i
    node["_node_id"] = _node_id(i)

    if _is_committed(i):
        target = _verified_by_source_index[i]
        target.clear()
        target.update(node)
    else:
        _verified.append(node)
        _verified_by_source_index[i] = node
    _pending_edits.pop(i, None)

    changed = any(node.get(k) != original.get(k) for k in ("type", "number", "heading", "text"))
    add_correction(_act, parser_output=original, human_output=node, changed=changed)
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
    for i in _order:
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


def _is_elevated_risk(node_index: int) -> bool:
    """Whether this piece is at elevated risk of automation blindness --
    a reviewer anchoring on whatever classification is already sitting
    there instead of actually forming their own view of the text. The
    signal is a diagnostics finding already attached to this specific node
    (duplicate numbering, an empty leaf, a low-confidence history match),
    computed at parse time for its own reasons. Deliberately narrow:
    gating every one of an Act's thousands of unambiguous, cleanly-parsed
    pieces the same way would just make rote friction reviewers click
    through without reading, which is the exact failure mode this is meant
    to prevent.

    Only a warning or an error counts. Every one of the 1328 info-level
    findings across this repo's own Acts is the same one -- "section 45
    has no body text", the ordinary shape of a Section whose content sits
    in its subsections -- and gating on those put 1360 of 3060 units
    behind a written assessment where only 74 carry a real warning. That
    is the rote friction this docstring warns about, 18 times over: it is
    what made reviewing an Act cost more than anyone would spend, and the
    corpus sat at 0.3% reviewed. An info finding is still *shown* on the
    piece; it just doesn't demand a justification before Accept.

    There was a second signal, node["source"] == "ai", for the
    model-backed parser that used to exist alongside the rules engine.
    That engine is gone (see run_pipeline.py's own docstring), and no node
    was ever actually tagged with it, so it gated nothing."""
    return any(f.get("severity") in ("error", "warning") for f in _findings_by_node.get(node_index, ()))


def _flag_model_disagreements(decided: dict) -> None:
    """The second opinion learned from your decisions (corpus/teaching/
    model.py): an info finding on each undecided piece it reads, surely,
    as something other than the parser did. Advice -- it gates nothing."""
    from corpus.teaching import model as teaching_model

    trained = teaching_model.load(".")
    if not trained or not trained.get("tree"):
        return
    for i in _order:
        if i in decided or i in _merged_away:
            continue
        p = teaching_model.disagrees(trained, _parse_node(i))
        if p:
            reads = "carrying on the line above, not a new piece" if p["label"] == teaching_model.CONTINUES \
                else f"a {p['label']}"
            _findings_by_node.setdefault(i, []).append({
                "severity": "info", "category": "model", "node_index": i, "reads_as": p["label"],
                "message": f"Your past decisions read this as {reads}: {', '.join(p['why'])} "
                           f"({p['support']} lines like it).",
            })


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
        "elevated_risk": _is_elevated_risk(node_index),
        "blind_review": db.get_blind_review(_act, _node_id(node_index)),
        "ai_suggestion": db.get_ai_suggestion(_act, _node_id(node_index)),
        "verified_at": node.get("verified_at"),
        "needs_followup": bool(node.get("needs_followup")),
        "page_start": node.get("page_start"),
        "page_end": node.get("page_end"),
        # Where it is printed, for the PDF view to draw it. "drawn" says
        # a person put it there rather than the parser, which is the one
        # thing a reviewer needs to know before moving it.
        "rects": _rects_for(node_index),
        "rects_drawn": node_index in _node_rects,
        # Whether what is on screen still matches what the parser says.
        # It won't when the piece carries a human's correction -- which is
        # the point -- but also when it carries a stored row from before a
        # parser fix, and those two look identical from here. Saying which
        # fields differ lets the reviewer tell them apart at a glance and,
        # where it is the second, reset the piece (see
        # reset_node_endpoint).
        "differs_from_parse": _differs_from_parse(node_index, node),
    }


def _differs_from_parse(node_index: int, node: dict) -> list:
    """Which of a piece's fields no longer match the current parse. For a
    piece the reviewer inserted, the comparison is against the piece as
    they inserted it -- no parser ever had an opinion about it, so
    nothing here can be a stale snapshot of an older one."""
    original = _parse_node(node_index)
    return [
        field for field in ("type", "number", "heading", "text")
        if (node.get(field) or "") != (original.get(field) or "")
    ]


def _deleted_in_unit(unit_no: int) -> list[int]:
    """Indices of pieces deleted out of this unit, in the order they sat
    in.

    A deleted piece is not in any unit -- it is not in the document at
    all -- so the unit it *was* in is found through the piece it follows,
    which delete_node_endpoint records for exactly this. Where that piece
    was itself deleted the chain is walked back until it reaches one that
    wasn't, so deleting a run of pieces still leaves every one of them
    offered back in the same place."""
    found = []
    for index, edit in sorted(_structure_edits.items()):
        if not edit.get("deleted"):
            continue
        anchor = edit.get("after")
        seen = {index}
        while anchor is not None and anchor not in _unit_of_index and anchor not in seen:
            seen.add(anchor)
            anchor = (_structure_edits.get(anchor) or {}).get("after")
        if anchor is not None and _unit_of_index.get(anchor) == unit_no:
            found.append(index)
    return found


def _unit_payload(unit_no: int) -> dict:
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    unit_nodes = [_current_node(i) for i in indices]
    labels = compute_unit_labels(unit_nodes) if unit_nodes and unit_nodes[0]["type"] in _UNIT_ROOT_TYPES else ["" for _ in unit_nodes]
    links_by_node = _links_by_node(_act)
    return {
        "unit_no": unit_no,
        "unit_count": len(_units),
        "status": _unit_status(unit_no),
        "root_type": _parse_node(_units[unit_no][0])["type"],
        "pieces": [_build_piece(i, lbl, n, links_by_node) for i, n, lbl in zip(indices, unit_nodes, labels)],
        # Offered back rather than gone for good -- a deletion is a
        # judgement, and the reviewer who made it is the one who should
        # get to change their mind about it.
        "deleted_pieces": [
            {
                "node_index": i,
                "type": _parse_node(i)["type"],
                "number": _parse_node(i).get("number"),
                "heading": _parse_node(i).get("heading"),
                "text": (_parse_node(i).get("text") or "")[:200],
            }
            for i in _deleted_in_unit(unit_no)
        ],
        "structure_editable": _structure_editable,
        # What a piece inserted *above* this unit would follow -- the
        # last piece of the unit before it, or the start of the document.
        # Only the server knows the document order, and "above this
        # section" is not the same anchor as "the start of the document"
        # for any section but the first.
        "anchor_above": _anchor_before(_units[unit_no][0]) if _units[unit_no] else structure.DOCUMENT_START,
        # Only known within this same server session -- a merge doesn't
        # persist "where did this go" anywhere reconstructible from disk,
        # so this is None (not an error) after a restart. See merge_endpoint.
        "merged_into_unit": _merged_into_unit.get(unit_no) if not indices else None,
        "lineage": _unit_lineage_payload(unit_no, unit_nodes, indices),
    }


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Legislation review")

# Serve the tool without letting it change anything. Driving the real UI
# against the real corpus is the only way to see that it works, and doing
# that without this wrote decisions nobody made into a reviewer's own
# review. One middleware rather than a check on each endpoint: every
# mutation here is a POST or a DELETE, and a guard that has to be
# remembered on the next endpoint is a guard that will be forgotten.
_READ_ONLY = False


@app.middleware("http")
async def refuse_writes_when_read_only(request: Request, call_next):
    if _READ_ONLY and request.method not in ("GET", "HEAD", "OPTIONS"):
        return JSONResponse(
            status_code=403,
            content={"detail": "This review server was started read-only, so nothing can be changed."},
        )
    return await call_next(request)

# static/site/ is the published site's template -- the page shell, its
# stylesheets, its browser-side scripts and Junicode (see
# corpus/html_view.py's TEMPLATE_DIR). Mounted at the same "/assets"
# every page's asset URLs are built from, so a browse page served here
# loads exactly the files export_static_site.py publishes. StaticFiles
# resolves the path itself and refuses to escape the directory, which is
# what the hand-rolled /fonts route this replaces had to check for.
app.mount("/assets", StaticFiles(directory=html_view.TEMPLATE_DIR), name="assets")
# The admin pages' own stylesheets (static/admin/), kept out of the pages
# so the style preview (corpus/publishing/style_preview.py) links the same
# files the tools do -- an inline copy would never show an edit.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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


class InsertRequest(BaseModel):
    # -1 (structure.DOCUMENT_START) inserts at the very start of the
    # document; otherwise the piece this new one follows.
    after_node_index: int
    type: str
    number: str | None = None
    heading: str | None = None
    text: str = ""


class MoveRequest(BaseModel):
    node_index: int
    after_node_index: int


class AcceptRequest(BaseModel):
    flagged: bool = False
    # What was wrong, in the reviewer's words: it goes in the report they
    # hand back (corpus/review/report.py).
    note: str = ""


def _note_pieces(indices, req: AcceptRequest) -> None:
    """A flag's note on each piece it flagged; accepting takes it away,
    since there is no longer anything wrong to say."""
    if req.flagged and not req.note.strip():
        return
    db.save_review_notes(_act, {_node_id(i): req.note if req.flagged else "" for i in indices if _node_id(i)})


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


class HistoryCreateRequest(BaseModel):
    rect: dict
    node_index: int


class BlindGuessRequest(BaseModel):
    type: str
    number: str | None = None
    heading: str | None = None
    reasoning: str


class NodeTypeCreateRequest(BaseModel):
    name: str


class NodeTypeRenameRequest(BaseModel):
    new_name: str


class NodeTypeDeleteRequest(BaseModel):
    # The type every node currently using the doomed one is moved to.
    # Required whenever it's actually in use -- see delete_node_type.
    replacement: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "review.html")

def _box_label(label: "str | None", node: dict) -> str:
    """What a box is called on the page. compute_unit_labels calls a unit's
    own piece "SECTION" whatever it is, which on the page read as a
    section still after it was retyped a clause or a Part."""
    return pieces_label(node) if label in (None, "", "SECTION") else label


@app.get("/api/meta")
def get_meta():
    # As the unit reads now, edits included: from the parse alone, a
    # section you retyped went on showing its old type in the list.
    tree_info = compute_unit_tree_info([_current_node(indices[0])["type"] for indices in _units], _hierarchy)
    units_summary = []
    for u, indices in enumerate(_units):
        root = _current_node(indices[0])
        units_summary.append({
            "unit_no": u,
            "type": root["type"],
            "number": root.get("number"),
            "heading": root.get("heading"),
            "status": _unit_status(u),
            "flagged_pieces": sum(1 for i in indices if i in _findings_by_node),
            "model_flags": sum(1 for i in indices if i not in _merged_away and i not in _verified_by_source_index
                               and any(f.get("category") == "model" for f in _findings_by_node.get(i, ()))),
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
        "renest_undo": (
            {"node_index": _renest_history[-1]["node_index"], "restores_type": _renest_history[-1]["previous_type"]}
            if _renest_history else None
        ),
        "units": units_summary,
        "has_source_pdf": bool(_source_pdf_path and Path(_source_pdf_path).exists()),
        "act_title": _act_title,
        "version_info": _version_info(),
    }


@app.get("/api/units/{unit_no}")
def get_unit(unit_no: int):
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    return _unit_payload(unit_no)


def _get_pdf_doc() -> pymupdf.Document:
    global _pdf_doc
    if _pdf_doc is None:
        if not _source_pdf_path or not Path(_source_pdf_path).exists():
            raise HTTPException(404, "No source PDF available for this Act")
        _pdf_doc = pymupdf.open(_source_pdf_path)
    return _pdf_doc


def _cache_page_image(key: "tuple[int, float]", png_bytes: bytes) -> None:
    """Keeps the cache under _PAGE_CACHE_BUDGET_BYTES, evicting whatever
    was inserted longest ago (dicts preserve insertion order) -- a
    reviewer works forward through an Act, so the oldest entry is also
    the one furthest behind where they are now."""
    _page_image_cache[key] = png_bytes
    total = sum(len(v) for v in _page_image_cache.values())
    while total > _PAGE_CACHE_BUDGET_BYTES and len(_page_image_cache) > 1:
        total -= len(_page_image_cache.pop(next(iter(_page_image_cache))))


_version_docs: dict = {}


def _version_doc(version: int) -> pymupdf.Document:
    """Another version's source PDF, for setting its page beside this
    one's -- verifying a change means seeing both printed pages, not one
    page and a diff."""
    if not _work_review or version not in _work_review["slugs"]:
        raise HTTPException(404, f"No version {version} of this work is held here")
    if _work_review["slugs"][version] == _act:
        return _get_pdf_doc()
    if version not in _version_docs:
        path = load_source_pdf_path(_work_review["slugs"][version])
        if not path or not Path(path).exists():
            raise HTTPException(404, f"Version {version}'s source PDF isn't where its parse says it is")
        _version_docs[version] = pymupdf.open(path)
    return _version_docs[version]


# Digits only, or it takes the ".png" requests below too.
@app.get("/api/versions/{version}/pages/{page_no:int}")
def get_version_page(version: int, page_no: int):
    """A page's size in PDF points, which the boxes drawn over it are
    measured in, and how many pages that version's PDF has."""
    doc = _version_doc(version)
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, f"Version {version}'s PDF has pages 1-{doc.page_count}; no page {page_no}")
    rect = doc[page_no - 1].rect
    return {"width": rect.width, "height": rect.height, "page_count": doc.page_count}


@app.get("/api/versions/{version}/pages/{page_no}.png")
def get_version_page_image(version: int, page_no: int, zoom: float = 1.0):
    zoom = min(_PAGE_ZOOM_STEPS, key=lambda step: abs(step - zoom))
    key = (version, page_no, zoom)
    if key in _page_image_cache:
        return Response(content=_page_image_cache[key], media_type="image/png")
    doc = _version_doc(version)
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, f"Version {version}'s PDF has pages 1-{doc.page_count}; no page {page_no}")
    scale = _PAGE_RENDER_ZOOM * zoom
    png_bytes = doc[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
    _cache_page_image(key, png_bytes)
    return Response(content=png_bytes, media_type="image/png")


@app.get("/api/pages/{page_no}.png")
def get_page_image(page_no: int, zoom: float = 1.0):
    """Renders one page of this Act's source PDF as a PNG, so a reviewer
    can check a piece's text against the real page it came from (see
    page_start/page_end on each piece from _build_piece) -- side by side
    with, or in place of, the parsed text. page_no is 1-indexed and refers
    to the *original* PDF's own page numbering (the same numbers
    page_start/page_end already use), not the Act-body-only slice
    run_pipeline.py may have started extraction from. Rendered once per
    page per server run and cached in memory -- an Act's page count is
    small enough (typically well under a thousand) that caching what a
    reviewer actually looks at is far cheaper than re-rendering on every
    click as they move between pieces on the same page -- bounded by
    _PAGE_CACHE_BUDGET_BYTES so a long session can't grow without limit.

    `zoom` is the panel's own zoom level, one of _PAGE_ZOOM_STEPS (anything
    else is snapped to the nearest). The page is rendered at that level
    rather than handed over at one size for the browser to stretch, so
    zooming in gives more detail instead of bigger pixels."""
    zoom = min(_PAGE_ZOOM_STEPS, key=lambda step: abs(step - zoom))
    key = (page_no, zoom)
    if key in _page_image_cache:
        return Response(content=_page_image_cache[key], media_type="image/png")
    doc = _get_pdf_doc()
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, f"This Act's source PDF has pages 1-{doc.page_count}; no page {page_no}")
    scale = _PAGE_RENDER_ZOOM * zoom
    png_bytes = doc[page_no - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")
    _cache_page_image(key, png_bytes)
    return Response(content=png_bytes, media_type="image/png")


def _indices_on_page(page_no: int) -> list[int]:
    """Every live provision printed on this page, in document order."""
    return [i for i in sorted(_unit_of_index)
            if i not in _merged_away and _node_is_live(i)
            and any(r.get("page") == page_no for r in _rects_for(i))]


@app.post("/api/pages/{page_no}/accept")
def accept_page(page_no: int, req: AcceptRequest):
    """Accepts (or flags) everything still outstanding on one page.

    The page rather than the unit, because the page is what a reviewer
    is actually looking at: a page of a printed Act routinely carries
    the tail of one Section, the whole of the next, and the head of a
    third, and having read all of it there is no reason to decide it in
    three goes.

    Pieces already accepted are left alone, the same as the whole-unit
    button: this finishes what is outstanding rather than redoing
    decisions already made.
    """
    doc = _get_pdf_doc()
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, f"This Act's source PDF has pages 1-{doc.page_count}; no page {page_no}")

    on_page = _indices_on_page(page_no)
    # A lent piece is another version's to decide, so the page is
    # finished without it; accepting it here is still one click away.
    outstanding = [i for i in on_page if not _is_committed(i) and _piece_status(i) != "inherited"]
    # Accepting also resolves a flag raised earlier, which is what
    # "accept" means once a piece has been through review once.
    reflagged = [] if req.flagged else [
        i for i in on_page
        if _is_committed(i) and _verified_by_source_index[i].get("needs_followup")
    ]
    if not outstanding and not reflagged:
        raise HTTPException(400, "Everything on this page has already been accepted.")

    for i in outstanding + reflagged:
        _accept_node(i, req.flagged)
    _note_pieces(outstanding + reflagged, req)

    return {
        "page": page_no,
        "decided": len(outstanding) + len(reflagged),
        # What each box on the page should now be drawn as, so the
        # overlay repaints from the same answer the panel does.
        "statuses": {str(i): _piece_status(i) for i in on_page},
        "units": {str(u): _unit_status(u)
                  for u in sorted({_unit_of_index[i] for i in on_page})},
    }


@app.get("/api/pages/{page_no}/boxes")
def get_page_boxes(page_no: int):
    """Every provision printed on this page, and where.

    This is what makes the page itself the thing a reviewer works on
    rather than a picture to check the text against. The whole page, not
    just the unit currently open: a provision's neighbours are the
    context that says whether it starts and ends where the parser thinks
    it does, and half of them belong to the section before or after.

    Sizes are in PDF points from the top-left of the page, and the page's
    own size comes back with them, so the overlay works out its own scale
    from the rendered image rather than having to know what zoom it asked
    for (see get_page_image, which renders at a zoom of the panel's
    choosing)."""
    doc = _get_pdf_doc()
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, f"This Act's source PDF has pages 1-{doc.page_count}; no page {page_no}")
    page = doc[page_no - 1]

    boxes = []
    for unit_no, indices in enumerate(_units):
        labels = None
        for position, i in enumerate(indices):
            if i in _merged_away:
                continue
            all_rects = _rects_for(i)
            rects = [r for r in all_rects if r.get("page") == page_no]
            if not rects:
                continue
            if labels is None:
                live = [j for j in indices if j not in _merged_away]
                nodes = [_current_node(j) for j in live]
                computed = (
                    compute_unit_labels(nodes)
                    if nodes and nodes[0]["type"] in _UNIT_ROOT_TYPES
                    else ["" for _ in nodes]
                )
                labels = dict(zip(live, computed))
            node = _current_node(i)
            boxes.append({
                "node_index": i,
                "unit_no": unit_no,
                "label": _box_label(labels.get(i), node),
                "type": node["type"],
                "status": _piece_status(i),
                "preview": reflow_with_map(node.get("text") or node.get("heading") or "")[0][:140],
                "rects": rects,
                # Its boxes on other pages. A section running over a page
                # break has one box on each, and without these the page
                # thought that box was the piece's only one: it would not
                # offer to delete it, and a save kept only this page's.
                "elsewhere": [r for r in all_rects if r.get("page") != page_no],
                "drawn": i in _node_rects,
            })

    return {
        "page": page_no,
        "width": round(page.rect.width, 2),
        "height": round(page.rect.height, 2),
        "boxes": boxes,
        "notes": _page_note_boxes(page_no),
        "editable": _structure_editable,
    }


def pieces_label(node: dict) -> str:
    """A label for a piece outside any Section's own numbering -- a Part
    or Division heading, which is its own one-piece unit."""
    bits = [node["type"].upper()]
    if node.get("number"):
        bits.append(node["number"])
    return " ".join(bits)


def _piece_status(i: int) -> str:
    row = _verified_by_source_index.get(i)
    if row is None:
        return "inherited" if _lent_status(_unit_of_index.get(i, -1)) else "pending"
    return "flagged" if row.get("needs_followup") else "accepted"


def _page_note_boxes(page_no: int) -> list[dict]:
    """The amendment-history notes printed in this page's margin, where
    they are printed, and which provision each is attached to.

    The Act itself draws the link by setting the note beside the
    provision it amends, and that placement is the only thing that says
    which provision a note belongs to when its own text doesn't name one.
    Handing back both ends lets the view draw the line the page implies
    -- and lets a reviewer redraw it by pointing at a different box."""
    notes = []
    for i in _order:
        if i in _merged_away:
            continue
        for position, h in enumerate(_current_node(i).get("history") or []):
            rect = h.get("rect")
            if rect and rect.get("page") == page_no:
                notes.append({
                    "raw": h.get("raw", ""), "rect": rect,
                    "node_index": i, "history_index": position,
                    "confidence": h.get("confidence"),
                })
    still_unattached = set(_currently_unattached_indices())
    for position, note in enumerate(_unattached_notes):
        rect = note.get("rect")
        if rect and rect.get("page") == page_no and position in still_unattached:
            notes.append({
                "raw": note.get("raw", ""), "rect": rect,
                "node_index": None, "unattached_id": position,
                "confidence": None,
            })
    return notes


def _printed_lines() -> list:
    """The source's own lines, loaded once and kept."""
    global _printed_lines_cache
    if _printed_lines_cache is None:
        _printed_lines_cache = load_printed_lines(_act, _source_pdf_path)
    return _printed_lines_cache


def _read_box(node_index: int) -> dict:
    """Re-reads one piece from the box drawn over it, and returns what
    changed.

    This is the point of drawing boxes at all. Up to here a box said
    where a provision is; this makes it say what the provision is, so a
    provision the parser split in the wrong place is corrected by drawing
    the box round the right words rather than by retyping them.

    A box with more than one rectangle reads as one provision printed in
    more than one place -- which is the ordinary shape of a continuation,
    and of anything that runs over a page."""
    rects = _rects_for(node_index)
    if not rects:
        raise HTTPException(400, "This piece has no box to read. Draw one over it first.")
    lines = _printed_lines()
    if not lines:
        raise HTTPException(
            503,
            "The source PDF this was parsed from isn't where the parse says it is, so there are no "
            "printed lines to read a box against.",
        )
    boxed = lines_in_rects(lines, rects)
    if not boxed:
        raise HTTPException(400, "There is nothing printed inside that box.")
    try:
        return read_box(_current_node(node_index), boxed, load_profile(_profile_name))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/nodes/{node_index}/read-box")
def read_box_endpoint(node_index: int):
    """Takes this piece's words from the box drawn over it."""
    _require_live(node_index)
    fields = _read_box(node_index)
    before = _current_node(node_index)
    if all((fields.get(k) or "") == (before.get(k) or "") for k in fields):
        return {"node_index": node_index, "changed": False,
                "message": "Already exactly what the box says."}
    updated = _mutate_node(node_index, **fields)
    _recompute_unit_paths(_unit_of_index[node_index])
    return {
        "node_index": node_index, "changed": True, "changed_fields": sorted(fields),
        "type": updated["type"], "number": updated.get("number"),
        "heading": updated.get("heading"), "text": updated.get("text"),
    }


@app.post("/api/units/{unit_no}/read-boxes")
def read_unit_boxes_endpoint(unit_no: int):
    """Takes every piece in this section from its own box.

    The whole section at once, because a section is usually wrong in more
    than one place at a time: one boundary in the wrong spot moves text
    off one piece and onto its neighbour, so fixing it means redrawing
    two boxes and re-reading both.

    It re-reads, and never restructures. A piece with no box is left
    alone, and no piece is created or removed -- the box says what a
    provision says, not which provisions there are. Adding or removing
    one is its own decision, made with Insert and Delete."""
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    changed, unchanged, skipped, failed = [], 0, 0, []
    for i in _units[unit_no]:
        if i in _merged_away:
            continue
        if not _rects_for(i):
            skipped += 1
            continue
        try:
            fields = _read_box(i)
        except HTTPException as e:
            failed.append({"node_index": i, "detail": e.detail})
            continue
        if all((fields.get(k) or "") == (_current_node(i).get(k) or "") for k in fields):
            unchanged += 1
            continue
        _mutate_node(i, **fields)
        changed.append(i)
    if changed:
        _recompute_unit_paths(unit_no)
    return {"unit_no": unit_no, "changed": changed, "unchanged": unchanged,
            "no_box": skipped, "failed": failed}


class RectsRequest(BaseModel):
    # None hands the piece back to the parser's own box; [] says it has
    # none. Both are answers, and they are different ones.
    rects: "list[dict] | None" = None


@app.post("/api/nodes/{node_index}/rects")
def set_node_rects_endpoint(node_index: int, req: RectsRequest):
    """Records where a reviewer says this piece is printed.

    Drawing a box is not a decision about the piece's text, and does not
    touch it: the boxes live in their own table (see db.node_rects) and
    the piece stays exactly as undecided, or as accepted, as it was. What
    it changes is what the page shows -- which is the whole point of
    working on the page rather than beside it."""
    _require_structure_editable()
    _require_live(node_index)
    rects = None
    if req.rects is not None:
        rects = []
        for raw in req.rects:
            try:
                rect = {
                    "page": int(raw["page"]),
                    "x0": round(float(raw["x0"]), 1), "y0": round(float(raw["y0"]), 1),
                    "x1": round(float(raw["x1"]), 1), "y1": round(float(raw["y1"]), 1),
                }
            except (KeyError, TypeError, ValueError) as e:
                raise HTTPException(400, f"Not a rectangle: {raw!r}") from e
            if rect["x1"] <= rect["x0"] or rect["y1"] <= rect["y0"]:
                raise HTTPException(400, "A box needs width and height")
            rects.append(rect)
    if rects is None:
        _node_rects.pop(node_index, None)
    else:
        _node_rects[node_index] = rects
    db.save_node_rects(_act, _node_id(node_index), rects, node_index=node_index)
    return {"node_index": node_index, "rects": _rects_for(node_index), "drawn": node_index in _node_rects}


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
    """Amendment-history margin notes attach_history (corpus/tree.py)
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
    (corpus/history_notes.py) couldn't work out on its own. Tagged
    "manual" rather than "high"/"low" (see diagnostics.py's own
    history-low-confidence check, which only ever flags "low") so it
    reads, later, as a human's own decision rather than another guess."""
    if not (0 <= req.unattached_id < len(_unattached_notes)):
        raise HTTPException(404, "No such note")
    _require_live(req.node_index)
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
    _require_live(req.node_index)
    _require_live(req.target_node_index)
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
    _require_live(req.node_index)
    history = list(_current_node(req.node_index).get("history") or [])
    if not (0 <= req.history_index < len(history)):
        raise HTTPException(404, "No such history note on this piece")
    note = history.pop(req.history_index)
    _mutate_node(req.node_index, history=history)
    if _history_key(note) not in {_history_key(n) for n in _unattached_notes}:
        _unattached_notes.append(note)
    return {"node_index": req.node_index, "history": history}


@app.post("/api/history/create")
def create_history_endpoint(req: HistoryCreateRequest):
    """A margin note the parse missed, read from a box drawn over it and
    attached to the piece a reviewer names.

    Read by parse_note, as every parsed note is, so it names its section
    and sub-path the same way and the site links it the same way."""
    rect = _clean_rect(req.rect)
    _require_live(req.node_index)
    raw = _page_text_in(rect)
    if not raw:
        raise HTTPException(400, "There is nothing printed inside that box.")
    note = {**parse_note(raw), "page": rect["page"], "rect": rect,
            "confidence": "manual", "linked_at": _now_iso()}
    if _history_key(note) in _attached_history_keys():
        raise HTTPException(400, "That note is already attached to a provision")
    history = [*(_current_node(req.node_index).get("history") or []), note]
    _mutate_node(req.node_index, history=history)
    return {"node_index": req.node_index, "history": _current_node(req.node_index)["history"]}


@app.post("/api/nodes/{node_index}/edit")
def edit_node_endpoint(node_index: int, req: EditRequest):
    """Changes one piece's type, number, heading or text.

    The recompute is the point of the third line, not an afterthought.
    A piece's displayed number is its *path* chain -- "(a)(c)" -- not its
    own number, because that is what says where in the Section it sits
    (see compute_unit_labels). Type and number are two of the three
    things that chain is derived from, so changing either without
    replaying the path leaves the label describing where the piece used
    to be: correcting a mis-parsed subparagraph "(c)" to a paragraph went
    on reading "(a)(c)" instead of "(c)", and so did everything nested
    after it, which is issue #51. Heading is the third, through a
    Definition, whose defined term is its heading and is carried into the
    path of every piece under it.

    Unconditional, as in read_box_endpoint above, which changes the same
    fields for the same kind of reason. Replaying a unit's paths is
    idempotent and costs one pass over one Section, which is less than
    working out whether it was needed."""
    _require_live(node_index)
    if req.type not in _relabel_types:
        raise HTTPException(400, f"Unknown type {req.type!r}")
    updated = _mutate_node(node_index, type=req.type, number=req.number or None, heading=req.heading or None, text=req.text)
    _recompute_unit_paths(_unit_of_index[node_index])
    updated = _current_node(node_index)
    return {"node_index": node_index, "type": updated["type"], "number": updated.get("number"),
            "heading": updated.get("heading"), "path": updated.get("path")}


class AlikeRequest(BaseModel):
    merging: bool = False
    targets: list[int]


def _alike(node_index: int, merging: bool) -> tuple[dict, dict, list[dict]]:
    """The rule a correction of this piece would teach, the correction
    itself, and every other undecided piece the rule picks out. Compared
    against what the parser made of each, since that is what it will do
    again on the next Act."""
    from corpus.teaching import alike, rules as teaching_rules

    seen = _parse_node(node_index).get("seen")
    if not seen:
        raise HTTPException(400, "This piece has no record of how it was printed (inserted, or parsed "
                                 "before pieces kept one), so there is nothing to compare others with.")
    parse_type = _parse_node(node_index)["type"]
    now = _current_node(node_index)
    rule = alike.rule_for(seen, None if merging else now.get("number"), merging)
    fix = {"merge": True} if merging else {"type": now["type"], "numbered": bool(now.get("number"))}
    found = []
    for i in _order:
        if i == node_index or not _node_is_live(i) or _is_committed(i):
            continue
        node = _parse_node(i)
        if node.get("type") != parse_type or not node.get("seen") or not teaching_rules.matches(rule, node["seen"]):
            continue
        current = _current_node(i)
        new_number = alike.number_in(rule, node["seen"]["text"]) if fix.get("numbered") else None
        if merging:
            unit = [j for j in _units[_unit_of_index[i]] if _node_is_live(j)]
            if unit[0] == i:
                continue   # a section's own line has nothing above it in the section to join
        elif current["type"] == now["type"] and (current.get("number") or None) == (new_number or current.get("number")):
            continue   # already reads that way
        found.append({"node_index": i, "unit_no": _unit_of_index.get(i), "page": node["seen"]["page"],
                      "text": node["seen"]["text"], "type": current["type"], "number": current.get("number"),
                      "new_number": new_number if fix.get("numbered") else current.get("number")})
    return rule, fix, found


@app.get("/api/nodes/{node_index}/alike")
def alike_endpoint(node_index: int, merging: bool = False):
    """Pieces printed like this one, to correct the same way ("Find
    others like this"). For a merge, `node_index` is the piece merged
    away."""
    from corpus.teaching import alike

    rule, fix, found = _alike(node_index, merging)
    words = "joins the piece above it" if merging else (
        f"is a {fix['type']}" + (", numbered as printed" if fix["numbered"] else ""))
    return {"description": alike.describe(rule, words), "fix": fix, "matches": found}


@app.post("/api/nodes/{node_index}/alike")
def apply_alike_endpoint(node_index: int, req: AlikeRequest):
    """Makes the same correction to each ticked look-alike, through the
    same endpoints a reviewer's own edit or merge goes through. Each stays
    undecided until its unit is accepted, and becomes an example then."""
    _rule, fix, found = _alike(node_index, req.merging)
    by_index = {f["node_index"]: f for f in found}
    applied, skipped = [], []
    for t in req.targets:
        f = by_index.get(t)
        try:
            if f is None:
                raise HTTPException(400, "no longer looks like it")
            if req.merging:
                unit = [j for j in _units[_unit_of_index[t]] if _node_is_live(j)]
                merge_endpoint(MergeRequest(target_node_index=unit[unit.index(t) - 1], source_node_indices=[t]))
            else:
                current = _current_node(t)
                edit_node_endpoint(t, EditRequest(type=fix["type"], number=f["new_number"],
                                                  heading=current.get("heading"), text=current.get("text") or ""))
            applied.append(t)
        except HTTPException as e:
            skipped.append({"node_index": t, "why": e.detail})
    return {"applied": applied, "skipped": skipped}


@app.post("/api/nodes/{node_index}/reset")
def reset_node_endpoint(node_index: int):
    """Puts one piece back to exactly what the parser says now, and
    returns it to the queue undecided.

    A stored row holds the text as it stood when it was decided. That is
    the point for an accepted piece -- it is the human's work, and
    corpus/reparse.py goes to some length to keep it attached to the
    right provision when the Act is parsed again. But it also means a
    parser fix cannot reach a piece that was already looked at: the
    Criminal Procedure Act's section 5 kept showing the Part 2.2 heading
    swallowed into its note long after the parse stopped doing that,
    because a flagged row from before the fix still carried it.

    So this is the way back. It clears any pending edit, drops the stored
    row entirely, and leaves the piece unverified and unflagged, so it
    comes round again and is decided against the text the parser produces
    today. Deliberately explicit rather than automatic: whether a stored
    row is a human's correction or a stale snapshot of an older parse is
    exactly the judgement a reviewer is here to make."""
    _require_live(node_index)
    _pending_edits.pop(node_index, None)
    row = _verified_by_source_index.pop(node_index, None)
    if row is not None:
        _verified[:] = [v for v in _verified if v is not row]
        save_verified(_act, _verified)
    node = _parse_node(node_index)
    return {
        "node_index": node_index,
        "type": node["type"], "number": node.get("number"), "heading": node.get("heading"),
        "unit_status": _unit_status(_unit_of_index[node_index]),
    }


@app.post("/api/split")
def split_endpoint(req: SplitRequest):
    i = req.node_index
    _require_live(i)
    if not _node_is_live(req.target_node_index) or req.target_node_index == i:
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
        _require_live(i)
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
    merged_inserted: list[int] = []
    _append_text_to_node(target, combined)
    target_path = _current_node(target).get("path") or {}
    for j in sources:
        # _nodes[j], not _current_node(j): the corruption pattern this
        # looks for was set by whatever the rule parser originally opened
        # j as, not whatever j's type/number may since have been edited
        # to -- see _repair_cascaded_path.
        _repair_cascaded_path(j, _parse_node(j), target_path)
        _merged_away.add(j)
        _pending_edits.pop(j, None)
        if _was_inserted(_structure_edits, j):
            # An inserted piece can't be *inferred* merged away at the
            # next startup the way a parse node is (see _was_inserted),
            # so merging one is recorded as the deletion it amounts to:
            # its text now lives in the destination.
            merged_inserted.append(j)

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
    if merged_inserted:
        # Last, so the unit bookkeeping above still sees the layout the
        # merge was decided against before the rebuild renumbers it.
        candidate = _structure_edits
        for j in merged_inserted:
            candidate = structure.with_edit(candidate, j, deleted=True)
        _structure_edits.clear()
        _structure_edits.update(candidate)
        _rebuild_structure()
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
        _require_live(idx)
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

    previous_type = _current_node(i)["type"]
    _mutate_node(i, type=new_type)
    _recompute_unit_paths(unit_no)
    # Recording just enough to reverse the *type* change (previous_type)
    # is enough on its own to also undo its knock-on effect on every
    # later piece's own displayed label in this unit: those never had
    # their own type changed, only their path recomputed off of this
    # piece's new one (see _recompute_unit_paths) -- restoring this one
    # piece's type and recomputing again naturally un-cascades all of it,
    # with nothing else to track.
    _renest_history.append({"node_index": i, "previous_type": previous_type, "unit_no": unit_no})
    updated = _current_node(i)
    return {"node_index": i, "type": updated["type"], "path": updated.get("path")}


@app.post("/api/renest/undo")
def undo_renest_endpoint():
    """Reverses the most recent successful renest (LIFO -- repeated calls
    walk back through several in a row), restoring the piece's own prior
    type and recomputing the unit's paths again so every other piece's
    label that shifted as a knock-on effect (see renest_endpoint) reverts
    right along with it. This is specifically why a whole-unit
    _recompute_unit_paths, not a hand-patched single path entry, is the
    right undo primitive here: nothing downstream of the renested piece
    ever had its own *type* changed in the first place, only its
    *displayed* nesting, which a fresh recompute off the restored type
    fixes for all of them at once, the same way it did going forward."""
    if not _renest_history:
        raise HTTPException(400, "Nothing to undo")
    entry = _renest_history[-1]
    i, previous_type, unit_no = entry["node_index"], entry["previous_type"], entry["unit_no"]
    if i in _merged_away:
        _renest_history.pop()
        raise HTTPException(400, "That piece has since been merged away and can't be un-nested")
    _renest_history.pop()
    _mutate_node(i, type=previous_type)
    _recompute_unit_paths(unit_no)
    updated = _current_node(i)
    return {"node_index": i, "unit_no": unit_no, "type": updated["type"], "path": updated.get("path")}


# ---------------------------------------------------------------------------
# Restructuring: add, remove, move (see corpus/structure.py)
#
# Edit, split, merge and renest between them can fix a piece that is
# wrong. None of them can fix a piece that is *missing* -- a heading the
# PDF set as an image, a provision the extractor dropped -- or one that
# is there twice, or one the parser attached three sections away from
# where it belongs. These three do, and they are the reason a structural
# fault no longer means re-parsing the document and losing the review.
# ---------------------------------------------------------------------------

def _anchor_before(i: int) -> int:
    """The node this one currently follows -- what it would need to be
    put back after. DOCUMENT_START when it is the first thing in the
    document."""
    position = _order.index(i)
    return _order[position - 1] if position else structure.DOCUMENT_START


def _commit_structure(candidate: dict[int, dict]) -> None:
    """Adopts a proposed set of structural edits, after checking it
    actually describes a document. Tried before it is kept, so a move
    that would place a piece after itself is refused with nothing
    changed."""
    try:
        structure.document_order(len(_nodes), candidate)
    except structure.StructureError as e:
        raise HTTPException(400, str(e)) from e
    _structure_edits.clear()
    _structure_edits.update(candidate)
    _rebuild_structure()


@app.post("/api/nodes/insert")
def insert_node_endpoint(req: InsertRequest):
    """Adds a piece the parse doesn't contain, directly after
    `after_node_index` (-1 for the very start of the document).

    It takes an index above every parse position, so nothing else shifts
    -- every stored decision, link span and finding still names the same
    provision it did before. Page numbers are inherited from the piece it
    follows so the source-PDF panel still opens somewhere useful, and it
    starts unreviewed, because a reviewer typing a provision in is
    exactly as much in need of checking as a parser emitting one."""
    _require_structure_editable()
    node_type = req.type.strip()
    if node_type not in _relabel_types:
        raise HTTPException(400, f"Unknown type: {req.type!r}")
    after = req.after_node_index
    if after != structure.DOCUMENT_START:
        _require_live(after)

    neighbour = _current_node(after) if after != structure.DOCUMENT_START else {}
    index = structure.next_index(len(_nodes), _structure_edits)
    node = {
        "type": node_type,
        "number": (req.number or "").strip() or None,
        "heading": (req.heading or "").strip() or None,
        "text": req.text,
        "page_start": neighbour.get("page_start"),
        "page_end": neighbour.get("page_start"),
        "char_start": None,
        "char_end": None,
        # Says where this came from wherever a node's origin is shown or
        # exported: not a line of the PDF, a person.
        "source": "inserted-in-review",
        "path": dict(neighbour.get("path") or {}),
    }
    placed = structure.place_after(_structure_edits, index, after)
    placed[index]["node"] = node
    _commit_structure(placed)
    unit_no = _unit_of_index[index]
    _recompute_unit_paths(unit_no)
    return {"node_index": index, "unit_no": unit_no, "unit_count": len(_units)}


@app.post("/api/nodes/{node_index}/delete")
def delete_node_endpoint(node_index: int):
    """Removes a piece from the document -- for one the parser invented
    out of a page header, a running footer, or the same provision picked
    up twice.

    Distinct from Merge, which keeps the text and moves it somewhere
    else; this is for text that should not be in the document at all.
    Recorded rather than destroyed: the piece keeps its index and its
    place in the order, so Restore puts it back exactly where it was,
    with whatever had already been decided about it intact."""
    _require_structure_editable()
    _require_live(node_index)
    unit_no = _unit_of_index[node_index]
    unit_indices = [i for i in _units[unit_no] if i not in _merged_away]
    if unit_indices[0] == node_index and len(unit_indices) > 1:
        raise HTTPException(
            400, "Can't delete the section itself while it still has pieces nested under it."
        )
    _commit_structure(structure.with_edit(
        _structure_edits, node_index, after=_anchor_before(node_index), deleted=True,
    ))
    return {"node_index": node_index, "unit_count": len(_units)}


@app.post("/api/nodes/{node_index}/restore")
def restore_node_endpoint(node_index: int):
    """Puts a deleted piece back where it was."""
    _require_structure_editable()
    edit = _structure_edits.get(node_index)
    if edit is None or not edit.get("deleted"):
        raise HTTPException(404, f"Node {node_index} isn't deleted")
    _commit_structure(structure.with_edit(_structure_edits, node_index, deleted=False))
    return {"node_index": node_index, "unit_no": _unit_of_index[node_index], "unit_count": len(_units)}


@app.post("/api/move")
def move_node_endpoint(req: MoveRequest):
    """Puts a piece directly after another one, anywhere in the document.

    Renest changes what a piece *is* (its level); this changes where it
    *sits*, which is the other half of "the parser attached this to the
    wrong place" -- a subsection that belongs to the previous section, a
    note that landed before the provision it annotates. Recorded as
    "follows that piece" rather than as a position, so it stays put as
    other things are inserted and moved around it."""
    _require_structure_editable()
    _require_live(req.node_index)
    after = req.after_node_index
    if after != structure.DOCUMENT_START:
        _require_live(after)
    if after == req.node_index:
        raise HTTPException(400, "A piece can't be placed after itself")

    was_in_unit = _unit_of_index[req.node_index]
    _commit_structure(structure.place_after(_structure_edits, req.node_index, after, _order))
    now_in_unit = _unit_of_index[req.node_index]
    # Both ends: the piece takes its numbering from where it now sits,
    # and the unit it left renumbers without it.
    for unit_no in {was_in_unit, now_in_unit}:
        if unit_no < len(_units):
            _recompute_unit_paths(unit_no)
    moved = _current_node(req.node_index)
    return {
        "node_index": req.node_index, "unit_no": now_in_unit, "unit_count": len(_units),
        "path": moved.get("path"),
    }


_CHAIN_LEVELS = ("subsection", "paragraph", "subparagraph", "sub_subparagraph")


class PlaceRequest(BaseModel):
    reference: str
    # Where among its new siblings; None puts it where the reference says,
    # moving it only if it can't be that where it is.
    after_node_index: "int | None" = None


# The first level of an item or clause unit, where a section has its
# subsections: "4.4" in "4.4(a)", as review labels it.
_SUB_ITEM_LEVELS = ("subitem", "subclause")


def _chain(node: dict) -> str:
    path = node.get("path") or {}
    head = path.get("subitem") or path.get("subclause") or ""
    return head + "".join(f"({path[level]})" for level in _CHAIN_LEVELS if path.get(level))


def _render_reference(segments: list[str], headed: bool) -> str:
    return (segments[0] if headed else f"({segments[0]})") + "".join(f"({seg})" for seg in segments[1:])


def _reference_segments(reference: str) -> tuple[list[str], bool]:
    """The levels a reference names, and whether it opens with a sub-item
    number ("4.4(a)") rather than a bracket ("(1)(d)(vii)")."""
    text = re.sub(r"\s+", "", reference or "")
    m = re.fullmatch(r"(\d+(?:\.\d+)*[A-Z]*)?((?:\([^()]+\))*)", text)
    if not text or not m:
        raise HTTPException(400, f"{reference!r} isn't a reference like (1)(d)(vii) or 4.4(a).")
    head = [m.group(1)] if m.group(1) else []
    return head + re.findall(r"\(([^()]+)\)", m.group(2)), bool(head)


def _block(unit_indices: list[int], i: int, as_type: "str | None" = None) -> list[int]:
    """i and what is nested under it -- the pieces after it that are
    deeper than it, up to the first that isn't. They move together, or a
    moved subparagraph would leave its (A) and (B) hanging off whatever it
    used to follow.

    `as_type` asks what would still be nested under it at another level:
    under both, since a piece moved deeper takes with it only what is
    deeper still. (1)(vii) read as a paragraph has (viii) to (x) nested
    under it; made the subparagraph it is, they become its siblings and
    stay put."""
    rank = make_ranks(_hierarchy)
    types = (_current_node(i)["type"], as_type or _current_node(i)["type"])
    out = [i]
    if any(t not in _NESTABLE_LEVELS for t in types):
        return out
    own = max(rank[t] for t in types)
    for j in unit_indices[unit_indices.index(i) + 1:]:
        t = _current_node(j)["type"]
        level = rank.get(t) if t in _NESTABLE_LEVELS else _depth_rank(j) if t == "continuation" else None
        if level is not None and level <= own:
            break
        out.append(j)
    return out


@app.post("/api/named/{node_id:path}/place")
def place_named(node_id: str, req: PlaceRequest):
    """The place endpoint by a piece's name rather than its position here,
    for History review, which reads the version's text without this
    server's positions."""
    index = next((i for i, name in _node_ids.items() if name == node_id), None)
    if index is None:
        raise HTTPException(404, f"No piece named {node_id!r} in this version.")
    return place_node_endpoint(index, req)


@app.post("/api/nodes/{node_index}/place")
def place_node_endpoint(node_index: int, req: PlaceRequest):
    """Makes a piece the provision a reference names: "(1)(d)(vii)" is a
    subparagraph numbered vii under paragraph (d) of subsection (1).

    A piece's place is its type as much as its order -- nesting is derived
    from the types in sequence (see _recompute_unit_paths), so a
    subparagraph the parser read as a paragraph reads as "(1)(vii)" and
    takes everything after it along. Number, type and order are three
    fields and one decision; this makes it one.

    What is still nested under it at its new level comes with it (see
    _block); what was only under it because of the wrong type stays. It
    moves only when it has to:
    where the reference already fits the piece's place once its type is
    right, it stays; otherwise it goes to the end of its new parent, or
    after `after_node_index` when that is given."""
    _require_live(node_index)
    segments, headed = _reference_segments(req.reference)
    unit_no = _unit_of_index[node_index]
    unit = [i for i in _units[unit_no] if i not in _merged_away]
    if unit[0] == node_index:
        raise HTTPException(400, "The section itself has no reference within it to change.")
    root_type = _current_node(unit[0])["type"]
    if headed and root_type not in ("item", "clause"):
        raise HTTPException(400, f"{req.reference!r} names a sub-item, and this {root_type} has none.")
    old_block = _block(unit, node_index)
    others = [i for i in unit[1:] if i not in old_block]

    wanted_parent = _render_reference(segments[:-1], headed).lower() if segments[:-1] else ""
    parent = None
    if segments[:-1]:
        parent = next((i for i in others if _current_node(i)["type"] in _CHAIN_LEVELS + _SUB_ITEM_LEVELS
                       and _chain(_current_node(i)).lower() == wanted_parent), None)
        if parent is None:
            raise HTTPException(400, f"This section has no {wanted_parent} to put it under.")
        parent_type = _current_node(parent)["type"]
        # A sub-item's own list is paragraphs, as a subsection's is.
        level = 1 if parent_type in _SUB_ITEM_LEVELS else _CHAIN_LEVELS.index(parent_type) + 1
        if level >= len(_CHAIN_LEVELS):
            raise HTTPException(400, "Nothing nests deeper than a sub-subparagraph.")
        new_type = _CHAIN_LEVELS[level]
    elif headed:
        new_type = "subclause" if root_type == "clause" else "subitem"
    else:
        # A section's top level is whatever its other pieces start at:
        # subsections, or paragraphs straight under the section.
        new_type = next((_current_node(i)["type"] for i in others if _current_node(i)["type"] in _CHAIN_LEVELS),
                        "subsection")
    if new_type not in _relabel_types:
        raise HTTPException(400, f"This document has no {new_type} type.")

    block = _block(unit, node_index, new_type)
    after = req.after_node_index
    if after is not None:
        if after in block:
            raise HTTPException(400, "A piece can't be placed after itself or something nested under it.")
        if after != structure.DOCUMENT_START:
            _require_live(after)

    _mutate_node(node_index, type=new_type, number=segments[-1])
    _recompute_unit_paths(unit_no)

    wanted = _render_reference(segments, headed)
    if after is None and _chain(_current_node(node_index)).lower() != wanted.lower():
        # The end of the new parent's own list, or of the section.
        if parent is not None:
            anchor_block = _block([i for i in _units[unit_no] if i not in _merged_away and i not in block], parent)
            after = anchor_block[-1]
        else:
            after = others[-1] if others else unit[0]
    if after is not None:
        _require_structure_editable()
        edits = _structure_edits
        previous = after
        for j in block:
            edits = structure.place_after(edits, j, previous, structure.document_order(len(_nodes), edits))
            previous = j
        _commit_structure(edits)
        for u in {unit_no, _unit_of_index[node_index]}:
            if u < len(_units):
                _recompute_unit_paths(u)

    placed = _current_node(node_index)
    got = _chain(placed)
    return {"node_index": node_index, "unit_no": _unit_of_index[node_index], "type": placed["type"],
            "reference": got, "matches": got.lower() == wanted.lower(), "moved": after is not None}


# ---------------------------------------------------------------------------
# A piece from a box drawn on the page
# ---------------------------------------------------------------------------

_MARKERS = (
    (re.compile(r"^\((\d+[A-Z]*)\)\s*"), "subsection"),
    (re.compile(r"^\(([ivxl]+[a-z]?)\)\s*"), "subparagraph"),
    (re.compile(r"^\(([a-z]{1,3})\)\s*"), "paragraph"),
    (re.compile(r"^\(([A-Z]{1,2})\)\s*"), "sub_subparagraph"),
    (re.compile(r"^(\d+[A-Z]*)\s+(?=[A-Z])"), "section"),
)


class BoxRequest(BaseModel):
    rect: dict


class BoxCreateRequest(BaseModel):
    rect: dict
    type: str
    number: "str | None" = None
    after_node_index: int


def _clean_rect(raw: dict) -> dict:
    try:
        rect = {"page": int(raw["page"]), **{k: round(float(raw[k]), 1) for k in ("x0", "y0", "x1", "y1")}}
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(400, f"Not a rectangle: {raw!r}") from e
    if rect["x1"] <= rect["x0"] or rect["y1"] <= rect["y0"]:
        raise HTTPException(400, "A box needs width and height")
    return rect


def _body_printed_in(rect: dict) -> str:
    lines = _printed_lines()
    if not lines:
        raise HTTPException(503, "The source PDF this was parsed from isn't where the parse says it is.")
    printed = ""
    for line in lines_in_rects(lines, [rect]):
        if line.text.strip():
            printed = join_printed_line(printed, line.text.strip())
    return printed


def _printed_in(rect: dict) -> str:
    printed = _body_printed_in(rect)
    if not printed:
        raise HTTPException(400, "There is nothing printed inside that box.")
    return printed


def _page_text_in(rect: dict) -> str:
    """Whatever the PDF prints inside a box, read from the page itself.

    Not from the extracted lines, which hold the body only: a history note
    the parser missed is missing because extraction never classed it as a
    margin note -- it fell in the header band, or merged into the note
    above -- so the lines the parse was built from are the one place it
    cannot be found. Joined with spaces, as extract_pages joins a margin
    block's lines."""
    doc = _get_pdf_doc()
    if not (1 <= rect["page"] <= doc.page_count):
        raise HTTPException(404, f"This Act's source PDF has pages 1-{doc.page_count}; no page {rect['page']}")
    clip = pymupdf.Rect(rect["x0"], rect["y0"], rect["x1"], rect["y1"])
    return " ".join(doc[rect["page"] - 1].get_text("text", clip=clip).split())


def _printed_beside(rect: dict) -> "int | None":
    """The live piece a margin note is most likely about: the one whose
    box on the same page starts nearest the note's own top. The Act sets
    a note level with the provision it amends."""
    best, best_gap = None, None
    for i in _order:
        if i in _merged_away:
            continue
        for r in _rects_for(i):
            if r.get("page") != rect["page"] or r.get("y0") is None:
                continue
            gap = abs(r["y0"] - rect["y0"])
            if best_gap is None or gap < best_gap:
                best, best_gap = i, gap
    return best if best is not None else _printed_before(rect)


def _printed_before(rect: dict) -> "int | None":
    """The live piece printed nearest above a box, reading down the pages
    -- where a piece drawn there most likely belongs in the document."""
    best, best_at = None, None
    here = (rect["page"], rect["y0"])
    for i in _order:
        if i in _merged_away:
            continue
        for r in _rects_for(i):
            at = (r.get("page"), r.get("y0"))
            if None in at or at > here:
                continue
            if best_at is None or at >= best_at:
                best, best_at = i, at
    return best


@app.post("/api/boxes/read")
def read_new_box_endpoint(req: BoxRequest):
    """What a box drawn over nothing says, and a first guess at what it
    is and where it goes -- for the reviewer to confirm, not to act on.
    The type is a guess from the marker alone, which is why it is asked:
    "(i)" is a subparagraph or the ninth paragraph depending on what came
    before it (see rule_parser.read_box on why a box can't decide that)."""
    rect = _clean_rect(req.rect)
    printed = _body_printed_in(rect)
    if not printed:
        # Nothing of the body in it, so a box drawn in the margin: offered
        # as a history note for the provision beside it.
        note = _page_text_in(rect)
        if not note:
            raise HTTPException(400, "There is nothing printed inside that box.")
        beside = _printed_beside(rect)
        return {
            "kind": "note", "text": note, "node_index": beside,
            "node_label": _piece_reference(beside) if beside is not None else None,
        }
    node_type, number = None, None
    for pattern, kind in _MARKERS:
        m = pattern.match(printed)
        if m:
            node_type, number = kind, m.group(1)
            break
    if node_type is None and re.match(r"^(Note|Example)s?\b", printed):
        node_type = printed.split()[0].rstrip("s:").lower()
    after = _printed_before(rect)
    return {
        "kind": "piece",
        "text": printed, "type": node_type if node_type in _relabel_types else None, "number": number,
        "after_node_index": after,
        "after_label": _piece_reference(after) if after is not None else None,
    }


def _piece_reference(i: int) -> str:
    """How the pickers name a piece: its section and its place in it."""
    unit = [j for j in _units[_unit_of_index[i]] if j not in _merged_away]
    root = _current_node(unit[0])
    node = _current_node(i)
    where = f"{root['type'].capitalize()} {root.get('number') or ''}".strip()
    own = _chain(node) if node["type"] in _CHAIN_LEVELS else node["type"] if i != unit[0] else ""
    return f"{where} {own}".strip()


@app.post("/api/boxes/create")
def create_from_box_endpoint(req: BoxCreateRequest):
    """A new piece where the reviewer drew a box, read from that box.

    For what the parse missed altogether -- text it dropped, a provision
    it swallowed into its neighbour. Inserted as Insert does, with the box
    as its box, then read the way Read-from-box reads any piece."""
    rect = _clean_rect(req.rect)
    _printed_in(rect)
    rank = make_ranks(_hierarchy)
    # A heading-only provision keeps its words in `heading` (see
    # rule_parser.read_box): a placeholder there is what sends the read
    # down that branch.
    heading_only = req.type == "heading_group" or (
        req.type in rank and "section" in rank and rank[req.type] < rank["section"])
    created = insert_node_endpoint(InsertRequest(
        after_node_index=req.after_node_index, type=req.type, number=(req.number or "").strip() or None,
        heading="?" if heading_only else None, text=""))
    i = created["node_index"]
    _mutate_node(i, page_start=rect["page"], page_end=rect["page"], source="drawn-in-review")
    _node_rects[i] = [rect]
    db.save_node_rects(_act, _node_id(i), [rect], node_index=i)
    _mutate_node(i, **_read_box(i))
    _recompute_unit_paths(_unit_of_index[i])
    node = _current_node(i)
    return {"node_index": i, "unit_no": _unit_of_index[i], "unit_count": len(_units),
            "type": node["type"], "number": node.get("number"), "heading": node.get("heading"),
            "text": node.get("text")}


@app.post("/api/nodes/{node_index}/blind-guess")
def blind_guess_endpoint(node_index: int, req: BlindGuessRequest):
    """Records a reviewer's own classification of a piece, made from its
    text alone, before the review panel reveals what the parser actually
    produced (see _is_elevated_risk for which pieces are offered this).

    Offered, not required. It used to block Accept until it was done,
    which cost more attention than it bought: the pieces it gated are
    rare, and a reviewer who has already read the text does not become
    more careful by being made to type it out again. What it is still
    for is blind_review_stats -- the only measure of the parser's
    accuracy that comes from a person rather than from the parser
    agreeing with itself.

    Comparison is exact-match on type, and on number normalised
    the same light way a human would read it (case/bracket-insensitive:
    "(A)" and "a" count as the same answer) -- this is reported back to
    the reviewer, not judged; disagreeing with the parser is a fine,
    useful outcome, not an error."""
    _require_live(node_index)
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
        _act, _node_id(node_index), node_index=node_index, guessed_type=req.type, guessed_number=(req.number or None),
        guessed_heading=(req.heading or None), reasoning=req.reasoning.strip(),
        matched_type=matched_type, matched_number=matched_number,
    )
    return {
        "node_index": node_index,
        "review": record,
        "actual": {"type": actual["type"], "number": actual.get("number"), "heading": actual.get("heading")},
    }


def _ai_suggestion_precondition(node_index: int) -> dict:
    """The elevated-risk finding an AI suggestion would answer, or raises
    an HTTPException if this node isn't a valid target for one (yet).

    Refuses before a reviewer's own blind_reviews row exists for this
    node on purpose -- see corpus.ai.assist's own module docstring
    on why: an AI suggestion is a third opinion to weigh against a
    human's own independent one and the parser's, never a first one
    read before forming that independent view in the first place."""
    _require_live(node_index)
    if not _is_elevated_risk(node_index):
        raise HTTPException(400, "This piece isn't flagged by diagnostics -- there's nothing here for a second opinion to weigh in on.")
    if db.get_blind_review(_act, _node_id(node_index)) is None:
        raise HTTPException(
            400,
            "Record your own independent assessment above first -- an AI suggestion is a second "
            "opinion to weigh against yours, not a first one to read before forming it.",
        )
    finding = next((f for f in _findings_by_node.get(node_index, []) if f.get("severity") in ("error", "warning")), None)
    if finding is None:
        raise HTTPException(400, "No warning or error finding on this piece to ask about.")
    return finding


@app.post("/api/nodes/{node_index}/ai-suggest")
def ai_suggest_endpoint(node_index: int):
    """A local model's second opinion on an elevated-risk piece (see
    corpus/ai/assist.py), asked only once the reviewer's own
    independent blind-review guess is already recorded (see
    _ai_suggestion_precondition), and cached (see db.save_ai_suggestion)
    so asking again doesn't needlessly re-run the model. Never applied
    to the piece automatically -- the review panel shows it alongside
    the reviewer's own guess and the parser's actual answer, for a
    human to weigh, same as every other signal here.

    503s with the backend's own message (see
    corpus.ai.backend.OllamaBackend.ensure_ready) if the local
    model isn't set up yet -- that message already names the exact next
    command to run (see install_ai_model.py), so it's passed through
    rather than wrapped."""
    finding = _ai_suggestion_precondition(node_index)
    # Document order, not index order: build_suggestion reads forward
    # from the piece to the end of its unit, which is only the right
    # neighbours while the list it walks is in reading order (see
    # ai_assist._history_low_confidence_context). So the piece is handed
    # over by where it sits, not by the index it is stored under.
    live = [i for i in _order if i not in _merged_away]
    current_nodes = [_current_node(i) for i in live]
    try:
        suggestion = build_suggestion(finding, live.index(node_index), current_nodes)
    except OllamaUnavailable as e:
        raise HTTPException(503, str(e)) from e
    record = db.save_ai_suggestion(
        _act, _node_id(node_index), node_index=node_index, answer=suggestion["answer"],
        reasoning=suggestion["reasoning"], confidence=suggestion["confidence"],
        model=suggestion["model"],
    )
    return {"node_index": node_index, "suggestion": record}


# ---------------------------------------------------------------------------
# Node types ("legislation part" types)
# ---------------------------------------------------------------------------
def _builtin_type_names() -> list[str]:
    """The types this document can be labelled with: any level its own
    profile declares that the schema doesn't know about, then the set for
    its kind of document -- an Act is never asked whether something is a
    "clause", and a Bill is never offered "section" (see
    schema.TYPES_BY_DOCUMENT).

    Anything a node actually carries is appended regardless. Filtering
    must never leave a piece's own type missing from the list it would be
    relabelled with: that would make the dropdown silently reassign it on
    the reviewer's next edit."""
    custom_levels = [t for t in _hierarchy if t not in NODE_TYPES]
    in_use = [t for t in _type_usage() if t]
    return list(dict.fromkeys([*custom_levels, *types_for_document(_document_type), *in_use]))


def _refresh_relabel_types() -> None:
    """Rebuilds the relabel list in place. _relabel_types is a module-level
    list other endpoints validate against by identity, so it's mutated
    rather than rebound."""
    builtins = _builtin_type_names()
    customs = [t for t in db.load_custom_types(_act) if t not in builtins]
    _relabel_types[:] = [*builtins, *customs]


def _type_usage() -> dict[str, int]:
    """How many live nodes currently carry each type, counted against each
    node's *effective* state (a pending edit, else its verified state,
    else the parse) -- the same view the reviewer is looking at, so a
    type they've just relabelled the last node away from reads as unused
    straight away."""
    counts: dict[str, int] = {}
    for i in _order:
        if i in _merged_away:
            continue
        t = _current_node(i).get("type")
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _node_types_payload() -> dict:
    builtins = set(_builtin_type_names())
    usage = _type_usage()
    return {
        "types": [
            {"name": t, "builtin": t in builtins, "in_use": usage.get(t, 0)}
            for t in _relabel_types
        ]
    }


def _reassign_type(old_type: str, new_type: str) -> int:
    """Moves every live node off old_type onto new_type, through the same
    _mutate_node path a hand relabel uses -- so each reassignment is
    logged as a correction and persisted (or staged as a pending edit for
    a not-yet-committed piece) exactly as if it had been done one at a
    time in the GUI. Returns how many nodes moved."""
    moved = 0
    for i in _order:
        if i in _merged_away:
            continue
        if _current_node(i).get("type") == old_type:
            _mutate_node(i, type=new_type)
            moved += 1
    return moved


@app.get("/api/node-types")
def list_node_types():
    return _node_types_payload()


@app.post("/api/node-types")
def create_node_type(req: NodeTypeCreateRequest):
    try:
        name = validate_custom_type_name(req.name, _relabel_types)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.add_custom_type(_act, name)
    _refresh_relabel_types()
    return {"created": name, **_node_types_payload()}


@app.post("/api/node-types/{name}/rename")
def rename_node_type(name: str, req: NodeTypeRenameRequest):
    """Renaming carries every node using the old name across to the new
    one, so a rename is never a silent way of orphaning nodes onto a type
    that no longer exists. Built-in types can't be renamed: they're what
    the parser emits, hierarchy.py ranks and akn_export.py maps to AkomaNtoso
    elements, so renaming one here would only desynchronise this Act's
    review state from the rest of the pipeline."""
    if name not in _relabel_types:
        raise HTTPException(404, f"Unknown type {name!r}")
    if name in _builtin_type_names():
        raise HTTPException(400, f"{name!r} is a built-in type and can't be renamed.")
    try:
        new_name = validate_custom_type_name(req.new_name, _relabel_types)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.rename_custom_type(_act, name, new_name)
    _refresh_relabel_types()
    moved = _reassign_type(name, new_name)
    return {"renamed": name, "to": new_name, "reassigned": moved, **_node_types_payload()}


@app.delete("/api/node-types/{name}")
def delete_node_type(name: str, req: NodeTypeDeleteRequest | None = None):
    """The safeguard: a type that nodes are still using is never simply
    removed. Either nothing uses it (delete outright), or the caller names
    an existing replacement type every one of those nodes is moved to
    first. Refusing without a replacement returns 409 with the usage count,
    which is what the GUI turns into its "N pieces still use this" prompt.

    Built-in types can't be deleted at all -- the parser will just emit
    them again on the next parse, and hierarchy.py/akn_export.py still
    expect them, so "deleting" one would be a lie."""
    if name not in _relabel_types:
        raise HTTPException(404, f"Unknown type {name!r}")
    if name in _builtin_type_names():
        raise HTTPException(400, f"{name!r} is a built-in type and can't be deleted.")
    in_use = _type_usage().get(name, 0)
    replacement = (req.replacement or "").strip() if req is not None else ""
    if in_use and not replacement:
        raise HTTPException(
            409,
            f"{name!r} is still used by {in_use} piece(s) -- choose a type to move them to first.",
        )
    moved = 0
    if in_use:
        if replacement == name:
            raise HTTPException(400, "The replacement type must be a different type.")
        if replacement not in _relabel_types:
            raise HTTPException(400, f"Unknown replacement type {replacement!r}")
        moved = _reassign_type(name, replacement)
    db.delete_custom_type(_act, name)
    _refresh_relabel_types()
    return {"deleted": name, "reassigned_to": replacement or None, "reassigned": moved, **_node_types_payload()}


@app.post("/api/nodes/{node_index}/accept")
def accept_node(node_index: int, req: AcceptRequest):
    """Accepts or flags exactly one piece, independent of the rest of its
    unit -- unlike /api/units/{unit_no}/accept, this never requires the
    whole unit to be ready at once. A unit's own status (and its sidebar
    dot) still only turns done/flagged once *every* one of its pieces has
    been decided one way or another, whether that happened here one at a
    time or via that whole-unit endpoint; see _unit_status."""
    _require_live(node_index)
    node = _accept_node(node_index, req.flagged)
    _note_pieces([node_index], req)
    return {
        "node_index": node_index,
        "verified_at": node.get("verified_at"),
        "needs_followup": bool(node.get("needs_followup")),
        # The piece's own status, worked out the one way it is worked
        # out -- the box drawn over it on the page is coloured by this
        # too, and deriving it a second time in the browser is how the
        # two came to disagree.
        "status": _piece_status(node_index),
        # Which unit was affected, not an assumption that it was the one
        # open in the panel: a box on the page can be accepted while a
        # different unit is being read beside it.
        "unit_no": _unit_of_index[node_index],
        "unit_status": _unit_status(_unit_of_index[node_index]),
    }


@app.post("/api/units/{unit_no}/accept")
def accept_unit(unit_no: int, req: AcceptRequest):
    """Accepts (or flags) everything in this unit that is still
    outstanding. Flagging a unit is a reviewer saying "come back to this",
    so accepting one that is already flagged is the whole point of having
    flagged it -- that is what clears the flag and finishes the unit.
    Only a unit that is already fully accepted has nothing left to do."""
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    status = _unit_status(unit_no)
    if status == "done":
        raise HTTPException(400, "Every piece in this unit is already accepted -- edit its pieces directly instead.")
    if req.flagged and status == "flagged":
        raise HTTPException(400, "This unit is already flagged for follow-up.")

    # Whatever hasn't been decided at all yet, plus -- when accepting --
    # whatever was previously flagged, since resolving those flags is
    # exactly what "accept this unit" means once it has been through
    # review once. Pieces already accepted are left alone either way:
    # this is "finish what's outstanding", not "redo the whole unit and
    # overwrite decisions already made piece by piece".
    live = [i for i in _units[unit_no] if i not in _merged_away]
    outstanding = [i for i in live if not _is_committed(i)]
    flagged = [] if req.flagged else [
        i for i in live if _is_committed(i) and _verified_by_source_index[i].get("needs_followup")
    ]
    if outstanding:
        unit_orig = [_parse_node(i) for i in outstanding]
        unit_nodes = [_current_node(i) for i in outstanding]
        before = len(_verified)
        commit_unit(unit_nodes, unit_orig, _act, _verified, flagged=req.flagged, unit_index=unit_no)
        for i, v in zip(outstanding, _verified[before:]):
            v["_source_node_index"] = i
            v["_node_id"] = _node_id(i)
            _verified_by_source_index[i] = v
            _pending_edits.pop(i, None)
        save_verified(_act, _verified)
    for i in flagged:
        # Already in `verified`, so this updates the row in place rather
        # than appending a second one for the same node.
        _accept_node(i, False)
    _note_pieces(outstanding + flagged, req)
    return {"unit_no": unit_no, "status": _unit_status(unit_no)}


@app.post("/api/units/{unit_no}/clear")
def clear_unit_endpoint(unit_no: int):
    """Drops every stored decision in one unit, so the whole section comes
    back undecided against whatever the parse says now.

    The same thing reset_node_endpoint does for one piece, for a Section
    with thirty of them. A parser fix rarely changes a single provision --
    it changes how a whole section was read -- and going through that
    section one piece at a time to say so is work the fix was supposed to
    save."""
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    cleared = 0
    for i in _units[unit_no]:
        # A merge is a decision too. A finished unit's pieces missing from
        # its decisions are taken to have been merged away -- so after a
        # re-parse of a section you had flagged, every piece the parser
        # now reads differently vanished, box and all, even once cleared.
        _merged_away.discard(i)
        _pending_edits.pop(i, None)
        row = _verified_by_source_index.pop(i, None)
        if row is not None:
            _verified[:] = [v for v in _verified if v is not row]
            cleared += 1
    if cleared:
        save_verified(_act, _verified)
    return {"unit_no": unit_no, "cleared": cleared, "status": _unit_status(unit_no)}


def reparse_command(document_type: "str | None", pdf_path: str) -> list[str]:
    """How to parse this document again, from the outside.

    By module, never by filename. This used to run "run_pipeline.py",
    which was true while that script sat at the repository root and
    silently false afterwards: the button failed with python complaining
    about a file nobody had moved on purpose.

    An Explanatory Memorandum has its own pipeline. run_pipeline only
    knows "act" and "bill" -- passing it --document-type em was refused by
    its own argument parser -- which is the same split dashboard.py's
    _parse_command makes."""
    if document_type == "em":
        return [sys.executable, "-m", "corpus.parsing.run_em_pipeline", pdf_path]
    cmd = [sys.executable, "-m", "corpus.parsing.run_pipeline", pdf_path]
    if document_type == "bill":
        cmd += ["--document-type", "bill"]
    return cmd


@app.post("/api/units/{unit_no}/reparse")
def reparse_unit_endpoint(unit_no: int):
    """Parses this document again and brings just this section back
    undecided against the result.

    There is no such thing as parsing one section on its own: the parser
    reads the document as one stream of lines, and where a section starts
    depends on everything before it. So the whole document is parsed --
    which for a 530-page Act is about two seconds -- and the pipeline's
    own re-anchoring carries every stored decision across onto the
    provision it describes (see corpus/parsing/reparse.py). Then the
    decisions for *this* section are dropped, so it is the one part of the
    document that comes back fresh.

    That is the useful shape of "re-parse this section": the section is
    re-read from the PDF, and the review work everywhere else survives.

    The unit is found again by what its opening provision *is* rather
    than by its number, because a re-parse can add or remove nodes
    earlier in the document and move every unit after them."""
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    if not _source_pdf_path:
        raise HTTPException(400, "This document has no source PDF recorded, so it can't be parsed again.")
    if json.loads((Path("data/parsed") / f"{_act}.json").read_text(encoding="utf-8")).get("slim"):
        raise HTTPException(409, "This version is kept slim -- its PDF has only the pages its margin notes say "
                                 "changed. Fetch it again from History review to re-read it.")
    root = _parse_node(_units[unit_no][0])
    identity = (root["type"], root.get("number"), root.get("heading"))
    before = _unit_snapshot(unit_no)

    cmd = reparse_command(_document_type, _source_pdf_path)
    result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise HTTPException(500, f"Re-parsing {_act} failed:\n{result.stdout}{result.stderr}"[:2000])

    _load_state(_act)
    found = next(
        (u for u, indices in enumerate(_units)
         if (_parse_node(indices[0])["type"], _parse_node(indices[0]).get("number"),
             _parse_node(indices[0]).get("heading")) == identity),
        None,
    )
    if found is None:
        # The parse no longer has this section at all. Everything else is
        # already re-anchored, so this is a real finding rather than a
        # failure -- say so instead of clearing a different section.
        return {"unit_no": None, "cleared": 0, "reparsed": True, "changes": None,
                "detail": f"{_act} was parsed again, but no section matching "
                          f"{identity[1] or identity[0]} is in the new parse."}
    cleared = clear_unit_endpoint(found)
    return {"unit_no": found, "cleared": cleared["cleared"], "reparsed": True,
            "changes": _unit_changes(before, _unit_snapshot(found)),
            "detail": f"{_act} was parsed again; this section's {cleared['cleared']} decision(s) were cleared."}


def _unit_snapshot(unit_no: int) -> list[dict]:
    """A unit's pieces as the parse has them and as they are drawn on the
    page -- what a re-parse is asked to reconsider."""
    return [{"said": (_parse_node(i).get("type"), _parse_node(i).get("number")),
             "rects": tuple(tuple(sorted(r.items())) for r in _parse_node(i).get("rects") or []),
             "drawn": i in _node_rects,
             "flagged": bool((_verified_by_source_index.get(i) or {}).get("needs_followup"))}
            for i in _units[unit_no] if i not in _merged_away]


def _unit_changes(before: list[dict], after: list[dict]) -> dict:
    """What a re-parse did to one unit, so the page can say it: without
    this the boxes were redrawn (or weren't) and nothing told you which.
    Compared by what the pieces say and where they print, not by name: a
    piece read at a new level gets a new name, and is exactly the change
    worth counting."""
    from collections import Counter

    def boxes(snapshot):
        return Counter(p["rects"] for p in snapshot if not p["drawn"] and p["rects"])

    return {
        "pieces_before": len(before), "pieces_after": len(after),
        "changed": sum((Counter(p["said"] for p in after) - Counter(p["said"] for p in before)).values())
                   + max(0, len(before) - len(after)),
        "boxes_redrawn": sum((boxes(after) - boxes(before)).values()),
        "drawn_kept": sum(p["drawn"] for p in after),
        # A flag is a decision like any other, and a re-parse clears this
        # unit's decisions -- worth saying, since it was put there on purpose.
        "flag_cleared": any(p["flagged"] for p in before),
    }


class CarriedFromRequest(BaseModel):
    key: "list | None" = None


@app.post("/api/units/{unit_no}/carried-from")
def carried_from_endpoint(unit_no: int, req: CarriedFromRequest):
    """Records that this unit's provision is one the version before
    numbered differently -- or, with no key, that it is not."""
    if not (0 <= unit_no < len(_units)):
        raise HTTPException(404, "No such unit")
    indices = [i for i in _units[unit_no] if i not in _merged_away]
    payload = _unit_lineage_payload(unit_no, [_current_node(i) for i in indices], indices)
    if not payload or "carry_candidates" not in payload:
        raise HTTPException(400, "Only a provision the version before does not have can be carried from one.")
    if req.key is not None and req.key not in [c["key"] for c in payload["carry_candidates"]]:
        raise HTTPException(400, "The version before has no such provision to carry from.")
    db.set_provision_link(_act, tuple(payload["key"]), tuple(req.key) if req.key else None)
    _load_versions()
    return _unit_payload(unit_no)


@app.get("/api/links")
def get_links():
    return load_links(_act)


@app.post("/api/links")
def post_link(req: LinkRequest):
    _require_live(req.node_index)
    node_text = _current_node(req.node_index).get("text") or ""
    span_text = node_text[req.start : req.end]
    target = resolve_link(req.label, span_text, _nodes, _definition_index)
    try:
        return add_link(_act, _node_id(req.node_index), req.start, req.end, req.label,
                        node_text, target=target, node_index=req.node_index)
    except LinkError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/links/{link_id}")
def remove_link(link_id: str):
    if not delete_link(_act, link_id):
        raise HTTPException(404, "No such link")
    return {"ok": True}


def _load_state(act: str, restart: bool = False) -> None:
    """Reads everything this process serves for one document: its parse,
    its source PDF, the units that parse groups into, and every stored
    decision about it.

    Called once at startup, and again by reparse_unit_endpoint -- a
    re-parse rewrites data/parsed/<act>.json underneath this process,
    and serving the node list loaded before it would mean answering from
    a parse that no longer exists. Every derived container is rebuilt
    from scratch rather than added to, so nothing from the previous parse
    survives into the new one."""
    global _act, _nodes, _units, _verified, _definition_index, _parse_fingerprint
    global _unattached_notes, _hierarchy, _relabel_types, _startup_resume_unit
    global _source_pdf_path, _act_title, _document_type, _positions_trusted
    global _pdf_doc, _structure_edits, _order, _structure_editable, _node_rects
    global _printed_lines_cache, _profile_name, _unplaced_edits

    _act = act
    _unit_of_index.clear()
    _verified_by_source_index.clear()
    _pending_edits.clear()
    _merged_away.clear()
    _findings_by_node.clear()
    _page_image_cache.clear()
    if _pdf_doc is not None:
        _pdf_doc.close()
        _pdf_doc = None

    _printed_lines_cache = None
    _profile_name = load_parse_profile(act)
    _nodes, _unattached_notes, _hierarchy, _parse_fingerprint = load_parsed(act)
    _source_pdf_path = load_source_pdf_path(act)
    _document_type = load_document_type(act)
    # Computed once here, not per-request: _detect_act_citation re-reads
    # and re-extracts the *whole* source PDF via PyMuPDF just to find the
    # title on its first couple of pages (see dashboard.py's own
    # _act_title_cache, added after that exact cost showed up per page
    # view there -- one Act per process here, so once at startup is enough).
    from corpus.exporters.akn_export import _detect_act_citation

    _act_title = _detect_act_citation(_source_pdf_path).get("title") or act
    # A restart throws away every decision about this document, and a
    # reviewer's inserts, deletions and moves are decisions -- leaving
    # them would restart the review against a structure nothing else
    # remembers agreeing to.
    _structure_editable = positions_are_trustworthy(act, _parse_fingerprint)
    if restart:
        _structure_edits, _unplaced_edits = {}, {}
        db.save_structure_edits(act, {})
    else:
        stored = load_structure_edits(act, _parse_fingerprint)
        _structure_edits, _unplaced_edits = place_edits(_nodes, stored)
    _name_new_inserts()
    # Gated the same way, and for the same reason: a box is recorded
    # against a node *position*, so against a parse those positions no
    # longer describe it would be drawn over the wrong provision.
    _node_rects = {}
    if _structure_editable and not restart:
        _index_of_name = name_index(_node_ids.items())
        for node_id, rects in db.load_node_rects(act).items():
            index = _index_of_name.get(node_id)
            if index is not None:
                _node_rects[index] = rects
    _order, _units = order_and_units(len(_nodes), _structure_edits, _parse_node)
    for u, indices in enumerate(_units):
        for i in indices:
            _unit_of_index[i] = u
    _definition_index = build_definition_index(_nodes)

    for finding in load_diagnostics(act):
        if finding.get("node_index") is not None:
            _findings_by_node.setdefault(finding["node_index"], []).append(finding)

    # ai_scan_findings holds one row per unit run_ai_review.py has already
    # scanned, including a "clean" row for a unit it looked at and found
    # nothing wrong with (see db.py's own table comment) -- only the
    # genuine concerns join diagnostics' own findings here. Gated on
    # positions_are_trustworthy for the same reason blind_reviews and
    # ai_suggestions already are: these rows are cached against a node
    # position from whenever the scan ran, and a re-parse that wasn't
    # re-anchored can no longer vouch for what that position now holds.
    if positions_are_trustworthy(act, _parse_fingerprint):
        for row in db.load_ai_scan_findings(act):
            if row["severity"] != "clean":
                _findings_by_node.setdefault(row["node_index"], []).append({
                    "severity": row["severity"],
                    "category": "ai-scan",
                    "message": row["message"],
                    "node_index": row["node_index"],
                })

    _verified = [] if restart else load_verified(act)
    placed, _unplaced_verified[:] = verified_by_index(_verified, _node_ids)
    _flag_model_disagreements(placed)
    # _verified is what the session works with, so it holds only what this
    # parse can place; the rest travels in _unplaced_verified. Compared by
    # identity rather than value, because two rows can be equal.
    kept = {id(row) for row in placed.values()}
    _verified[:] = [row for row in _verified if id(row) in kept]
    _verified_by_source_index.update(placed)
    for index, row in _verified_by_source_index.items():
        # The position a row is about, in this parse rather than in the
        # one it was written against.
        row["_source_node_index"] = index
    if not _verified and _parse_fingerprint:
        # Nothing reviewed yet, so whatever gets accepted from here on
        # belongs to this parse -- record that now, rather than leaving
        # the first session's work unattributable to any parse at all.
        db.save_parse_fingerprint(act, _parse_fingerprint)
    # After the verified rows are in: the type list includes every type
    # actually in use (see _builtin_type_names), and a reviewer's own
    # relabel lives in those rows, not in the parse.
    _refresh_relabel_types()
    _positions_trusted = positions_are_trustworthy(act, _parse_fingerprint)
    _startup_resume_unit = _resume_point(_units, _verified, markers_are_complete=_positions_trusted)
    # Reconstruct which nodes were merged away in a prior session: any
    # index belonging to an already-fully-processed unit (before the
    # resume point) that never made it into `verified` at all -- a
    # node merged away is simply never appended there (see the merge
    # endpoint) -- must have been merged into something else rather
    # than just not-yet-reached. Only sound while the stored rows and
    # this parse still agree on what an index means; see
    # positions_are_trustworthy, and build_current_nodes for the same
    # guard on the read-only path.
    if _positions_trusted:
        for u in finished_units(_units, _verified, markers_are_complete=_positions_trusted):
            for i in _units[u]:
                if i not in _verified_by_source_index and not _was_inserted(_structure_edits, i):
                    _merged_away.add(i)
    _load_versions()


# ---------------------------------------------------------------------------
# One version of a work among others
# ---------------------------------------------------------------------------


def _load_versions() -> None:
    """Works out, once per load, which of this version's units another
    version vouches for (corpus/review/inheritance.py). A reviewer here
    sees those as done elsewhere and is shown only what differs."""
    global _work_review, _reference_nodes, _amending_acts, _instructions
    _unit_lineage.clear()
    _reference_nodes = None
    _amending_acts = {}
    _instructions = None
    _work_review = inheritance.work_review(_act)
    if not _work_review:
        return
    version = _work_review["version"]
    state = _work_review["states"][version]
    statuses = _work_review["status"][version]
    key_of_root = {unit["root"]: key for key, unit in state["units"].items()}
    for u, indices in enumerate(_units):
        key = key_of_root.get(indices[0]) if indices else None
        if key in statuses:
            _unit_lineage[u] = {**statuses[key], "key": key}


def _version_info() -> "dict | None":
    if not _work_review:
        return None
    version = _work_review["version"]
    versions = _work_review["versions"]
    own = next(v for v in versions if v["version"] == version)
    reference = next((v for v in versions if v["version"] == _work_review["reference"]), None)
    return {
        "version": version,
        "versions": [{k: v[k] for k in ("version", "slug", "as_at_printed", "to_review", "current")}
                     for v in versions],
        "to_review": sum(1 for u in _unit_lineage.values() if u["status"] == lineage.TO_REVIEW),
        "reference": _work_review["reference"],
        # A queue swollen by the parsers' own disagreements rather than
        # by Parliament: re-parsing this version is what shrinks it.
        "parser_differs": bool(reference and reference["parser_version"] != own["parser_version"]),
    }


def _reference_provisions() -> dict:
    """The reference version's provisions as a reader sees them, read
    once per load -- what a changed unit here is compared against."""
    global _reference_nodes
    if _reference_nodes is None:
        reference = _work_review["reference"]
        _reference_nodes = inheritance.effective_nodes(_work_review["slugs"][reference], _work_review, reference)
    return diffing.provisions(_reference_nodes)


def _key_label(key: tuple) -> str:
    # A unit with no number is keyed by its own name, in two parts rather
    # than three (inheritance.unit_key).
    if key[0] == "unit":
        return key[1]
    kind, schedule, number = key
    if kind != "provision":
        return f"{kind.capitalize()} {number.upper()}"
    return f"Schedule {schedule} clause {number.upper()}" if schedule else f"Section {number.upper()}"


def _changed_pieces(older: list[dict], newer: list[dict], root: int, versions: tuple) -> dict:
    """The pieces that differ, each on its own, and which of this tool's
    pieces they are -- a section is reviewed here for the paragraph an
    amendment touched, not read end to end for it. A changed heading
    belongs to the section piece itself (`root`); `versions` is (older,
    newer), which the two columns are headed with."""
    changes, changed = [], []
    for change in html_view._compare_pieces(older, newer):
        if change["label"] == "Heading":
            index = root
        else:
            index = next((u["tree_node"]["node"].get("_review_index") for u in (change["old"], change["new"])
                          if u is not None and "_review_index" in u["tree_node"]["node"]), None)
        if index is not None and index not in changed:
            changed.append(index)
        changes.append({"op": change["op"], "label": change["label"], "html": change["html"],
                        "old_html": change["old_html"], "new_html": change["new_html"],
                        "older": versions[0], "newer": versions[1], "node_index": index})
    return {"changes": changes, "changed_pieces": changed}


def _work_instructions() -> dict:
    """This work's amending-Act instructions (corpus/amending/load.py),
    read once per load."""
    global _instructions
    if _instructions is None:
        from corpus.amending import load
        _instructions = load.work_instructions(_act, Path("."), load.work_title(_act, Path("."), _act_title))
    return _instructions


def _target_label(ins: dict) -> str:
    where = f"Sch {ins['schedule']} item" if ins.get("schedule") else "s"
    number = ins["path"][0] if ins.get("schedule") and ins.get("path") else ins.get("section") or ""
    rest = ins["path"][1:] if ins.get("schedule") else ins.get("path") or []
    label = f"{where} {number}" + "".join(f"({p})" for p in rest)
    return label + (" heading" if ins.get("heading") else "") + (
        f", definition of {ins['definition']}" if ins.get("definition") else "")


def _amending_evidence(keys: tuple, older: list[dict], newer: list[dict], newer_version: int, root: int) -> dict:
    """What the amending Acts instructed for this section between the two
    reprints, checked against how it changed -- or {} where no Act that
    first shows in the newer reprint has been fetched, and the margin
    notes are all there is to go on."""
    from corpus.amending.match import match

    from corpus.amending.load import fetched_for

    work = _work_instructions()
    acts = fetched_for(work, newer_version)
    if not acts:
        return {}
    candidates = [i for key in dict.fromkeys(keys) for i in work["by_key"].get(key, []) if i["act"] in acts]
    result = match(candidates, older, newer)
    numbers = {u["name"]: u["tree_node"]["node"].get("number") for u in (*older, *newer) if u.get("name")}

    def piece(pair):
        own = next((u for u in pair if u is not None and "_review_index" in u["tree_node"]["node"]), None)
        shown = next((u for u in reversed(pair) if u is not None), None)
        return (own["tree_node"]["node"]["_review_index"] if own else None,
                html_view._piece_label(shown, numbers) if shown else None)

    instructions = []
    for ins in result["instructions"]:
        node_index, at = piece(ins["pair"])
        # The Act says where the piece belongs; where the parse has it
        # elsewhere, that is a reference the place tool can put it at.
        place_as = None
        if ins["status"] == "elsewhere" and node_index is not None and not ins.get("schedule") and ins.get("path"):
            path = list(ins["path"])
            if ins["action"] == "insert_provision" and ins.get("number"):
                path = path[:-1] + [ins["number"]]
            place_as = "".join(f"({p})" for p in path)
        instructions.append({"place_as": place_as, "act": ins["act"], "provision": ins["provision"], "raw": ins["raw"],
                             "target": _target_label(ins), "action": ins["action"], "status": ins["status"],
                             "at": "heading" if ins["at"] == "heading" else at,
                             # A heading belongs to the section piece itself.
                             "node_index": root if ins["at"] == "heading" else node_index})
    return {"instructions": instructions,
            "unexplained": [i for i in (piece(c["pair"])[0] for c in result["unexplained"]) if i is not None]}


def _reference_view(reference: int, their_nodes: list[dict], their_units: list[dict],
                    older: list[dict], newer: list[dict]) -> dict:
    """Where this section is printed in the version it is compared with:
    the page to open there, and the boxes to mark on it -- the pieces that
    changed, or the whole section where none did."""
    theirs = {id(u["tree_node"]["node"]) for u in their_units}
    changed_nodes = [side["tree_node"]["node"] for change in html_view._compare_pieces(older, newer)
                     for side in (change["old"], change["new"])
                     if side is not None and id(side["tree_node"]["node"]) in theirs]
    rects = [r for node in (changed_nodes or their_nodes) for r in node.get("rects") or []]
    page = rects[0]["page"] if rects else (their_nodes[0].get("page_start") if their_nodes else None)
    return {"version": reference, "page": page, "rects": rects}


def _note_raws(nodes: list[dict]) -> list[str]:
    out = []
    for node in nodes:
        for note in node.get("history") or []:
            raw = note.get("raw") if isinstance(note, dict) else note
            if raw:
                out.append(raw)
    return out


def _endnotes_of(version: int) -> tuple:
    if version not in _amending_acts:
        path = Path("data/parsed") / f"{_work_review['slugs'][version]}.json"
        endnotes = json.loads(path.read_text(encoding="utf-8")).get("endnotes") if path.exists() else None
        listed = {a["citation"] for a in (endnotes or {}).get("amending_acts") or [] if a.get("citation")}
        _amending_acts[version] = (listed, amendments.build_amendment_index(endnotes))
    return _amending_acts[version]


def _amendment_evidence(mine: tuple, theirs: tuple) -> dict:
    """The Act's own account of a changed unit: the margin notes the newer
    of the two versions prints that the older did not, each with the
    endnote for the Act it cites, and whether those notes record an
    amendment at all -- lineage.act_records_amendment, the same test the
    public histories apply. Where they don't, the difference is the
    parser's, and that is worth saying before anyone reviews it as law."""
    (older_v, older_key, older_nodes), (newer_v, newer_key, newer_nodes) = sorted([mine, theirs])
    loose = {v: lineage.loose_notes(_work_review["states"][v].get("unattached"))
             for v in (older_v, newer_v)}
    older_notes = _note_raws(older_nodes) + loose[older_v].get(older_key, [])
    newer_notes = _note_raws(newer_nodes) + loose[newer_v].get(newer_key, [])
    listed, index = _endnotes_of(newer_v)
    was, cited_before = set(older_notes), lineage._cited(older_notes)
    notes = []
    for raw in dict.fromkeys(newer_notes):
        if raw in was:
            continue
        records = amendments.resolve_note(raw, index)
        # A note that grew a citation is shown for that Act alone: its
        # older ones were explained in the version before.
        fresh = [r for r in records if r.get("citation") not in cited_before]
        notes.append({"raw": raw, "version": newer_v, "fresh": bool(fresh),
                      "acts": [{**r, "described": amendments.describe(r)} for r in fresh or records]})
    # A note only reworded or moved says nothing about this change, and
    # goes once one that does is there to read.
    if any(n["fresh"] for n in notes):
        notes = [n for n in notes if n["fresh"]]
    return {"notes": notes, "evidenced": lineage.act_records_amendment(older_notes, newer_notes, listed)}


def _unit_lineage_payload(unit_no: int, unit_nodes: list[dict], indices: list[int]) -> "dict | None":
    """What the version this one is compared with says about this unit:
    who vouches for it, or how its words differ, and -- for a provision
    the reference lacks -- which of the reference's own it might have
    been carried from."""
    entry = _unit_lineage.get(unit_no)
    if not entry or not _work_review:
        return None
    version, reference = _work_review["version"], _work_review["reference"]
    out = {"status": entry["status"], "source": entry["source"], "reference": reference,
           "key": list(entry["key"]), "label": _key_label(entry["key"])}
    if entry["status"] != lineage.TO_REVIEW or reference is None:
        return out
    theirs = _reference_provisions()
    links = {v: state.get("links") or {} for v, state in _work_review["states"].items()}
    their_key = lineage.step(links, entry["key"], version, reference)
    provision = theirs.get(their_key)
    order = _hierarchy or html_view.HIERARCHY_ORDER
    # Tagged with where each piece sits in this tool, so a difference found
    # in the comparison can be traced back to the piece it is in.
    mine = html_view._wording_units([{**n, "_review_index": i} for n, i in zip(unit_nodes, indices)], order)
    out["slugs"] = {"this": _act, "reference": _work_review["slugs"][reference]}
    if provision is None:
        out["compare"] = None
        # A section new in the newer reprint: the Act that inserted it
        # names it by its own number.
        if reference < version:
            out.update(_amending_evidence((entry["key"],), [], mine, version, indices[0]))
    else:
        end = diffing.unit_end(_reference_nodes, provision["node_index"])
        their_nodes = _reference_nodes[provision["node_index"]:end]
        other = html_view._wording_units(their_nodes, order)
        older, newer = (other, mine) if reference < version else (mine, other)
        out["compare"] = html_view._compare_html(older, newer)
        out.update(_changed_pieces(older, newer, indices[0], tuple(sorted((version, reference)))))
        out["reference_view"] = _reference_view(reference, their_nodes, older if reference < version else newer,
                                                older, newer)
        out.update(_amendment_evidence(
            (version, entry["key"], unit_nodes), (reference, their_key, their_nodes)))
        out.update(_amending_evidence((entry["key"], their_key), older, newer, max(version, reference), indices[0]))
    # Carrying from is recorded against the later of two versions, and
    # offered where the earlier one lacks this number.
    # Offered by the unit's own number, so a link already made stays
    # visible, and can be undone, once it has joined the two.
    if reference < version and entry["key"] not in theirs and entry["key"][0] == "provision":
        mine_keys = set(_work_review["states"][version]["raw"])
        out["carry_candidates"] = [
            {"key": list(k), "label": _key_label(k)}
            for k in theirs if k[0] == "provision" and k not in mine_keys
        ]
        carried = (_work_review["states"][version].get("links") or {}).get(entry["key"])
        out["carried_from"] = list(carried) if carried else None
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--restart", action="store_true", help="ignore existing progress and start from the beginning")
    ap.add_argument("--read-only", action="store_true",
                    help="serve the tool without letting anything change -- for looking at the real "
                         "corpus, or driving the interface, without writing a decision nobody made")
    args = ap.parse_args()
    if args.read_only:
        globals()["_READ_ONLY"] = True
    _load_state(args.act, restart=args.restart)

    import uvicorn

    print(f"Serving {args.act}: {len(_nodes)} nodes, {len(_units)} units."
          + (" READ-ONLY -- nothing can be changed." if _READ_ONLY else ""))
    if _unplaced_verified:
        print(
            f"Note: {len(_unplaced_verified)} reviewed piece(s) are about provisions this parse "
            "doesn't contain, so they aren't shown. They are kept as they are, not deleted, and "
            "come back if the provision does."
        )
    if _verified and not _positions_trusted:
        print(
            f"Note: {len(_verified)} reviewed piece(s) were stored against a parse this one can't be matched to, "
            "so nothing is being assumed about pieces you merged away -- every node is shown. "
            "Re-run run_pipeline.py to re-anchor them."
        )
    print(f"Corrections logged so far across all Acts: {stats()}")
    if _unattached_notes:
        print(f"{len(_unattached_notes)} amendment-history note(s) couldn't be auto-linked to a node.")
    print(f"Open http://127.0.0.1:{args.port}/ in a browser.")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
