"""
Renders a parsed Act into a browsable set of Markdown files -- one per
Section plus an Act-level index linking them in order, with defined
terms and "section N" / "Part N" / "Division N" references hyperlinked
between pages. A Bill or an Explanatory Memorandum exports the same
way; those call their top-level provisions clauses rather than
sections (see hierarchy.SECTION_LEVEL_TYPES) and cross-reference them
as "clause N". The goal is the AustLII browsing experience (open an
Act, click through to a section, follow a cross-reference or a defined
term) as plain Markdown files instead of a database-backed website.

Like akn_export.py, this is a read-only export over whichever node
list you point it at (data/ai_parsed/<act>.json, merged with whatever
review.py has verified so far in data/legislation.db) -- it doesn't
change extraction, the rule parser, or review.py.

Layout written under the given output directory:
    index.md              Part/Division/Subdivision headings, each Section
                           listed as a link, in document order
    sections/sXX.md        one file per Section (its own text plus all
                           nested subsections/paragraphs/subparagraphs/
                           notes, its amendment history, and prev/next links)

Cross-referencing works off text patterns (see definitions.py for the
same caveat on defined-term detection): "section 12"-style mentions
only get linked when the number matches a Section that actually exists
in this Act and doesn't look like it's citing a different Act ("section
5 of the Sentencing Act 1991" is deliberately left unlinked). This is a
navigation aid, not a guarantee -- an unmatched or ambiguous reference
is left as plain text rather than linked to the wrong place.

Every file opens with YAML front matter -- a title and description
meant for a future web interface (search results, browser tabs, link
previews), plus a "verified" flag summarising how much of it a human
has reviewed. review.py stamps a node with verified_at the moment a
human accepts or edits it (never just for flagging something for
follow-up -- see its commit_unit/_apply_action docstrings); a Section
page's front matter is "full" only if every one of its own Subsection,
Paragraph, Subparagraph and Note pieces carries that stamp, "partial"
if only some do, "none" if it's still fresh out of the rules engine.
index.md's front matter reports the same summary across the whole Act.
"""
import re
from pathlib import Path

import yaml

from .akn_export import _format_num, build_hierarchy_tree
from .definitions import (
    extract_section_ref_terms,
    extract_terms,
    looks_like_definitions_section,
    split_definition_clauses,
)
from .extract import reflow
from .hierarchy import HIERARCHY_ORDER, SECTION_LEVEL_TYPES, make_ranks, schedule_is_pageable

SECTIONS_DIR = "sections"


def _structural_types(hierarchy_order: list[str]) -> tuple[str, ...]:
    """The container levels above Section -- chapter, part, division,
    subdivision by default -- in hierarchy order. These get their own
    heading in index.md and appear in a Section's breadcrumb; Section
    itself is a link, and the bracket levels live on a Section's own
    page."""
    rank = make_ranks(hierarchy_order)
    return tuple(l for l in hierarchy_order if rank[l] < rank["section"])


def _section_filename(number: str | None, node_type: str = "section") -> str:
    """"s3.md" for an Act's section 3, "c3.md" for a Bill's (or an EM's)
    clause 3 -- the prefix follows what the document actually calls its
    top-level provisions, so a link or a URL reads the same way the
    citation does."""
    slug = re.sub(r"[^a-z0-9]+", "", (number or "x").lower())
    prefix = "c" if node_type == "clause" else "s"
    return f"{prefix}{slug or 'x'}.md"


