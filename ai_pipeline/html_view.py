"""
Renders a parsed Act as a live, read-only, AustLII-style HTML browsing
view -- an index page (Part/Division/Subdivision headings, each Section
listed as a link, in document order) plus one page per Section, with
defined terms and Part/Division/"section N" cross-references hyperlinked
between them, the same way a real AustLII page reads.

This reuses markdown_export.py's document model wholesale -- the tree
walk, filename/slug assignment, and definition/cross-reference collection
are all the exact same functions that module already uses to write
Markdown files (see the imports below). The only thing that differs here
is the output format (HTML strings with real <a href> links, rendered
straight into an HTTP response) and the data source: dashboard.py calls
this against review.py's build_current_nodes (verified where a unit's
been committed, the original parse otherwise -- see its own docstring),
not a static file, and does so fresh on every request. The point is
letting a reviewer immediately see how their in-progress edits will read
to an actual user, without running export_markdown.py as a separate step
first.

Kept intentionally independent of review.py's own live server process:
rendering a page here needs no interactive state (no edit/split/merge),
just whatever build_current_nodes reads off disk, so a Section page can
be rendered without that Act's review.py child process even running.

Known gap shared with markdown_export.py: cross-reference and defined-term
matching is a text-pattern heuristic (see definitions.py), not a
guarantee -- an unmatched or ambiguous mention is left as plain text
rather than linked to the wrong place.
"""
import html
import re

from .akn_export import _format_num, build_hierarchy_tree
from .hierarchy import HIERARCHY_ORDER
from .markdown_export import (
    _DIVISION_REF_RE,
    _PART_REF_RE,
    _SECTION_REF_RE,
    _collect_verification,
    _display_title,
    _heading_level,
    _iter_body_units,
    _iter_tree,
    _structural_types,
    assign_filenames,
    collect_definitions,
    collect_sections,
    compute_index_slugs,
    compute_section_slugs,
)


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def _strip_md(filename: str) -> str:
    return filename[:-3] if filename.endswith(".md") else filename


def _build_context(parsed: dict, act_title: str) -> dict:
    nodes = parsed["nodes"]
    hierarchy_order = parsed.get("hierarchy") or HIERARCHY_ORDER
    structural_types = _structural_types(hierarchy_order)
    tree_roots, _collisions = build_hierarchy_tree(nodes, hierarchy_order)
    sections = collect_sections(tree_roots, structural_types)
    filenames_by_eid, section_files = assign_filenames(sections)
    definitions = collect_definitions(sections, filenames_by_eid, section_files)
    index_slugs = compute_index_slugs(tree_roots, act_title, structural_types)

    # Part/Division eId lookups for prose "Part N" / "Division N" links --
    # same one-pass walk export_to_markdown does for the same reason.
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

    return {
        "tree_roots": tree_roots,
        "structural_types": structural_types,
        "sections": sections,
        "filenames_by_eid": filenames_by_eid,
        "section_files": section_files,
        "definitions": definitions,
        "index_slugs": index_slugs,
        "part_eids": part_eids,
        "division_eids": division_eids,
    }


def _build_linkifier_html(section_files: dict[str, str], part_eids: dict[str, str], division_eids: dict[str, str], definitions: dict[str, dict], base_url: str):
    """Same regex/priority scheme as markdown_export._build_linkifier, but
    emits <a href> tags instead of Markdown link syntax. Must only ever be
    called on text that's *already* been HTML-escaped (see _esc) -- the
    patterns below match plain words/digits, never anything an escape pass
    would have altered, so escaping first and linkifying second is safe:
    the substituted spans are exact, already-escaped slices of the input,
    never re-derived from unescaped source."""
    parts = []
    if definitions:
        term_alt = "|".join(re.escape(t) for t in sorted(definitions, key=lambda t: (-len(t), t)))
        parts.append(f"(?P<def>\\b(?:{term_alt})\\b)")
    parts.append(f"(?P<secref>{_SECTION_REF_RE})")
    parts.append(f"(?P<partref>{_PART_REF_RE})")
    parts.append(f"(?P<divref>{_DIVISION_REF_RE})")
    master = re.compile("|".join(parts), re.IGNORECASE)

    def section_href(filename: str) -> str:
        return f"{base_url}/section/{_strip_md(filename)}"

    def replace(m: re.Match, current_file: str, current_fragment: str | None) -> str:
        text = m.group(0)
        if m.lastgroup == "def":
            info = definitions.get(text.lower())
            if not info:
                return text
            if info["file"] == current_file and info.get("fragment") == current_fragment:
                return text  # already sitting under this exact heading -- don't link a term to itself
            href = section_href(info["file"])
            if info.get("fragment"):
                href = f"{href}#{info['fragment']}"
            return f'<a href="{href}">{text}</a>'
        if m.lastgroup == "secref":
            num = re.search(r"\d+[A-Za-z]*", text).group(0)
            filename = section_files.get(num.lower())
            return f'<a href="{section_href(filename)}">{text}</a>' if filename else text
        if m.lastgroup == "partref":
            num = text.split(None, 1)[1]
            fragment = part_eids.get(num.lower())
            return f'<a href="{base_url}/#{fragment}">{text}</a>' if fragment else text
        if m.lastgroup == "divref":
            num = text.split(None, 1)[1]
            fragment = division_eids.get(num.lower())
            return f'<a href="{base_url}/#{fragment}">{text}</a>' if fragment else text
        return text

    def linkify(escaped_text: str, current_file: str, current_fragment: str | None = None) -> str:
        return master.sub(lambda m: replace(m, current_file, current_fragment), escaped_text)

    return linkify


