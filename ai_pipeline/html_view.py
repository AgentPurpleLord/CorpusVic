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

A Section page is laid out the way the Act itself prints rather than the
way the Markdown export has to: subsections/paragraphs/subparagraphs are
indented by their nesting depth with their number hanging in the left
margin, and each provision's amendment-history notes sit in a margin
column beside it. Markdown has no indentation of its own to carry
structure with, so markdown_export.py turns every one of those into a
heading and pools the history at the foot -- that's a limitation of the
format, not the intended reading.

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

from .akn_export import build_hierarchy_tree
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


def _margin_notes_html(node: dict) -> str:
    """This one provision's own amendment-history notes, for the right-hand
    margin column beside it -- the same place the source PDF prints them,
    rather than pooled into one list at the foot of the page. A note whose
    own attachment was a guess (confidence "low" -- see tree.py's
    attach_history) is marked, so a reader can tell "the drafter put this
    here" apart from "the parser worked out where this probably goes"."""
    bits = []
    for h in node.get("history") or []:
        cls = "hist-note low" if h.get("confidence") == "low" else "hist-note"
        title = ' title="Attached to this provision as the closest match, not an exact citation"' if h.get("confidence") == "low" else ""
        bits.append(f'<span class="{cls}"{title}>{_esc(h["raw"])}</span>')
    return "".join(bits)


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

    # The body reads as the Act itself does: each provision indented by its
    # own nesting depth with its number hanging in the left margin, rather
    # than every subsection/paragraph becoming its own <h4>/<h5> heading the
    # way the Markdown export has to (Markdown has no indentation of its
    # own to carry structure with). The anchors those headings used to
    # provide are kept -- they're what cross-references from other sections
    # link into (see _build_linkifier_html's `fragment`) -- just moved onto
    # the provision <div> itself.
    slugs = compute_section_slugs(tree_node)
    out.append('<div class="provisions">')
    for unit in _iter_body_units(tree_node):
        unit_tree_node = unit["tree_node"]
        unit_node = unit_tree_node["node"]
        key = (unit_tree_node["eid"], unit["clause_index"])
        slug = slugs.get(key)
        id_attr = f' id="{_esc(slug)}"' if slug else ""

        classes = ["prov", f"prov-{_esc(unit_node['type'])}"]
        if unit["text"] is None:
            classes.append("prov-heading")  # a heading-only provision (a Subdivision caption, say)

        bits = []
        if unit["header_text"] is not None:
            # A defined term is set in bold italics where it's introduced
            # (the drafting convention -- see rule_parser.py's
            # _try_definition_start); every other label is just the
            # provision's own number, hanging left of its text.
            label_class = "prov-term" if unit_node["type"] == "definition" else "prov-num"
            bits.append(f'<span class="{label_class}">{_esc(unit["header_text"])}</span>')
        if unit["text"] is not None:
            # A number's gutter is CSS (.prov-num's own width), but a defined
            # term runs straight on into its text, so it needs a real space --
            # except where that text opens with punctuation ("appear, in
            # relation to a party, ..."), which must sit tight against it.
            if bits and unit_node["type"] == "definition" and not unit["text"].lstrip().startswith((",", ".", ";", ":", ")", "\u2014", "-")):
                bits.append(" ")
            bits.append(linkify(_esc(unit["text"]), target_filename, slug))

        # bits are joined with no separator on purpose: the gutter between a
        # provision's number and its text is the label span's own width and
        # padding (see .prov-num), so an extra space here would push the
        # first line out of line with the wrapped ones below it.
        out.append(f'<div class="{" ".join(classes)}"{id_attr} style="--depth:{unit["depth"]}">{"".join(bits)}</div>')
        # One margin cell per provision, empty or not: the two columns are
        # auto-placed rows of the same grid, so a note only stays level with
        # the provision it belongs to if every provision contributes a cell.
        notes = _margin_notes_html(unit_node) if unit["clause_index"] == 0 else ""
        out.append(f'<div class="prov-notes">{notes}</div>')
    out.append("</div>")

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
/* The palette (and the data-theme dark override below) is deliberately the
   same set of variable names static/review.html uses, driven by the same
   localStorage["reviewTheme"] key -- toggling the theme in the review GUI
   and then clicking through to a browse page keeps the theme, because both
   surfaces read the one preference. */