def assign_filenames(sections: list[tuple[dict, list[dict]]]) -> tuple[dict[str, str], dict[str, str]]:
    """{eid: filename} for every section (used to write and link its own
    page) and {number.lower(): filename} for the *first* section with
    that number (used for "section N" prose cross-references).

    Section numbers aren't always unique across the whole document: a
    Schedule is its own container (see hierarchy.py's own note on
    "schedule"'s depth), but its internal numbered items still reuse
    the ordinary "section" node type rather than getting a schedule-
    specific one -- real Schedules number their own clauses "in the
    same way as sections" (see basic-structure.yaml) -- so a Schedule
    reproducing the full text of a historical amending Act, or just
    numbering its own items 1, 2, 3..., can introduce a "3", "4", etc.
    that collides with the Act's own. Silently overwriting one
    section's page with another's would be exactly the kind of data
    loss this pipeline is built to avoid, so a repeat number gets a
    disambiguating suffix for its own page instead -- and isn't
    registered for cross-referencing, since "section 3" in running
    prose should resolve to the genuine section, not a schedule's
    reprint."""
    filenames_by_eid: dict[str, str] = {}
    first_by_number: dict[str, str] = {}
    used: set[str] = set()

    for tree_node, _breadcrumb in sections:
        number = tree_node["node"].get("number")
        base = _section_filename(number, tree_node["node"]["type"])
        filename = base
        n = 2
        while filename in used:
            filename = base.replace(".md", f"_{n}.md")
            n += 1
        used.add(filename)
        filenames_by_eid[tree_node["eid"]] = filename
        if number and number.lower() not in first_by_number:
            first_by_number[number.lower()] = filename

    return filenames_by_eid, first_by_number


def page_title(node: dict) -> str:
    """The H1 of a top-level provision's own page. An Act's section
    keeps its bare "3 Definitions" form -- that's how a section is
    actually cited. A Bill's or an EM's provision gets its type spelled
    out ("Clause 5 Purposes"), because a bare number isn't how anything
    is cited, and an EM's entries have no headings to tell them apart
    otherwise. A Schedule page (see hierarchy.schedule_is_pageable)
    gets the same treatment as a Part or Division's own heading --
    "Schedule 3 - Persons who may witness statements..." -- since a
    bare "3 Persons who may witness..." would read as though 3 were
    this Act's own section number, which it isn't."""
    if node["type"] == "schedule":
        return _display_title(node["type"], node.get("number"), node.get("heading"))
    label = f"{node.get('number') or ''} {node.get('heading') or ''}".strip()
    if node["type"] == "clause" and node.get("number"):
        return f"Clause {label}"
    return label


_INDEX_SNIPPET_CHARS = 90


def index_label(node: dict) -> str:
    """How a top-level provision reads in an index listing. An Act's or
    a Bill's is "3 Definitions" -- number plus heading, the Table of
    Provisions form. An Explanatory Memorandum's entries have no
    headings at all (see em_parser.py), so a list of them would
    otherwise be a column of bare numbers; those fall back to "Clause
    N" plus the start of the entry's own text, which is exactly what a
    reader needs to pick one out."""
    if node.get("heading"):
        return f"{node.get('number') or ''} {node['heading']}".strip()
    # No heading: the snippet has to carry the row. It reads as a
    # sentence ("sets out the purposes of the Bill..."), so the number
    # in front of it needs its type spelled out, or the two would run
    # together -- which is exactly how the source document writes it:
    # "Clause 1 sets out...".
    label = page_title(node)
    snippet = " ".join((node.get("text") or "").split())
    if not snippet:
        return label or "(untitled)"
    if len(snippet) > _INDEX_SNIPPET_CHARS:
        snippet = snippet[:_INDEX_SNIPPET_CHARS].rsplit(" ", 1)[0] + "\u2026"
    return f"{label} {snippet}".strip()


def _heading_level(node_type: str) -> int:
    return {"chapter": 1, "part": 1, "division": 2, "subdivision": 3, "heading_group": 3}.get(node_type, 3)


# ---------------------------------------------------------------------------
# Every internal link target is a real Markdown header, not an HTML <a
# id>. Plenty of viewers (VS Code's built-in preview among them) only
# resolve a "#fragment" link against auto-generated header anchors --
# they never look at arbitrary <a id="..."> tags, even though that's
# valid HTML and GitHub's own renderer *does* honour it. So every node
# that can be a link target (every Part/Division/Subdivision in the
# index, every Subsection/Paragraph/Subparagraph in a Section page,
# each individual clause of a Definitions section that got split apart
# for readability) is rendered as its own header, and every link is
# built from that header's own computed slug -- the same lowercase,
# strip-punctuation, hyphenate, de-duplicate approach GitHub (and most
# other Markdown tools) use, so the fragment actually matches what the
# file will resolve to.
# ---------------------------------------------------------------------------

