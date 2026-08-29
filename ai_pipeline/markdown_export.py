"""
Renders a parsed Act into a browsable set of Markdown files -- one per
Section plus an Act-level index linking them in order, with defined terms
and "section N" / "Part N" / "Division N" references hyperlinked between
pages. The goal is the AustLII browsing experience (open an Act, click
through to a section, follow a cross-reference or a defined term) as plain
Markdown files instead of a database-backed website.

Like akn_export.py, this is a read-only export over whichever node list you
point it at (data/verified/<act>.json preferred, data/ai_parsed/<act>.json
as a fallback) -- it doesn't change extraction, the rule parser, or
review.py.

Layout written under the given output directory:
    index.md              Part/Division/Subdivision headings, each Section
                           listed as a link, in document order
    sections/sXX.md        one file per Section (its own text plus all
                           nested subsections/paragraphs/subparagraphs/
                           notes, its amendment history, and prev/next links)

Cross-referencing is text-pattern-based (see definitions.py for the same
caveat on defined-term detection): "section 12"-style mentions only get
linked when the number matches a Section that actually exists in this Act
and doesn't look like it's citing a different Act ("section 5 of the
Sentencing Act 1991" is deliberately left unlinked). This is a navigation
aid, not a guarantee -- an unmatched or ambiguous reference is left as
plain text rather than linked to the wrong place.
"""
import re
from pathlib import Path

from .akn_export import HIERARCHY_ORDER, _format_num, build_hierarchy_tree
from .definitions import (
    extract_section_ref_terms,
    extract_terms,
    looks_like_definitions_section,
    split_definition_clauses,
)

SECTIONS_DIR = "sections"
NON_LEAF_TYPES = set(HIERARCHY_ORDER) | {"heading_group"}