:root {
  color-scheme: light;
  --bg: #ffffff; --panel: #ffffff; --fg: #1a1a1a; --muted: #6b6b6b;
  --border: #d7d7d7; --accent: #2b6cb0;
  --done: #16a34a; --pending: #9ca3af; --flagged: #d97706;
  --verify-full-bg: #dcfce7; --verify-partial-bg: #fef3c7; --verify-none-bg: #e5e7eb;
  --bar-bg: #111827; --bar-fg: #d1d5db; --bar-link: #93c5fd;
  --sans: ui-sans-serif, system-ui, sans-serif;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #16181d; --panel: #1e2126; --fg: #e8e8ea; --muted: #9aa1ab;
  --border: #34383f; --accent: #5b9bd9;
  --done: #34d17f; --pending: #8b93a0; --flagged: #f0ad4e;
  --verify-full-bg: #132a1c; --verify-partial-bg: #2c2410; --verify-none-bg: #262a31;
  --bar-bg: #05070a; --bar-fg: #b6bcc6; --bar-link: #7fb6ea;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: Georgia, "Times New Roman", serif; line-height: 1.65; }
.previewbar {
  background: var(--bar-bg); color: var(--bar-fg); font-family: var(--sans); font-size: 12px;
  padding: 6px 20px; display: flex; gap: 14px; align-items: center;
}
.previewbar a { color: var(--bar-link); }
.page { max-width: 980px; margin: 0 auto; padding: 26px 20px 60px; }
h1 { font-size: 22px; margin: 0 0 10px; font-family: var(--sans); }
h2 { font-size: 17px; margin: 30px 0 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; font-family: var(--sans); }
h3 { font-size: 15px; margin: 22px 0 6px; font-family: var(--sans); color: var(--fg); }
h4, h5, h6 { font-size: 14px; margin: 16px 0 4px; font-weight: 600; font-family: var(--sans); }
p { margin: 0 0 13px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.breadcrumb { font-family: var(--sans); font-size: 12.5px; color: var(--muted); margin-bottom: 10px; }
.verify-badge { display: inline-block; font-family: var(--sans); font-size: 11.5px; padding: 2px 9px; border-radius: 10px; margin-bottom: 18px; }
.verify-full { background: var(--verify-full-bg); color: var(--done); }
.verify-partial { background: var(--verify-partial-bg); color: var(--flagged); }
.verify-none { background: var(--verify-none-bg); color: var(--pending); }
.section-list { list-style: none; padding-left: 0; margin: 0 0 10px; }
.section-list li { padding: 3px 0; font-family: var(--sans); font-size: 14px; }
.section-nav { margin-top: 32px; padding-top: 14px; border-top: 1px solid var(--border); font-family: var(--sans); font-size: 13px; }

/* The Section body, laid out the way the Act itself prints: one two-column
   grid whose rows alternate provision / margin-note, so a note stays level
   with the provision it belongs to (that's why render_section emits an
   empty .prov-notes cell for every provision, not just annotated ones).
   Indentation carries the structure -- --depth is the provision's nesting
   distance below the Section -- with the number hanging in the margin to
   its left, so a subsection reads as a subsection without needing its own
   heading. */
.provisions { display: grid; grid-template-columns: minmax(0, 1fr) 190px; column-gap: 24px; }
.prov {
  margin: 0 0 11px;
  padding-left: calc(var(--depth, 0) * 26px + 2.4em);
  text-indent: -2.4em;   /* pulls the first line back out so the number hangs */
}
.prov-num { display: inline-block; min-width: 1.9em; padding-right: 0.5em; }
.prov-term { font-weight: 600; font-style: italic; }
.prov-heading {
  font-family: var(--sans); font-weight: 600; font-size: 14px;
  margin: 20px 0 8px; text-indent: 0;
  padding-left: calc(var(--depth, 0) * 26px);
}
.prov-notes { font-family: var(--sans); font-size: 11.5px; color: var(--muted); line-height: 1.45; }
.hist-note { display: block; margin-bottom: 5px; }
/* A note the parser placed by proximity rather than by an explicit
   citation -- flagged so a reader can tell a guess from a certainty. */
.hist-note.low { border-left: 2px solid var(--border); padding-left: 6px; font-style: italic; }

.theme-toggle {
  position: fixed; top: 10px; right: 14px; z-index: 30;
  border: 1px solid var(--border); background: var(--panel); color: var(--fg);
  border-radius: 6px; padding: 4px 9px; cursor: pointer; font-size: 13px;
  font-family: var(--sans);
}

/* Below the width the two columns need, the margin notes fold in underneath
   their provision rather than being squeezed into an unreadable strip. */
@media (max-width: 720px) {
  .provisions { display: block; }
  .prov-notes { padding-left: 12px; margin: -4px 0 12px; }
}
"""

# Applied in <head>, before first paint, so a dark-mode reader doesn't get a
# white flash on every page load; the button wiring below runs after the DOM
# exists. Both halves read/write the same key static/review.html does.
THEME_HEAD_SCRIPT = """
try {
  if (localStorage.getItem("reviewTheme") === "dark") document.documentElement.dataset.theme = "dark";
} catch (e) {}
"""

THEME_BODY_SCRIPT = """
(function () {
  var btn = document.getElementById("theme-toggle-btn");
  function paint() {
    var dark = document.documentElement.dataset.theme === "dark";
    btn.innerHTML = dark ? "&#9728;&#65039;" : "&#127769;";
    btn.title = dark ? "Switch to light mode" : "Switch to dark mode";
  }
  paint();
  btn.onclick = function () {
    var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    if (next === "dark") document.documentElement.dataset.theme = "dark";
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("reviewTheme", next); } catch (e) {}
    paint();
  };
})();
"""


def page_shell(title: str, body_html: str, previewbar_html: str = "") -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>{PAGE_CSS}</style>\n"
        f"<script>{THEME_HEAD_SCRIPT}</script>\n</head>\n<body>\n"
        f"{previewbar_html}"
        "<button class=\"theme-toggle\" id=\"theme-toggle-btn\" type=\"button\">&#127769;</button>\n"
        f"<div class=\"page\">\n{body_html}\n</div>\n"
        f"<script>{THEME_BODY_SCRIPT}</script>\n</body>\n</html>"
    )