_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")


def _github_slug(text: str, counts: dict[str, int]) -> str:
    s = text.strip().lower()
    s = _SLUG_STRIP_RE.sub("", s)
    s = re.sub(r"\s+", "-", s)
    if s not in counts:
        counts[s] = 0
        return s
    counts[s] += 1
    return f"{s}-{counts[s]}"


# A list item whose source prints a bullet rather than a letter, so it
# has no number of its own to head it with. An Act letters every item
# it lists; an Explanatory Memorandum bullets them (see em_parser.py),
# and rendering those as bare paragraphs would lose the fact that
# they're a list at all.
_BULLETED_TYPES = ("paragraph", "subparagraph", "sub_subparagraph")


def _is_bulleted_item(unit: dict) -> bool:
    node = unit["tree_node"]["node"]
    return unit["header_text"] is None and not node.get("number") and node["type"] in _BULLETED_TYPES


def _clause_header_text(label: str | None, index: int, total: int) -> str | None:
    if total <= 1:
        return label
    if label:
        return f"{label} ({index + 1})"
    return f"¶{index + 1}"  # a lead-in clause with no bracket label of its own (e.g. a section's unnumbered text)


def _iter_body_units(tree_node: dict, depth: int = 0, in_definitions: bool = False, _is_page_root: bool = True):
    """Yields one dict per renderable unit of a section's subtree, in
    the exact order rendering will emit them -- the single source of
    truth both _render_body (which prints them) and
    compute_section_slugs (which works out their header anchors before
    the page is even written) walk through.

    "level" is the Markdown header level a unit would print as (capped
    at 6, since Markdown has no deeper header); "depth" is the raw
    nesting distance from the Section itself, with no cap --
    html_view.py renders indentation from it rather than headers, so it
    needs the real depth, not one flattened by that cap.

    `_is_page_root` is only for this function's own recursion -- true
    exactly once, on the outermost call, and never on a nested one.
    depth alone can't tell the two apart: a Section's own immediate
    children are deliberately recursed into *without* incrementing
    depth (see child_depth below), so a paragraph sitting right under a
    Section root also arrives at depth 0."""
    node = tree_node["node"]
    t = node["type"]
    # An ordinary Section or Clause is always page-root whenever it
    # shows up here at all (collect_sections never lets one appear
    # nested inside another's subtree); the only other page root
    # reaching this function is a Schedule whose own content has
    # nowhere else to go (see hierarchy.schedule_is_pageable) -- caught
    # by _is_page_root, since "schedule" isn't itself in
    # SECTION_LEVEL_TYPES. Either way this node's own num/heading
    # becomes the page's H1, not repeated as a body sub-heading, and
    # its children start their own nesting at this same depth rather
    # than one deeper -- exactly like a Section's children do.
    is_root = _is_page_root or t in SECTION_LEVEL_TYPES

    if is_root:
        label = None
        in_definitions = in_definitions or looks_like_definitions_section(node.get("heading"))
    else:
        label = _format_num(t, node["number"]) if node.get("number") else None

    text = reflow(node.get("text"))
    heading = node.get("heading")
    level = min(depth + 2, 6)

    if t == "definition" and heading:
        # Already split into its own node by the rules engine (see
        # rule_parser.py's _try_definition_start) -- the term itself is
        # this node's own heading, with its defining text (if any)
        # sitting right below it, unlike the other heading+text
        # combinations below, which are pure headers with no body of
        # their own. collect_definitions reads this same
        # node["heading"] directly rather than re-deriving the term
        # from `text` -- there's nothing left for extract_terms to
        # find any more, since the term is no longer at the start of
        # the text.
        yield {"tree_node": tree_node, "clause_index": 0, "text": text or None, "header_text": heading, "level": level, "depth": depth}
    elif heading and not is_root:
        header_text = f"{label} {heading}".strip() if label else heading
        yield {"tree_node": tree_node, "clause_index": 0, "text": None, "header_text": header_text, "level": level, "depth": depth}
    elif text:
        # A Definitions section's separate "term means ..." clauses
        # commonly arrive joined into one node's block of text, with
        # the rule parser unable to split them (see definitions.py's
        # module docstring) -- split on that same boundary so each
        # clause becomes its own paragraph (and, when there's more
        # than one, its own header) instead of one run-on line.
        clauses = split_definition_clauses(text) if in_definitions else [text]
        needs_header = not is_root or len(clauses) > 1
        for i, clause in enumerate(clauses):
            header_text = _clause_header_text(label, i, len(clauses)) if needs_header else None
            yield {"tree_node": tree_node, "clause_index": i, "text": clause, "header_text": header_text, "level": level, "depth": depth}
    elif label:
        yield {"tree_node": tree_node, "clause_index": 0, "text": None, "header_text": label, "level": level, "depth": depth}

    child_depth = depth if is_root else depth + 1
    for child in tree_node["children"]:
        yield from _iter_body_units(child, child_depth, in_definitions, _is_page_root=False)