def _section_filename(number: str | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", (number or "x").lower())
    return f"s{slug or 'x'}.md"


def assign_filenames(sections: list[tuple[dict, list[dict]]]) -> tuple[dict[str, str], dict[str, str]]:
    """{eid: filename} for every section (used to write and link its own
    page) and {number.lower(): filename} for the *first* section with that
    number (used for "section N" prose cross-references).

    Section numbers aren't always unique document-wide: the rule parser
    doesn't yet model Schedules as their own container (see akn_export.py's
    docstring for the same caveat), so a Schedule reproducing the full text
    of a historical amending Act can introduce its own "3", "4", etc.
    Silently overwriting one section's page with another's would be exactly
    the kind of data loss this pipeline is built to avoid, so a repeat
    number gets a disambiguating suffix for its own page instead -- and
    isn't registered for cross-referencing, since "section 3" in running
    prose should resolve to the genuine section, not a schedule's reprint."""
    filenames_by_eid: dict[str, str] = {}
    first_by_number: dict[str, str] = {}
    used: set[str] = set()

    for tree_node, _breadcrumb in sections:
        number = tree_node["node"].get("number")
        base = _section_filename(number)
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


def _heading_level(node_type: str) -> int:
    return {"part": 1, "division": 2, "subdivision": 3, "heading_group": 3}.get(node_type, 3)


def _clause_anchor(base_eid: str, index: int, total: int) -> str:
    """A node holding more than one concatenated definition clause gets a
    per-clause sub-anchor (base_eid__def2, __def3, ...) so a term links to
    its own clause rather than the top of a node that might hold several;
    a node with just one clause keeps using its own eid, unchanged."""
    return base_eid if total <= 1 else f"{base_eid}__def{index + 1}"


def _display_title(node_type: str, number: str | None, heading: str | None) -> str:
    """"Part I - Offences", "Division 1 - Offences against the person",
    "Subdivision (1) - Homicide" -- the type name spelled out (Part/
    Division/Subdivision aren't in the source text for a citation like "3
    Punishment for murder" is, but spelling them out is exactly what makes
    an index or breadcrumb readable on its own, AustLII-style). A section
    keeps its bare "3 Punishment for murder" form -- that already matches
    how sections are actually cited, so no type-name prefix there."""
    heading = heading or ""
    if node_type == "section" or not number:
        return f"{number or ''} {heading}".strip()
    return f"{node_type.capitalize()} {_format_num(node_type, number)} - {heading}".strip(" -")


# ---------------------------------------------------------------------------
# Tree walks: collect sections (for the index + prev/next chain) and defined
# terms (for cross-linking), each keyed by the eId scheme from akn_export so
# both exports stay consistent with each other.
# ---------------------------------------------------------------------------

def collect_sections(tree_roots: list[dict]) -> list[tuple[dict, list[dict]]]:
    """[(section_tree_node, breadcrumb_of_ancestor_tree_nodes), ...] in
    document order. Doesn't descend into a section's own children -- those
    belong to that section's own page, not the index."""
    sections = []

    def walk(tree_node, breadcrumb):
        node = tree_node["node"]
        if node["type"] == "section":
            sections.append((tree_node, breadcrumb))
            return
        next_breadcrumb = breadcrumb + [tree_node] if node["type"] in ("part", "division", "subdivision") else breadcrumb
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
    """term (lowercase) -> {"eid", "file", "display"}, under two conditions
    (see definitions.py): the term is introduced inside a section whose
    heading suggests it defines terms, or a clause anywhere points a term at
    a specific section ("term has the same meaning as in section N") -- the
    latter always wins on overlap, since following the pointer to where the
    term is actually explained beats linking to wherever the pointer sits."""
    definitions: dict[str, dict] = {}

    def walk_definitions_section(tree_node, filename):
        node = tree_node["node"]
        clauses = split_definition_clauses(node.get("text") or "")
        for i, clause in enumerate(clauses):
            anchor = _clause_anchor(tree_node["eid"], i, len(clauses))
            for term in extract_terms(clause):
                definitions.setdefault(term, {"eid": anchor, "file": filename, "display": term})
        for child in tree_node["children"]:
            walk_definitions_section(child, filename)

    def walk_section_refs(tree_node):
        for terms, section_num in extract_section_ref_terms(tree_node["node"].get("text") or ""):
            target_file = section_files.get(section_num.lower())
            if not target_file:
                continue  # referenced section doesn't exist in this Act -- leave unlinked, not linked wrong
            for term in terms:
                definitions[term] = {"eid": None, "file": target_file, "display": term}
        for child in tree_node["children"]:
            walk_section_refs(child)

    for section_node, _ in sections:
        if looks_like_definitions_section(section_node["node"].get("heading")):
            walk_definitions_section(section_node, filenames_by_eid[section_node["eid"]])
    for section_node, _ in sections:
        walk_section_refs(section_node)
    return definitions


# ---------------------------------------------------------------------------
# Cross-reference linkification -- one regex pass so a definition term and a
# "section N" mention can never corrupt each other's replacement.
# ---------------------------------------------------------------------------

# Don't link a mention that's actually citing a *different* Act -- "section
# 5(2G) of that Act" and "sections 5A and 5B of the Sentencing Act 1991"
# both need to skip past an optional pinpoint cite ("(2G)") or a second
# number in a range ("and 5B") before the "of the/that ... Act" qualifier
# becomes visible; a plain lookahead right after the first number misses
# both. Still a heuristic -- an external reference with no "of ... Act"
# wording at all (rare) would still get linked.
_SECTION_REF_RE = (
    r"\bsections?\s+\d+[A-Za-z]*\b"
    r"(?!(?:\s*\([^)]*\))?(?:\s+and\s+\d+[A-Za-z]*)?\s+of\s+(?:the|that|any)\b)"
)
_PART_REF_RE = r"\bPart\s+(?:[IVXLCDM]+|\d+[A-Za-z]*)\b"
_DIVISION_REF_RE = r"\bDivision\s+\d+[A-Za-z]*\b"


def _build_linkifier(section_files: dict[str, str], part_eids: dict[str, str], division_eids: dict[str, str], definitions: dict[str, dict]):
    parts = []
    if definitions:
        term_alt = "|".join(re.escape(t) for t in sorted(definitions, key=lambda t: (-len(t), t)))
        parts.append(f"(?P<def>\\b(?:{term_alt})\\b)")
    parts.append(f"(?P<secref>{_SECTION_REF_RE})")
    parts.append(f"(?P<partref>{_PART_REF_RE})")
    parts.append(f"(?P<divref>{_DIVISION_REF_RE})")
    master = re.compile("|".join(parts), re.IGNORECASE)

    def replace(m: re.Match, current_eid: str, index_href: str) -> str:
        text = m.group(0)
        if m.lastgroup == "def":
            info = definitions.get(text.lower())
            if not info or info["eid"] == current_eid:
                return text
            target = f"{info['file']}#{info['eid']}" if info["eid"] else info["file"]
            return f"[{text}]({target})"
        if m.lastgroup == "secref":
            num = re.search(r"\d+[A-Za-z]*", text).group(0)
            filename = section_files.get(num.lower())
            return f"[{text}]({filename})" if filename else text
        if m.lastgroup == "partref":
            num = text.split(None, 1)[1]
            eid = part_eids.get(num.lower())
            return f"[{text}]({index_href}#{eid})" if eid else text
        if m.lastgroup == "divref":
            num = text.split(None, 1)[1]
            eid = division_eids.get(num.lower())
            return f"[{text}]({index_href}#{eid})" if eid else text
        return text

    def linkify(text: str, current_eid: str, index_href: str = "../index.md") -> str:
        return master.sub(lambda m: replace(m, current_eid, index_href), text)

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


def _render_body(tree_node: dict, linkify, current_section_eid: str, out: list[str], depth: int = 0, in_definitions: bool = False) -> None:
    node = tree_node["node"]
    t = node["type"]
    indent = "  " * depth

    if t == "section":
        label = None  # the section's own num/heading are the page's H1, not repeated in the body
        in_definitions = in_definitions or looks_like_definitions_section(node.get("heading"))
    else:
        label = _format_num(t, node["number"]) if node.get("number") else None

    text = (node.get("text") or "").strip()
    heading = node.get("heading")

    if heading and t != "section":
        out.append(f'<a id="{tree_node["eid"]}"></a>')
        out.append(f"{indent}**{label} {heading}**" if label else f"{indent}**{heading}**")
        out.append("")
    elif text:
        # A Definitions section's separate "term means ..." clauses commonly
        # arrive concatenated into one node's text blob with the rule parser
        # unable to split them (see definitions.py's module docstring) --
        # split on that same boundary so each clause becomes its own
        # paragraph (rather than one run-on line) with its own anchor
        # (matching collect_definitions's _clause_anchor exactly), so a
        # term links to its own clause rather than the top of a node that
        # might hold several. A node with just one clause (the common case
        # outside Definitions sections) renders exactly as before.
        clauses = split_definition_clauses(text) if in_definitions else [text.replace("\n", " ")]
        for i, clause in enumerate(clauses):
            anchor = _clause_anchor(tree_node["eid"], i, len(clauses))
            # A section's own eid is already anchored once by its page H1
            # (render_section_page); only emit here too when this clause's
            # anchor is actually distinct from that (i.e. this section node
            # holds more than one clause itself) -- otherwise skip to avoid
            # a pointless duplicate id, since a non-section node's own eid
            # never gets anchored anywhere else.
            if t != "section" or anchor != tree_node["eid"]:
                out.append(f'<a id="{anchor}"></a>')
            rendered = linkify(clause, current_section_eid)
            prefix = f"{indent}{label} " if (label and i == 0) else indent
            out.append(f"{prefix}{rendered}")
            out.append("")
    elif label:
        if t != "section":
            out.append(f'<a id="{tree_node["eid"]}"></a>')
        out.append(f"{indent}{label}")
        out.append("")

    for child in tree_node["children"]:
        _render_body(child, linkify, current_section_eid, out, depth + 1 if t != "section" else depth, in_definitions)


def render_section_page(tree_node: dict, breadcrumb: list[dict], linkify, prev_link: str | None, next_link: str | None) -> str:
    node = tree_node["node"]
    out = []
    if breadcrumb:
        crumb = " > ".join(_display_title(b["node"]["type"], b["node"].get("number"), b["node"].get("heading")) for b in breadcrumb)
        out.append(f"[Act index](../index.md) > {crumb}")
        out.append("")
    out.append(f'<a id="{tree_node["eid"]}"></a>')
    out.append(f"# {node['number']} {node.get('heading') or ''}".strip())
    out.append("")
    _render_body(tree_node, linkify, tree_node["eid"], out)

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
    return "\n".join(out) + "\n"


def render_index(tree_roots: list[dict], act_title: str, filenames_by_eid: dict[str, str]) -> str:
    out = [f"# {act_title}", ""]

    def walk(tree_node, out):
        node = tree_node["node"]
        t = node["type"]
        if t == "section":
            filename = f"{SECTIONS_DIR}/{filenames_by_eid[tree_node['eid']]}"
            out.append(f"- [{node['number']} {node.get('heading') or ''}]({filename})".rstrip())
            return
        if t in ("part", "division", "subdivision", "heading_group"):
            level = _heading_level(t)
            title = _display_title(t, node.get("number"), node.get("heading"))
            out.append(f'<a id="{tree_node["eid"]}"></a>')
            out.append(f"{'#' * level} {title}")
            out.append("")
        for child in tree_node["children"]:
            walk(child, out)

    for root in tree_roots:
        walk(root, out)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def export_to_markdown(parsed: dict, out_dir: str, act_title: str | None = None) -> dict:
    """Writes index.md + sections/*.md under out_dir. Returns a small stats
    dict (section count, definitions found) for the caller to report."""
    nodes = parsed["nodes"]
    tree_roots, _collisions = build_hierarchy_tree(nodes)
    sections = collect_sections(tree_roots)
    filenames_by_eid, section_files = assign_filenames(sections)
    definitions = collect_definitions(sections, filenames_by_eid, section_files)

    part_eids = {b["node"]["number"].lower(): b["eid"] for root in tree_roots for b in _all_of_type(root, "part") if b["node"].get("number")}
    division_eids = {b["node"]["number"].lower(): b["eid"] for root in tree_roots for b in _all_of_type(root, "division") if b["node"].get("number")}
    linkify = _build_linkifier(section_files, part_eids, division_eids, definitions)

    out_path = Path(out_dir)
    (out_path / SECTIONS_DIR).mkdir(parents=True, exist_ok=True)

    for i, (section_node, breadcrumb) in enumerate(sections):
        prev_link = filenames_by_eid[sections[i - 1][0]["eid"]] if i > 0 else None
        next_link = filenames_by_eid[sections[i + 1][0]["eid"]] if i + 1 < len(sections) else None
        page = render_section_page(section_node, breadcrumb, linkify, prev_link, next_link)
        (out_path / SECTIONS_DIR / filenames_by_eid[section_node["eid"]]).write_text(page, encoding="utf-8")

    title = act_title or parsed.get("act", "Act")
    (out_path / "index.md").write_text(render_index(tree_roots, title, filenames_by_eid), encoding="utf-8")

    return {"sections": len(sections), "definitions": len(definitions)}


def _all_of_type(tree_node: dict, type_name: str):
    if tree_node["node"]["type"] == type_name:
        yield tree_node
    for child in tree_node["children"]:
        yield from _all_of_type(child, type_name)