def _verification_badge(verification: dict) -> str:
    status = verification["status"]
    label = {
        "full": "Fully reviewed",
        "partial": f"Partially reviewed ({verification['verified_count']}/{verification['total_count']})",
        "none": "Not yet reviewed",
    }[status]
    return f'<div class="verify-badge verify-{status}">{_esc(label)}</div>'


def _render_history_html(tree_node: dict, out: list[str]) -> None:
    node = tree_node["node"]
    for h in node.get("history") or []:
        label = _format_num(node["type"], node["number"]) if node.get("number") else ""
        prefix = f"{_esc(label)} " if label else ""
        out.append(f"<li>{prefix}{_esc(h['raw'])}</li>")
    for child in tree_node["children"]:
        _render_history_html(child, out)


def render_index(parsed: dict, act_title: str, base_url: str) -> str:
    """base_url is this Act's own root, e.g. "/browse/crimes-act" (no
    trailing slash) -- every link rendered here and in render_section is
    built from it, so the caller controls the URL scheme entirely."""
    ctx = _build_context(parsed, act_title)
    tree_roots = ctx["tree_roots"]
    structural_types = ctx["structural_types"]
    filenames_by_eid = ctx["filenames_by_eid"]
    index_slugs = ctx["index_slugs"]
    verification = _collect_verification(tree_roots)

    out = [f"<h1>{_esc(act_title)}</h1>", _verification_badge(verification)]
    list_open = False

    def close_list():
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    def walk(tree_node):
        nonlocal list_open
        node = tree_node["node"]
        t = node["type"]
        if t == "section":
            href = f"{base_url}/section/{_strip_md(filenames_by_eid[tree_node['eid']])}"
            label = f"{node['number']} {node.get('heading') or ''}".strip()
            if not list_open:
                out.append('<ul class="section-list">')
                list_open = True
            out.append(f'<li><a href="{href}">{_esc(label)}</a></li>')
            return
        if t in (*structural_types, "heading_group"):
            close_list()
            level = min(_heading_level(t) + 1, 6)  # +1: the Act title itself is the page's own <h1>
            title = _display_title(t, node.get("number"), node.get("heading"))
            slug = index_slugs.get(tree_node["eid"])
            id_attr = f' id="{_esc(slug)}"' if slug else ""
            out.append(f"<h{level}{id_attr}>{_esc(title)}</h{level}>")
        for child in tree_node["children"]:
            walk(child)

    for root in tree_roots:
        walk(root)
    close_list()

    return "\n".join(out)