def compute_section_slugs(tree_node: dict) -> dict[tuple[str, int], str | None]:
    """(node_eid, clause_index) -> the header slug that unit will
    render as on its page, or None for a unit that doesn't get its own
    header (a section's single, unlabelled block of lead-in text --
    already covered by the page's own H1, so a link to it just leaves
    out the fragment)."""
    node = tree_node["node"]
    counts: dict[str, int] = {}
    _github_slug(page_title(node), counts)  # reserve the page's own H1 slug first
    slugs: dict[tuple[str, int], str | None] = {}
    for unit in _iter_body_units(tree_node):
        key = (unit["tree_node"]["eid"], unit["clause_index"])
        slugs[key] = _github_slug(unit["header_text"], counts) if unit["header_text"] is not None else None
    return slugs


def compute_index_slugs(tree_roots: list[dict], act_title: str, structural_types: tuple[str, ...]) -> dict[str, str]:
    """node_eid -> header slug for every Chapter, Part, Division,
    Subdivision or heading_group that will appear in index.md (all one
    page, so one shared counts table, seeded with the page's own H1
    first to match the real order)."""
    header_types = (*structural_types, "heading_group")
    counts: dict[str, int] = {}
    _github_slug(act_title, counts)
    slugs: dict[str, str] = {}

    def walk(tree_node):
        node = tree_node["node"]
        t = node["type"]
        if t in header_types:
            title = _display_title(t, node.get("number"), node.get("heading"))
            slugs[tree_node["eid"]] = _github_slug(title, counts)
        for child in tree_node["children"]:
            walk(child)

    for root in tree_roots:
        walk(root)
    return slugs


def _collect_verification(tree_nodes: list[dict]) -> dict:
    """Rolls up review.py's per-node verified_at stamps (see
    commit_unit/_apply_action there) across every node in these
    subtrees -- "full" if all of them carry a stamp, "partial" if only
    some do, "none" if none do. verified_at is the most recent stamp
    found (ISO 8601, UTC, so a plain string max() gives the right
    order), or None. Called once per Section for that page's own front
    matter, and once across the whole tree for index.md's Act-wide
    summary."""
    total = 0
    timestamps: list[str] = []

    def walk(tree_node: dict) -> None:
        nonlocal total
        total += 1
        ts = tree_node["node"].get("verified_at")
        if ts:
            timestamps.append(ts)
        for child in tree_node["children"]:
            walk(child)

    for tree_node in tree_nodes:
        walk(tree_node)

    if not timestamps:
        status = "none"
    elif len(timestamps) == total:
        status = "full"
    else:
        status = "partial"
    return {
        "status": status,
        "verified_at": max(timestamps) if timestamps else None,
        "verified_count": len(timestamps),
        "total_count": total,
    }