def render_section(parsed: dict, act_title: str, base_url: str, section_slug: str) -> str | None:
    """Renders the Section whose assign_filenames-computed id matches
    section_slug (the same string render_index links to), or None if no
    Section matches -- the caller (dashboard.py) turns that into a 404."""
    ctx = _build_context(parsed, act_title)
    sections = ctx["sections"]
    filenames_by_eid = ctx["filenames_by_eid"]

    target_filename = f"{section_slug}.md"
    match_index = next((i for i, (tn, _b) in enumerate(sections) if filenames_by_eid[tn["eid"]] == target_filename), None)
    if match_index is None:
        return None
    tree_node, breadcrumb = sections[match_index]
    node = tree_node["node"]

    linkify = _build_linkifier_html(ctx["section_files"], ctx["part_eids"], ctx["division_eids"], ctx["definitions"], base_url)
    title = f"{node['number']} {node.get('heading') or ''}".strip()
    verification = _collect_verification([tree_node])

    out = []
    crumb_bits = [f'<a href="{base_url}/">Act index</a>']
    crumb_bits.extend(_esc(_display_title(b["node"]["type"], b["node"].get("number"), b["node"].get("heading"))) for b in breadcrumb)
    out.append(f'<div class="breadcrumb">{" &raquo; ".join(crumb_bits)}</div>')
    out.append(_verification_badge(verification))
    out.append(f"<h1>{_esc(title)}</h1>")

    slugs = compute_section_slugs(tree_node)
    for unit in _iter_body_units(tree_node):
        key = (unit["tree_node"]["eid"], unit["clause_index"])
        if unit["header_text"] is not None:
            slug = slugs.get(key)
            id_attr = f' id="{_esc(slug)}"' if slug else ""
            level = min(unit["level"], 6)
            out.append(f"<h{level}{id_attr}>{_esc(unit['header_text'])}</h{level}>")
        if unit["text"] is not None:
            linked = linkify(_esc(unit["text"]), target_filename, slugs.get(key))
            out.append(f"<p>{linked}</p>")

    history: list[str] = []
    _render_history_html(tree_node, history)
    if history:
        out.append("<h2>History</h2>")
        out.append('<ul class="history">')
        out.extend(history)
        out.append("</ul>")

    nav = []
    if match_index > 0:
        prev_filename = filenames_by_eid[sections[match_index - 1][0]["eid"]]
        nav.append(f'<a href="{base_url}/section/{_strip_md(prev_filename)}">&laquo; Previous</a>')
    nav.append(f'<a href="{base_url}/">Act index</a>')
    if match_index + 1 < len(sections):
        next_filename = filenames_by_eid[sections[match_index + 1][0]["eid"]]
        nav.append(f'<a href="{base_url}/section/{_strip_md(next_filename)}">Next &raquo;</a>')
    out.append(f'<div class="section-nav">{" | ".join(nav)}</div>')

    return "\n".join(out)


PAGE_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b6b6b; --border: #d7d7d7;
  --accent: #2b6cb0; --done: #16a34a; --pending: #9ca3af; --flagged: #d97706;
  --sans: ui-sans-serif, system-ui, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: Georgia, "Times New Roman", serif; line-height: 1.65; }
.previewbar {
  background: #111827; color: #d1d5db; font-family: var(--sans); font-size: 12px;
  padding: 6px 20px; display: flex; gap: 14px; align-items: center;
}
.previewbar a { color: #93c5fd; }
.page { max-width: 760px; margin: 0 auto; padding: 26px 20px 60px; }
h1 { font-size: 22px; margin: 0 0 10px; font-family: var(--sans); }
h2 { font-size: 17px; margin: 30px 0 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; font-family: var(--sans); }
h3 { font-size: 15px; margin: 22px 0 6px; font-family: var(--sans); color: #333; }
h4, h5, h6 { font-size: 14px; margin: 16px 0 4px; font-weight: 600; font-family: var(--sans); }
p { margin: 0 0 13px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.breadcrumb { font-family: var(--sans); font-size: 12.5px; color: var(--muted); margin-bottom: 10px; }
.verify-badge { display: inline-block; font-family: var(--sans); font-size: 11.5px; padding: 2px 9px; border-radius: 10px; margin-bottom: 18px; }
.verify-full { background: #dcfce7; color: var(--done); }
.verify-partial { background: #fef3c7; color: var(--flagged); }
.verify-none { background: #e5e7eb; color: var(--pending); }
.section-list { list-style: none; padding-left: 0; margin: 0 0 10px; }
.section-list li { padding: 3px 0; font-family: var(--sans); font-size: 14px; }
.history { font-family: var(--sans); font-size: 12.5px; color: var(--muted); padding-left: 18px; }
.section-nav { margin-top: 32px; padding-top: 14px; border-top: 1px solid var(--border); font-family: var(--sans); font-size: 13px; }
"""


def page_shell(title: str, body_html: str, previewbar_html: str = "") -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>{_esc(title)}</title>\n<style>{PAGE_CSS}</style>\n</head>\n<body>\n"
        f"{previewbar_html}<div class=\"page\">\n{body_html}\n</div>\n</body>\n</html>"
    )