def _front_matter(fields: dict) -> str:
    """YAML front matter, safely produced by a real YAML writer --
    titles and descriptions come from Act text, which routinely
    contains colons, quotes and other characters that would silently
    corrupt a hand-formatted "key: value" line (e.g. a heading
    containing ": " would be misread as introducing a nested
    mapping)."""
    body = yaml.safe_dump(fields, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{body}---\n\n"


def _display_title(node_type: str, number: str | None, heading: str | None) -> str:
    """"Part I - Offences", "Division 1 - Offences against the person",
    "Subdivision (1) - Homicide" -- the type name spelled out (Part,
    Division and Subdivision aren't spelled out for a citation the way
    "3 Punishment for murder" is in the source text, but spelling them
    out here is exactly what makes an index or breadcrumb readable on
    its own, AustLII-style). A section keeps its bare "3 Punishment for
    murder" form -- that already matches how sections are actually
    cited, so it gets no type-name prefix."""
    heading = heading or ""
    if node_type in SECTION_LEVEL_TYPES or not number:
        return f"{number or ''} {heading}".strip()
    return f"{node_type.capitalize()} {_format_num(node_type, number)} - {heading}".strip(" -")


# ---------------------------------------------------------------------------
# Tree walks: collect sections (for the index + prev/next chain) and
# defined terms (for cross-linking), each keyed by the eId scheme from
# akn_export so both exports stay consistent with each other.
# ---------------------------------------------------------------------------

def collect_sections(tree_roots: list[dict], structural_types: tuple[str, ...]) -> list[tuple[dict, list[dict]]]:
    """[(section_tree_node, breadcrumb_of_ancestor_tree_nodes), ...] in
    document order -- every top-level provision, whether this document
    calls them sections or clauses (see hierarchy.SECTION_LEVEL_TYPES),
    or a Schedule whose own content is unnumbered prose with no
    Section-level child of its own (see hierarchy.schedule_is_pageable).
    Doesn't descend into any of these nodes' own children -- those
    belong to that page, not the index."""
    sections = []

    def walk(tree_node, breadcrumb):
        node = tree_node["node"]
        if node["type"] in SECTION_LEVEL_TYPES or schedule_is_pageable(tree_node):
            sections.append((tree_node, breadcrumb))
            return
        next_breadcrumb = breadcrumb + [tree_node] if node["type"] in structural_types else breadcrumb
        for child in tree_node["children"]:
            walk(child, next_breadcrumb)

    for root in tree_roots:
        walk(root, [])
    return sections


def collect_definitions(
    sections: list[tuple[dict, list[dict]]],
    filenames_by_eid: dict[str, str],
    section_files: dict[str, str],
) -> dict[str, dict]:
    """term (lowercase) -> {"fragment", "file", "display"}, under two
    conditions (see definitions.py): the term is introduced inside a
    section whose heading suggests it defines terms, or a clause
    anywhere points a term at a specific section ("term has the same
    meaning as in section N") -- the second always wins where both
    apply, since following the pointer to where the term is actually
    explained beats linking to wherever the pointer happens to sit.
    "fragment" is the same header slug computed by
    compute_section_slugs/_render_body, so a term's link lands on the
    exact clause header that page will actually render."""
    definitions: dict[str, dict] = {}

    def walk_definitions_section(tree_node, filename):
        slugs = compute_section_slugs(tree_node)
        for unit in _iter_body_units(tree_node):
            key = (unit["tree_node"]["eid"], unit["clause_index"])
            node = unit["tree_node"]["node"]
            if node.get("type") == "definition" and node.get("heading"):
                # Already split into its own node by the rules engine
                # (see rule_parser.py's _try_definition_start) -- its
                # term is this node's own heading, used directly
                # rather than re-derived from `text` via extract_terms,
                # which can't find it any more (a split definition's
                # text starts straight at "means ..."/"includes ...",
                # with the term no longer at its start).
                term = node["heading"].strip().lower()
                if term:
                    definitions.setdefault(term, {"fragment": slugs.get(key), "file": filename, "display": term})
                continue
            for term in extract_terms(unit["text"] or ""):
                definitions.setdefault(term, {"fragment": slugs.get(key), "file": filename, "display": term})

    def walk_section_refs(tree_node):
        for terms, section_num in extract_section_ref_terms(tree_node["node"].get("text") or ""):
            target_file = section_files.get(section_num.lower())
            if not target_file:
                continue  # referenced section doesn't exist in this Act -- leave unlinked, not linked wrong
            for term in terms:
                definitions[term] = {"fragment": None, "file": target_file, "display": term}
        for child in tree_node["children"]:
            walk_section_refs(child)

    for section_node, _ in sections:
        if looks_like_definitions_section(section_node["node"].get("heading")):
            walk_definitions_section(section_node, filenames_by_eid[section_node["eid"]])
    for section_node, _ in sections:
        walk_section_refs(section_node)
    return definitions


# ---------------------------------------------------------------------------
# Cross-reference linkification -- one regex pass so a definition term
# and a "section N" mention can never corrupt each other's replacement.
# ---------------------------------------------------------------------------

# Don't link a mention that's actually citing a *different* Act --
# "section 5(2G) of that Act" and "sections 5A and 5B of the Sentencing
# Act 1991" both need to skip past an optional pinpoint cite ("(2G)")
# or a second number in a range ("and 5B") before the "of the/that ...
# Act" qualifier becomes visible; a plain lookahead right after the
# first number would miss both. Still a heuristic -- an external
# reference with no "of ... Act" wording at all (rare) would still get
# linked.
_SECTION_REF_RE = (
    r"\bsections?\s+\d+[A-Za-z]*\b"
    r"(?!(?:\s*\([^)]*\))?(?:\s+and\s+\d+[A-Za-z]*)?\s+of\s+(?:the|that|any)\b)"
)
# The same, for a document whose own provisions are clauses. Kept
# separate and chosen per document (see section_ref_pattern) rather
# than always matching both words: an Act that says "clause 3" means a
# clause of a Schedule or of an agreement it reproduces, not its own
# section 3, so linking that to section 3 would be actively wrong.
_CLAUSE_REF_RE = (
    r"\bclauses?\s+\d+[A-Za-z]*\b"
    r"(?!(?:\s*\([^)]*\))?(?:\s+and\s+\d+[A-Za-z]*)?\s+of\s+(?:the|that|any)\b)"
)


def section_ref_pattern(sections: list[tuple[dict, list[dict]]]) -> str:
    """Which word this document's own cross-references use, decided by
    what its top-level provisions actually are, rather than a flag the
    caller has to remember to set."""
    if any(tn["node"]["type"] == "clause" for tn, _b in sections):
        return _CLAUSE_REF_RE
    return _SECTION_REF_RE
_PART_REF_RE = r"\bPart\s+(?:[IVXLCDM]+|\d+[A-Za-z]*)\b"
_DIVISION_REF_RE = r"\bDivision\s+\d+[A-Za-z]*\b"


def _build_linkifier(section_files: dict[str, str], part_eids: dict[str, str], division_eids: dict[str, str], definitions: dict[str, dict], secref_re: str = _SECTION_REF_RE):
    parts = []
    if definitions:
        term_alt = "|".join(re.escape(t) for t in sorted(definitions, key=lambda t: (-len(t), t)))
        parts.append(f"(?P<def>\\b(?:{term_alt})\\b)")
    parts.append(f"(?P<secref>{secref_re})")
    parts.append(f"(?P<partref>{_PART_REF_RE})")
    parts.append(f"(?P<divref>{_DIVISION_REF_RE})")
    master = re.compile("|".join(parts), re.IGNORECASE)

    def replace(m: re.Match, current_file: str, current_fragment: str | None, index_href: str) -> str:
        text = m.group(0)
        if m.lastgroup == "def":
            info = definitions.get(text.lower())
            if not info:
                return text
            if info["file"] == current_file and info.get("fragment") == current_fragment:
                return text  # already sitting under this exact heading -- don't link a term back to itself
            target = f"{info['file']}#{info['fragment']}" if info["fragment"] else info["file"]
            return f"[{text}]({target})"
        if m.lastgroup == "secref":
            num = re.search(r"\d+[A-Za-z]*", text).group(0)
            filename = section_files.get(num.lower())
            return f"[{text}]({filename})" if filename else text
        if m.lastgroup == "partref":
            num = text.split(None, 1)[1]
            fragment = part_eids.get(num.lower())
            return f"[{text}]({index_href}#{fragment})" if fragment else text
        if m.lastgroup == "divref":
            num = text.split(None, 1)[1]
            fragment = division_eids.get(num.lower())
            return f"[{text}]({index_href}#{fragment})" if fragment else text
        return text

    def linkify(text: str, current_file: str, current_fragment: str | None = None, index_href: str = "../index.md") -> str:
        return master.sub(lambda m: replace(m, current_file, current_fragment, index_href), text)

    return linkify


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_history(tree_node: dict, out: list[str]) -> None:
    node = tree_node["node"]
    for h in node.get("history") or []:
        label = _format_num(node["type"], node["number"]) if node.get("number") else ""
        prefix = f"{label} " if label else ""
        out.append(f"- {prefix}{h['raw']}")
    for child in tree_node["children"]:
        _render_history(child, out)


def _render_body(
    tree_node: dict,
    linkify,
    current_file: str,
    slugs: dict[tuple[str, int], str | None],
    out: list[str],
    in_definitions: bool = False,
) -> None:
    for unit in _iter_body_units(tree_node, 0, in_definitions):
        if unit["header_text"] is not None:
            out.append(f"{'#' * unit['level']} {unit['header_text']}")
            out.append("")
        if unit["text"] is not None:
            key = (unit["tree_node"]["eid"], unit["clause_index"])
            body = linkify(unit["text"], current_file, slugs.get(key))
            if _is_bulleted_item(unit):
                # A Markdown list item, indented by its nesting depth,
                # so a sub-list nests instead of flattening out. The
                # blank line after each one keeps the list "loose" --
                # it still reads as one list.
                out.append(f"{'  ' * unit['depth']}- {body}")
            else:
                out.append(body)
            out.append("")


def render_section_page(
    tree_node: dict,
    breadcrumb: list[dict],
    linkify,
    current_file: str,
    prev_link: str | None,
    next_link: str | None,
    act_title: str,
) -> str:
    node = tree_node["node"]
    title = page_title(node)
    citation = f"{act_title} s {node['number']}".strip() if node.get("number") else act_title
    description = f"{citation}: {node['heading']}" if node.get("heading") else citation
    verification = _collect_verification([tree_node])
    front_matter = _front_matter(
        {
            "title": title,
            "description": description,
            "verified": verification["status"],
            "verified_at": verification["verified_at"],
            "verified_count": verification["verified_count"],
            "total_count": verification["total_count"],
        }
    )

    out = []
    if breadcrumb:
        crumb = " > ".join(_display_title(b["node"]["type"], b["node"].get("number"), b["node"].get("heading")) for b in breadcrumb)
        out.append(f"[Act index](../index.md) > {crumb}")
        out.append("")
    out.append(f"# {title}")
    out.append("")
    slugs = compute_section_slugs(tree_node)
    _render_body(tree_node, linkify, current_file, slugs, out)

    history: list[str] = []
    _render_history(tree_node, history)
    if history:
        out.append("## History")
        out.append("")
        out.extend(history)
        out.append("")

    nav = []
    if prev_link:
        nav.append(f"[« Previous]({prev_link})")
    nav.append("[Act index](../index.md)")
    if next_link:
        nav.append(f"[Next »]({next_link})")
    out.append(" | ".join(nav))
    return front_matter + "\n".join(out) + "\n"


def render_index(tree_roots: list[dict], act_title: str, filenames_by_eid: dict[str, str], structural_types: tuple[str, ...]) -> str:
    verification = _collect_verification(tree_roots)
    front_matter = _front_matter(
        {
            "title": act_title,
            "description": f"Index of sections in the {act_title}.",
            "verified": verification["status"],
            "verified_at": verification["verified_at"],
            "verified_count": verification["verified_count"],
            "total_count": verification["total_count"],
        }
    )

    out = [f"# {act_title}", ""]

    def walk(tree_node, out):
        node = tree_node["node"]
        t = node["type"]
        pageable_schedule = t == "schedule" and schedule_is_pageable(tree_node)
        if t in SECTION_LEVEL_TYPES or pageable_schedule:
            filename = f"{SECTIONS_DIR}/{filenames_by_eid[tree_node['eid']]}"
            # See html_view.render_index's own copy of this logic for
            # why a pageable Schedule gets its type spelled out rather
            # than index_label's bare "3 Persons who may witness...".
            label = _display_title(t, node.get("number"), node.get("heading")) if pageable_schedule else index_label(node)
            out.append(f"- [{label}]({filename})")
            return
        if t in (*structural_types, "heading_group"):
            level = _heading_level(t)
            title = _display_title(t, node.get("number"), node.get("heading"))
            out.append(f"{'#' * level} {title}")
            out.append("")
        for child in tree_node["children"]:
            walk(child, out)

    for root in tree_roots:
        walk(root, out)
    return front_matter + "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def export_to_markdown(parsed: dict, out_dir: str, act_title: str | None = None) -> dict:
    """Writes index.md and sections/*.md under out_dir. Returns a small
    stats dict (section count, definitions found) for the caller to
    report."""
    nodes = parsed["nodes"]
    hierarchy_order = parsed.get("hierarchy") or HIERARCHY_ORDER
    structural_types = _structural_types(hierarchy_order)
    tree_roots, _collisions = build_hierarchy_tree(nodes, hierarchy_order)
    sections = collect_sections(tree_roots, structural_types)
    filenames_by_eid, section_files = assign_filenames(sections)
    definitions = collect_definitions(sections, filenames_by_eid, section_files)

    title = act_title or parsed.get("act", "Act")
    index_slugs = compute_index_slugs(tree_roots, title, structural_types)
    # Part/Division eId lookups for prose "Part N" / "Division N"
    # links. One walk over the tree in document order; on a duplicate
    # number (a Schedule reprinting a Part) the later occurrence wins,
    # same as the dict comprehension this replaces.
    part_eids: dict[str, str] = {}
    division_eids: dict[str, str] = {}
    for root in tree_roots:
        for tree_node in _iter_tree(root):
            number = tree_node["node"].get("number")
            if not number:
                continue
            if tree_node["node"]["type"] == "part":
                part_eids[number.lower()] = index_slugs[tree_node["eid"]]
            elif tree_node["node"]["type"] == "division":
                division_eids[number.lower()] = index_slugs[tree_node["eid"]]
    linkify = _build_linkifier(section_files, part_eids, division_eids, definitions, section_ref_pattern(sections))

    out_path = Path(out_dir)
    (out_path / SECTIONS_DIR).mkdir(parents=True, exist_ok=True)

    for i, (section_node, breadcrumb) in enumerate(sections):
        prev_link = filenames_by_eid[sections[i - 1][0]["eid"]] if i > 0 else None
        next_link = filenames_by_eid[sections[i + 1][0]["eid"]] if i + 1 < len(sections) else None
        current_file = filenames_by_eid[section_node["eid"]]
        page = render_section_page(section_node, breadcrumb, linkify, current_file, prev_link, next_link, title)
        (out_path / SECTIONS_DIR / current_file).write_text(page, encoding="utf-8")

    (out_path / "index.md").write_text(render_index(tree_roots, title, filenames_by_eid, structural_types), encoding="utf-8")

    return {"sections": len(sections), "definitions": len(definitions)}


def _iter_tree(tree_node: dict):
    """Every tree node in the subtree, parent before children -- i.e.
    document order."""
    yield tree_node
    for child in tree_node["children"]:
        yield from _iter_tree(child)
