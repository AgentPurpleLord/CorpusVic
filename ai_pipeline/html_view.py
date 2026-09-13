"""
Renders a parsed Act as a live, read-only, AustLII-style HTML browsing
view -- an index page (Part/Division/Subdivision headings, each Section
listed as a link, in document order) plus one page per Section, with
defined terms and Part/Division/"section N" cross-references
hyperlinked between them, the same way a real AustLII page reads.

This reuses markdown_export.py's document model wholesale -- the tree
walk, filename and slug assignment, and definition/cross-reference
collection are all the exact same functions that module already uses
to write Markdown files (see the imports below). The only things that
differ here are the output format (HTML strings with real <a href>
links, rendered straight into an HTTP response) and the data source:
dashboard.py calls this against review.py's build_current_nodes
(verified where a unit's been committed, the original parse otherwise
-- see its own docstring), not a static file, and does so fresh on
every request. The point is letting a reviewer immediately see how
their in-progress edits will read to an actual user, without running
export_markdown.py as a separate step first.

A Section page is laid out the way the Act itself prints, rather than
the way the Markdown export has to: subsections, paragraphs and
subparagraphs are indented by their nesting depth with their number
hanging in the left margin, and each provision's amendment-history
notes sit in a margin column beside it. Markdown has no indentation of
its own to carry structure with, so markdown_export.py turns every one
of those into a heading and gathers the history at the foot instead --
that's a limitation of the format, not how it's meant to be read.

The Act's own Endnotes get a page of their own (render_endnotes): the
General information block, the Table of Amendments read as a real
table -- each amending Act with its assent and commencement dates and
the provisions of this Act it actually touched -- and the Explanatory
details. Each margin note on a Section page links into it, naming the
Act behind its citation, because a note only ever says "No. 68/2009"
and no reader keeps a hundred Act numbers in their head.

A Section page also carries an "Explained in" bar: the Bill clause it
was enacted from and the Explanatory Memorandum's note on it, worked
out by ai_pipeline/commentary.py from run_bill_linking.py's link
records and handed here as ready-made chips. They're ordinary links
into those documents' own browse pages, so hovering one answers "what
does the EM say about this provision?" without leaving the section.

The same renderers serve a Bill and an Explanatory Memorandum too, not
just an Act: those call their top-level provisions clauses rather than
sections (see hierarchy.SECTION_LEVEL_TYPES), and an EM's entries carry
no headings at all, so an index row falls back to the start of the
entry's own text.

Every link on those pages also has a hover preview: pause on a defined
term, a "section N" reference or a "Part N" reference, and a small
card shows what's behind it -- the definition and its own paragraphs,
the Section's opening provisions, the Sections under that Part.
Checking what a term means is the single most common reason to follow
a link here, and following it costs you your place, so render_preview
builds those cards, and static/site/preview.js is the browser side
of it.

Kept deliberately independent of review.py's own live server process:
rendering a page here needs no interactive state (no edit, split or
merge), just whatever build_current_nodes reads off disk, so a Section
page can be rendered without that Act's review.py child process even
running.

A gap shared with markdown_export.py: cross-reference and defined-term
matching relies on text patterns (see definitions.py), not a
guarantee -- an unmatched or ambiguous mention is left as plain text
rather than linked to the wrong place.
"""
import html
import re
from pathlib import Path

from .akn_export import build_hierarchy_tree
from .amendments import anchor_id, describe, linkify_note
from .diffing import provision_identity
from .hierarchy import HIERARCHY_ORDER, SECTION_LEVEL_TYPES, schedule_is_pageable, schedule_numbers
from .act_registry import load_act_registry
from .link_targets import load_known_acts
from .markdown_export import (
    _DIVISION_REF_RE,
    _PART_REF_RE,
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
    index_label,
    page_title,
    section_ref_pattern,
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
    # "section N" in an Act, "clause N" in a Bill or an EM -- decided
    # from the document's own provisions (see markdown_export's own
    # comment on why this isn't simply always matching both words).
    secref_re = section_ref_pattern(sections)
    # A Bill's or an EM's front page isn't an "Act index" -- calling it
    # one on every page of both was the kind of small wrongness that
    # makes a reader doubt everything else on the page.
    index_link_text = "Contents" if any(tn["node"]["type"] == "clause" for tn, _b in sections) else "Act index"

    # Part/Division eId lookups for prose "Part N" / "Division N"
    # links -- the same one-pass walk export_to_markdown does for the
    # same reason.
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
        "secref_re": secref_re,
        "index_link_text": index_link_text,
    }


# An Act or Bill's name as it would actually appear inline in a
# sentence: a run of Capitalised words (allowing a handful of lowercase
# connectors -- "of", "the", "and", ... -- since a title routinely
# carries them, "Justice Legislation Amendment (Sexual Offences and
# Other Matters) Act 2022") that ends in "Act"/"Bill" and a year. This
# is never matched on its own: a citation this loose would catch plenty
# of prose that merely happens to end that way ("A person authorised by
# or under section 229 of the Transport ... Act 1983" would swallow the
# whole clause if the connector list were too generous, or a bare
# capital letter were allowed to start it) -- what makes it safe is
# that a match is thrown away unless it's then found *word for word* in
# known_acts.yaml or act_registry.json (see _build_linkifier_html), so
# an over-matched or truncated span (most of them, in practice -- a
# parenthesised subtitle breaks the word-by-word chain this pattern
# requires) just fails to link rather than linking to the wrong place.
# This is also why the registry's roughly 8000 titles are never turned
# into one giant matching pattern the way known_acts.yaml's own handful
# are elsewhere in this function: one small fixed pattern here, then a
# dict lookup for each candidate it happens to find, costs nothing
# close to compiling a pattern that size on every page.
_ACT_TITLE_WORD = r"[A-Z][\w'()-]*"
_ACT_TITLE_CONNECTOR = r"(?:of|the|and|for|in|on|to|by|or)"
# The whole span is wrapped in (?-i:...): master (below) is compiled
# case-insensitively for the sake of def/secref/partref/divref, and
# under that flag [A-Z] would also match a lowercase letter -- which is
# exactly the capitalisation check this pattern exists to enforce, so
# it has to opt back out of that flag explicitly rather than inherit it.
_ACT_TITLE_SPAN_RE = (
    rf"(?-i:\b{_ACT_TITLE_WORD}(?:\s+(?:{_ACT_TITLE_WORD}|{_ACT_TITLE_CONNECTOR}))*"
    rf"\s+(?:Act|Bill)\s+(?:18|19|20)\d{{2}}\b)"
)


def _build_linkifier_html(section_files: dict[str, str], part_eids: dict[str, str], division_eids: dict[str, str], definitions: dict[str, dict], base_url: str, secref_re: str, own_title: "str | None" = None):
    """Same pattern and priority scheme as
    markdown_export._build_linkifier, but produces <a href> tags instead
    of Markdown link syntax. Must only ever be called on text that's
    *already* been HTML-escaped (see _esc) -- the patterns below match
    plain words and digits, never anything an escape pass would have
    changed, so escaping first and linkifying second is safe: the
    substituted spans are exact, already-escaped slices of the input,
    never re-derived from unescaped source.

    Also links a mention of another Act by name -- "the Crimes Act
    1958", say, in a Schedule's own prose. A candidate span (see
    _ACT_TITLE_SPAN_RE) is checked first against
    ai_pipeline/known_acts.yaml (this pipeline's own parsed Acts, linked
    straight to their /browse/ page) and, failing that, against the
    general act_registry.json (linked instead to the standing
    /legislation/<act_no> resolver -- see dashboard.py's
    legislation_resolver -- for an Act this pipeline hasn't parsed).
    `own_title` -- this document's own citation -- is excluded from
    both, so an Act's own name inside its own text doesn't link to
    itself."""
    known_acts = {slug: title for slug, title in load_known_acts().items() if title != own_title}
    known_acts_by_lower = {title.lower(): (slug, title) for slug, title in known_acts.items()}
    registry_by_lower = {
        title.lower(): (title, entry) for title, entry in load_act_registry().items() if title != own_title
    }

    parts = []
    if definitions:
        term_alt = "|".join(re.escape(t) for t in sorted(definitions, key=lambda t: (-len(t), t)))
        parts.append(f"(?P<def>\\b(?:{term_alt})\\b)")
    parts.append(f"(?P<secref>{secref_re})")
    parts.append(f"(?P<partref>{_PART_REF_RE})")
    parts.append(f"(?P<divref>{_DIVISION_REF_RE})")
    parts.append(f"(?P<actref>{_ACT_TITLE_SPAN_RE})")
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
                return text  # already sitting under this exact heading -- don't link a term back to itself
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
        if m.lastgroup == "actref":
            # A leading "The "/"the " is how a sentence actually names
            # an Act ("under the Public Administration Act 2004"), but
            # is never part of the Act's own citation -- stripped
            # before either lookup, exactly as
            # link_targets.resolve_act_citation already does for a
            # reviewer-labelled citation span.
            key = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE).lower()
            known = known_acts_by_lower.get(key)
            if known:
                return f'<a href="{_site_prefix(base_url)}/browse/{known[0]}/">{text}</a>'
            registry = registry_by_lower.get(key)
            if registry:
                _title, entry = registry
                href = _legislation_href(entry, base_url)
                return f'<a class="unresolved" href="{href}" title="Not yet parsed into this pipeline">{text}</a>'
            return text
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


def _site_prefix(base_url: str) -> str:
    """The part of a page's own base_url before "/browse/<slug>" --
    "" when a page is served from the domain root (the live dashboard,
    per deploy/README.md), or a path like "/repo-name" when the whole
    site sits under a subpath (a GitHub Pages project site -- see
    export_static_site.py). Used by the handful of links below that
    don't already build on base_url the way every in-Act link does, so
    they still land inside the site instead of at the real domain root."""
    return base_url.rsplit("/browse/", 1)[0] if "/browse/" in base_url else ""


def _legislation_href(citation: dict, base_url: str = "") -> str:
    """The standing address for a citation this pipeline detected but
    doesn't (yet) know how to name -- /legislation/<act_no>[-<year>]
    (see dashboard.py's legislation_resolver), which redirects to that
    Act's own parse once one exists, and otherwise says plainly that it
    hasn't been parsed yet. Every citation this pipeline notices becomes
    a link to *something*; this is the standing "something" for one
    that resolved to nothing more specific."""
    act_no = citation.get("act_no")
    year = citation.get("year")
    prefix = _site_prefix(base_url)
    return f"{prefix}/legislation/{act_no}-{year}" if year else f"{prefix}/legislation/{act_no}"


def _linked_citation_html(run: dict, base_url: str, css_class: str) -> str:
    """One linkify_note run, marked up: a resolved citation links into
    this Act's own Endnotes entry (full name and dates in the tooltip);
    an unresolved one -- still detected, just not nameable from what
    this Act or the general registry holds -- links to the standing
    resolver instead, marked so a reader can tell "click through to
    read this elsewhere" apart from "click through to find out this
    hasn't been parsed yet". Plain text only for a run that named no
    citation at all."""
    record = run.get("record")
    if record is not None:
        href = f'{base_url}/endnotes#{anchor_id(record.get("citation"))}'
        return f'<a class="{css_class}" href="{_esc(href)}" title="{_esc(describe(record))}">{_esc(run["text"])}</a>'
    citation = run.get("citation")
    if citation is not None:
        href = _legislation_href(citation, base_url)
        return (f'<a class="{css_class} unresolved" href="{_esc(href)}" '
                f'title="Not yet parsed into this pipeline">{_esc(run["text"])}</a>')
    return _esc(run["text"])


def _margin_notes_html(node: dict, base_url: str = "", amendment_index: dict | None = None) -> str:
    """This one provision's own amendment-history notes, for the right-
    hand margin column beside it -- the same place the source PDF
    prints them, rather than gathered into one list at the foot of the
    page. A note whose attachment was a guess (confidence "low" -- see
    tree.py's attach_history) is marked, so a reader can tell "the
    drafter put this here" apart from "the parser worked out where this
    probably goes".

    Given an amendment_index (see ai_pipeline/amendments.py), the
    citation *within* the note is a link to that Act's entry in the
    Endnotes, with the Act's full name and dates in its tooltip. The
    citation is what the note actually says and what a reader wants to
    click; spelling the Act out in full beside every note would push
    the note itself out of the margin it's printed in, for a name
    that's just one hover away. A citation this pipeline can't name at
    all still links, to the standing /legislation/<act_no> resolver --
    see _linked_citation_html."""
    bits = []
    for h in node.get("history") or []:
        low = h.get("confidence") == "low"
        cls = "hist-note low" if low else "hist-note"
        title = ' title="Attached to this provision as the closest match, not an exact citation"' if low else ""
        marked = [_linked_citation_html(run, base_url, "hist-act") for run in linkify_note(h["raw"], amendment_index)]
        bits.append(f'<span class="{cls}"{title}>{"".join(marked)}</span>')
    return "".join(bits)


def build_page_index(parsed: dict, act_title: str) -> dict:
    """Where each of this document's top-level provisions lives, for a
    caller building links *into* it from somewhere else (dashboard.py,
    turning an Act section's Bill/EM links into hrefs, or a timeline
    entry's own version into a page for it). Returns
    {"by_node_index": {position in parsed["nodes"] -> page id},
     "by_key": {diffing.provision_identity(type, schedule, number) -> page id},
     "schedule_by_node_index": {position -> the Schedule it sits in}} --
    page id being what render_index links to and render_section matches
    on.

    Keyed by Schedule as well as number because a Schedule starts
    numbering its own provisions from 1 again: this Act has a section
    11 and a Schedule 1 clause 11, on different pages ("s11" and
    "s11_2"), and a lookup by number alone used to silently return the
    first of them for both. Keyed by kind too (diffing.provision_identity,
    not the bare commentary.provision_key most other callers use)
    because a pageable Schedule (see hierarchy.schedule_is_pageable) is
    addressed by its own number with no Schedule of its own to sit in
    -- the same (None, number) pair an ordinary body section with that
    number would use -- and the two must not collide."""
    ctx = _build_context(parsed, act_title)
    position_of = {id(node): i for i, node in enumerate(parsed["nodes"])}
    schedules = schedule_numbers(parsed["nodes"])
    by_node_index = {}
    by_key = {}
    schedule_by_node_index = {}
    for tree_node, _breadcrumb in ctx["sections"]:
        node = tree_node["node"]
        position = position_of.get(id(node))
        if position is None:
            continue
        page = _strip_md(ctx["filenames_by_eid"][tree_node["eid"]])
        # A Schedule isn't "inside itself": its own page is keyed as
        # sitting in no Schedule at all, the same way diffing.py's own
        # provisions() keys it -- see that function's docstring.
        schedule = None if node["type"] == "schedule" else schedules[position]
        by_node_index[position] = page
        schedule_by_node_index[position] = schedule
        by_key.setdefault(provision_identity(node["type"], schedule, node.get("number")), page)
    return {
        "by_node_index": by_node_index,
        "by_key": by_key,
        "schedule_by_node_index": schedule_by_node_index,
    }


def render_index(parsed: dict, act_title: str, base_url: str,
                 superseded: dict | None = None, unpublished_pages: "set[str] | None" = None,
                 show_review_badge: bool = True) -> str:
    """base_url is this Act's own root, e.g. "/browse/crimes-act" (no
    trailing slash) -- every link rendered here and in render_section
    is built from it, so the caller controls the URL scheme entirely.

    superseded, if given, is {"version", "current", "current_url",
    "as_at_printed"} -- see render_superseded_banner.

    unpublished_pages, if given, is the page ids whose provision hasn't
    been released to readers yet (see export_static_site.py, which
    publishes an Act a reviewed provision at a time). They stay in the
    contents list, still linked, and are marked -- the page they lead to
    says the same thing. Leaving them out instead would make a
    part-published Act look complete, which for legislation is the
    dangerous reading: a missing section must never look like a section
    that doesn't exist. None (the live dashboard, which shows
    everything) marks nothing.

    show_review_badge is how much of the Act a human has checked, which
    is what a reviewer wants to know and the wrong thing to tell a
    reader: on a site that only publishes checked provisions, "partially
    reviewed" reads as doubt about the text actually on screen. Off
    there; on for the dashboard, whose whole job is tracking it."""
    ctx = _build_context(parsed, act_title)
    tree_roots = ctx["tree_roots"]
    structural_types = ctx["structural_types"]
    filenames_by_eid = ctx["filenames_by_eid"]
    index_slugs = ctx["index_slugs"]
    verification = _collect_verification(tree_roots)

    out = [f"<h1>{_esc(act_title)}</h1>"]
    if show_review_badge:
        out.append(_verification_badge(verification))
    if superseded:
        out.append(render_superseded_banner(
            superseded.get("version"), superseded.get("current"),
            superseded.get("current_url"), superseded.get("as_at_printed"),
        ))
    # Which reprint of the Act this is, as the PDF's own front matter
    # states it (see ai_pipeline/versions.py). This is just a statement
    # of fact, not yet a judgement about whether it's current --
    # knowing whether this has been superseded needs to know what other
    # versions exist, which is the timeline's job. This is stated as
    # this pipeline's own reading of the document, never as "the
    # Authorised Version", which is the name for the government's own
    # published text, not for anything reconstructed from it here.
    version = parsed.get("version") or {}
    if version.get("version") is not None:
        as_at = f' &mdash; incorporating amendments as at {_esc(version["as_at_printed"])}' if version.get("as_at_printed") else ""
        out.append(f'<div class="act-version">Version {_esc(str(version["version"]))}{as_at}</div>')
    if parsed.get("endnotes"):
        out.append(
            f'<div class="index-nav"><a href="{base_url}/endnotes">Endnotes</a> '
            "&mdash; general information, the Table of Amendments, explanatory details</div>"
        )
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
        pageable_schedule = t == "schedule" and schedule_is_pageable(tree_node)
        if t in SECTION_LEVEL_TYPES or pageable_schedule:
            href = f"{base_url}/section/{_strip_md(filenames_by_eid[tree_node['eid']])}"
            # A pageable Schedule gets the same "type spelled out"
            # label its own heading used to have, before it became a
            # page instead of a bare <h4> above a list -- "3 Persons
            # who may witness..." would otherwise read as though 3
            # were this Act's own section number, which it isn't (see
            # hierarchy.schedule_is_pageable).
            label = _display_title(t, node.get("number"), node.get("heading")) if pageable_schedule else index_label(node)
            if not list_open:
                out.append('<ul class="section-list">')
                list_open = True
            page_id = _strip_md(filenames_by_eid[tree_node["eid"]])
            mark = (
                ' <span class="unpublished-tag">not yet published</span>'
                if unpublished_pages and page_id in unpublished_pages else ""
            )
            out.append(f'<li><a href="{href}">{_esc(label)}</a>{mark}</li>')
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


def _crossrefs_html(crossrefs: list[dict]) -> str:
    """The "where else this provision is explained" bar -- one chip per
    related document (the Bill clause this section was enacted from, an
    Explanatory Memorandum note about it). Each chip is an ordinary
    link into that document's own page, so hovering one previews it the
    same way every other link on the page does; the caller
    (dashboard.py, via ai_pipeline/commentary.py) works out what
    belongs here."""
    if not crossrefs:
        return ""
    chips = "".join(
        f'<a class="crossref crossref-{_esc(ref.get("kind") or "other")}" href="{_esc(ref["href"])}"'
        f'{f" title=" + chr(34) + _esc(ref["title"]) + chr(34) if ref.get("title") else ""}>'
        f'{_esc(ref["label"])}</a>'
        for ref in crossrefs
    )
    return f'<div class="crossrefs"><span class="crossrefs-label">Explained in</span>{chips}</div>'


# ---------------------------------------------------------------------------
# A provision's timeline
# ---------------------------------------------------------------------------
# An Act is reprinted every few weeks and each reprint restates the
# whole thing, so the only way to see how a provision's wording has
# moved is to compare the reprints (ai_pipeline/diffing.py). What comes
# back is rendered here, on the provision's own page, because that's
# where a reader is when the question occurs to them -- not on a
# separate compare-two-versions screen they'd have to know to go and
# look for.
#
# It's shown only on a provision that actually changed. A control on
# every provision that mostly just says "nothing happened" trains a
# reader to stop opening it, which costs more than it gives.


def _diff_html(diff: list[dict]) -> str:
    """One version's change, showing how the words moved: what went,
    struck through, and what arrived, marked. Unchanged words are kept
    around them so a reader sees the amendment in its sentence rather
    than as a pair of disembodied phrases -- which is how the amending
    Act itself reads ("in section 366(1)(d), after 'offence' insert
    ...")."""
    out = []
    for segment in diff:
        text = _esc(segment["text"])
        if segment["op"] == "equal":
            out.append(f'<span class="d-eq">{text}</span>')
        elif segment["op"] == "delete":
            out.append(f'<del class="d-del">{text}</del>')
        else:
            out.append(f'<ins class="d-ins">{text}</ins>')
    return " ".join(out)


def _timeline_note_html(raw: str, base_url: str, amendment_index: "dict | None") -> str:
    """One amendment note, with the Act it names linked to that Act's
    entry in the Endnotes -- the same treatment the note gets in the
    margin, so the citation means the same thing and goes to the same
    place no matter where a reader meets it (see
    _linked_citation_html)."""
    marked = [_linked_citation_html(run, base_url, "hist-act") for run in linkify_note(raw, amendment_index)]
    return f'<span class="tl-note">{"".join(marked)}</span>'


def _timeline_entry_html(entry: dict, base_url: str, amendment_index: "dict | None",
                         version_urls: "dict | None") -> str:
    """One point on a provision's timeline: the version it changed at, what
    the Act says did the changing, and the words that moved."""
    version = entry.get("version")
    when = entry.get("as_at_printed")
    stamp = f"Version {version}" if version is not None else "Earlier version"
    if when:
        stamp += f" \u2014 as at {_esc(when)}"
    href = (version_urls or {}).get(version)
    heading = f'<a class="tl-version" href="{_esc(href)}">{stamp}</a>' if href else f'<span class="tl-version">{stamp}</span>'

    change = entry.get("change")
    # The Act's own words for what happened to a provision. Using
    # anything else here would have the timeline describe the
    # amendment in language the amendment itself doesn't use.
    verb = {"inserted": "Inserted", "repealed": "Repealed", "changed": "Amended"}.get(change, "Changed")
    notes = "".join(_timeline_note_html(raw, base_url, amendment_index)
                    for raw in entry.get("new_history") or [])
    body = f'<div class="tl-diff">{_diff_html(entry["diff"])}</div>' if entry.get("diff") else ""
    note_block = f'<div class="tl-notes">{notes}</div>' if notes else ""
    return (
        f'<li class="tl-entry tl-{_esc(change or "changed")}">'
        f'<div class="tl-head">{heading}<span class="tl-verb">{verb}</span></div>'
        f'{note_block}{body}</li>'
    )


def render_timeline(entries: list[dict], base_url: str, amendment_index: "dict | None" = None,
                    version_urls: "dict | None" = None) -> str:
    """A provision's history across the versions of the Act held here,
    or "" where it has none.

    Newest first: a reader arriving at this control almost always
    wants the most recent change, and having to scroll a long timeline
    to reach it would make the common case the expensive one. The
    collapsed summary says how many changes there are and when the
    last one was, so the control answers the first question without
    being opened.
    """
    if not entries:
        return ""
    newest_first = sorted(entries, key=lambda e: (e.get("version") is None, -(e.get("version") or 0)))
    latest = newest_first[0]
    count = len(newest_first)
    when = latest.get("as_at_printed")
    summary = f"{count} change{'s' if count != 1 else ''} across the versions held here"
    if when:
        summary += f"; most recent as at {_esc(when)}"
    items = "".join(_timeline_entry_html(e, base_url, amendment_index, version_urls) for e in newest_first)
    return (
        '<details class="timeline">'
        f'<summary class="timeline-summary">This provision has changed &mdash; '
        f'<span class="tl-count">{summary}</span></summary>'
        f'<ol class="tl-list">{items}</ol>'
        "</details>"
    )


def render_superseded_banner(version: "int | None", current: "int | None", current_url: "str | None",
                             as_at_printed: "str | None" = None) -> str:
    """The notice on a version that's no longer the law.

    Shown at the top of every page of a superseded version, not just
    where that page's own provision changed: a reader who's arrived at
    an old reprint is reading the wrong law whether or not this
    particular section is one of the ones that moved, and finding that
    out at the bottom of the page is finding it out too late.
    """
    if version is None or current is None or version >= current:
        return ""
    when = f" (as at {_esc(as_at_printed)})" if as_at_printed else ""
    link = (f' <a class="supersede-link" href="{_esc(current_url)}">Go to Version {current}</a>'
            if current_url else "")
    return (
        f'<div class="supersede" role="status">This is <strong>Version {version}</strong>{when} '
        f'and is not the law as it now stands &mdash; Version {current} is.{link}</div>'
    )


# ---------------------------------------------------------------------------
# The section reading view
# ---------------------------------------------------------------------------
# A section page carries three pieces of furniture the contents and
# Endnotes pages don't: an outline of the rest of the document beside the
# text, a bar of reading controls above it, and the provisions either side
# of this one below it. See static/site/reader.css and reader.js for the
# other half of each.


def _outline_entry(tree_node: dict, base_url: str, filenames_by_eid: dict[str, str],
                   target_filename: str, unpublished_pages: "set[str] | None") -> str:
    """One section in the outline, marked when it is the page you're on --
    aria-current, so it reads as "you are here" to a screen reader and not
    merely as a different colour.

    A section whose provision hasn't been released to readers yet is
    marked too, the way the contents page marks it (see render_index): a
    line that looks like every other one, and turns out to be a page
    saying the text isn't there, is worse than one that says so first. The
    mark is a dot rather than the contents page's "not yet published",
    which at this width would set most of the sidebar in two-line
    entries -- with the words themselves kept for a screen reader, which
    has no dot to see."""
    filename = filenames_by_eid[tree_node["eid"]]
    node = tree_node["node"]
    label = (
        _display_title(node["type"], node.get("number"), node.get("heading"))
        if node["type"] == "schedule" else index_label(node)
    )
    page_id = _strip_md(filename)
    current = ' aria-current="page"' if filename == target_filename else ""
    held_back = bool(unpublished_pages) and page_id in unpublished_pages
    mark = '<span class="outline-unpub"> (not yet published)</span>' if held_back else ""
    css = ' class="unpublished"' if held_back else ""
    return (
        f'<li class="outline-leaf"><a href="{base_url}/section/{page_id}"{current}{css}>'
        f"{_esc(label)}{mark}</a></li>"
    )


def _outline_html(ctx: dict, act_title: str, breadcrumb: list[dict], target_filename: str,
                  base_url: str, has_endnotes: bool = False,
                  unpublished_pages: "set[str] | None" = None) -> str:
    """The provisions around the one you're reading -- and only those.

    Just the branch this page sits on: the Part it is in, the Division
    within that, and the sections beside it. Not the other Parts, not the
    Schedules, not the whole Act. An outline that listed everything would
    be a second contents page in every sidebar (the Criminal Procedure Act
    alone would put over a thousand links on every page of itself), and
    what a reader wants beside a provision is its immediate neighbourhood.
    Going further than that is what the contents page and the Home button
    are for, and both are linked at the top of this.

    A structural line links to its heading on the contents page (the same
    anchor render_index gives it), because a Part is not a page here. The
    outline also names the document, which a section page otherwise never
    states: the heading is the provision's, and "Act index" doesn't say
    which index."""
    structural_types = ctx["structural_types"]
    filenames_by_eid = ctx["filenames_by_eid"]
    index_slugs = ctx["index_slugs"]
    open_eids = {b["eid"] for b in breadcrumb}

    def children_html(tree_node: dict) -> str:
        items = []
        for child in tree_node["children"]:
            node = child["node"]
            t = node["type"]
            if t in SECTION_LEVEL_TYPES or (t == "schedule" and schedule_is_pageable(child)):
                items.append(_outline_entry(child, base_url, filenames_by_eid,
                                            target_filename, unpublished_pages))
                continue
            # A Part or Division that isn't on the way to this page is not
            # the immediate context, so it isn't listed at all.
            if t not in (*structural_types, "heading_group") or child["eid"] not in open_eids:
                continue
            title = _display_title(t, node.get("number"), node.get("heading"))
            slug = index_slugs.get(child["eid"])
            href = f"{base_url}/#{_esc(slug)}" if slug else f"{base_url}/"
            items.append(
                f'<li class="outline-struct"><a href="{href}">{_esc(title)}</a>'
                f"{children_html(child)}</li>"
            )
        return f'<ul class="outline-list">{"".join(items)}</ul>' if items else ""

    body = "".join(
        children_html({"children": [root], "node": {"type": ""}, "eid": ""})
        for root in ctx["tree_roots"]
    )
    endnotes = (
        f'<a class="outline-endnotes" href="{base_url}/endnotes">Endnotes</a>' if has_endnotes else ""
    )
    return (
        '<nav class="outline" aria-label="Contents">'
        f'<a class="outline-doc" href="{base_url}/">{_esc(act_title)}</a>'
        f'<a class="outline-contents" href="{base_url}/">{_esc(ctx["index_link_text"])}</a>'
        f"{endnotes}{body}</nav>"
    )


def _version_choices_html(version_urls: "dict | None", version_dates: "dict | None",
                          this_version: "int | None") -> str:
    """"Compare with another version" -- every other version of the Act
    held here that pages this same provision, so the comparison lands on
    the same words rather than on that version's front page.

    A disclosure rather than a button that goes somewhere: which version
    you want is a choice, and the dates are what you make it on. Nothing
    at all where this is the only version that has the provision, which is
    most documents -- a control offering no choices is worse than none."""
    others = sorted(
        (version, url) for version, url in (version_urls or {}).items() if version != this_version
    )
    if not others:
        return ""
    items = []
    for version, url in reversed(others):  # newest first: the likeliest comparison
        when = (version_dates or {}).get(version)
        dated = f' <span class="version-date">as at {_esc(when)}</span>' if when else ""
        items.append(f'<li><a href="{_esc(url)}">Version {_esc(str(version))}</a>{dated}</li>')
    return (
        '<details class="versions"><summary>Compare with another version</summary>'
        f'<ul class="version-list">{"".join(items)}</ul></details>'
    )


def _readerbar_html(version: dict, superseded: "dict | None", version_urls: "dict | None",
                    version_dates: "dict | None") -> str:
    """The bar above the text: which day's law this is, how to compare it
    with another, and the two reading controls.

    "Text as at" is the date the reprint itself states it incorporates
    amendments to, read off the PDF's front matter -- not the day the file
    was parsed, and never offered as the authorised text. A document with
    no version at all (a Bill, an Explanatory Memorandum) says nothing
    rather than guessing, and keeps the reading controls."""
    bits = []
    as_at = version.get("as_at_printed")
    this_version = version.get("version")
    if as_at or this_version is not None:
        stated = _esc(as_at) if as_at else f"Version {_esc(str(this_version))}"
        # "Current" only where there is something to be current against:
        # what this tool knows is the versions it holds, so on a document
        # with only one the claim would be about nothing (see
        # dashboard._superseded).
        tag = ""
        if version_urls and len(version_urls) > 1:
            tag = (
                '<span class="asat-tag superseded">Superseded</span>' if superseded
                else '<span class="asat-tag">Current</span>'
            )
        bits.append(
            f'<div class="asat"><span class="asat-label">Text as at</span> '
            f"<strong>{stated}</strong>{tag}</div>"
        )
    bits.append(_version_choices_html(version_urls, version_dates, this_version))
    # The controls are written out by hand rather than by reader.js so that
    # they are in the HTML a reader without JavaScript gets -- disabled
    # there, but never a row of buttons that silently do nothing.
    bits.append(
        '<div class="readerctl" hidden>'
        '<span class="ctl-label">Size</span>'
        '<button type="button" class="ctl-btn" id="reader-smaller" title="Smaller text">A&minus;</button>'
        '<button type="button" class="ctl-btn" id="reader-bigger" title="Larger text">A+</button>'
        '<button type="button" class="ctl-btn" id="reader-notes" aria-pressed="true">Notes on</button>'
        "</div>"
    )
    return f'<div class="readerbar">{"".join(bits)}</div>'


def _section_nav_html(sections: list, match_index: int, base_url: str,
                      filenames_by_eid: dict[str, str], index_link_text: str) -> str:
    """The provisions either side of this one, named. "Next" alone makes a
    reader click to find out where they are going; "15 Review of a limit"
    lets them decide not to."""

    def link(offset: int, arrow_before: str, arrow_after: str, css: str) -> str:
        index = match_index + offset
        if not 0 <= index < len(sections):
            return ""
        tree_node = sections[index][0]
        label = index_label(tree_node["node"])
        href = f"{base_url}/section/{_strip_md(filenames_by_eid[tree_node['eid']])}"
        return f'<a class="{css}" href="{href}">{arrow_before}{_esc(label)}{arrow_after}</a>'

    return (
        '<nav class="section-nav" aria-label="Nearby provisions">'
        f'{link(-1, "&larr; ", "", "nav-prev")}'
        f'<a class="nav-up" href="{base_url}/">{_esc(index_link_text)}</a>'
        f'{link(1, "", " &rarr;", "nav-next")}'
        "</nav>"
    )


def render_section(
    parsed: dict, act_title: str, base_url: str, section_slug: str,
    crossrefs: list[dict] | None = None, amendment_index: dict | None = None,
    timeline: list[dict] | None = None, version_urls: dict | None = None,
    superseded: dict | None = None, version_dates: dict | None = None,
    unpublished_pages: "set[str] | None" = None, show_review_badge: bool = True,
) -> str | None:
    """Renders the Section whose assign_filenames-computed id matches
    section_slug (the same string render_index links to), or None if no
    Section matches -- the caller (dashboard.py) turns that into a 404.

    crossrefs, if given, are the related-document chips described in
    _crossrefs_html; amendment_index, if given, is what lets each
    margin note name the Act behind its citation (see
    _margin_notes_html).

    timeline, if given, is this provision's own entries from
    ai_pipeline/diffing.build_timeline -- how its wording has moved
    across the versions of the Act held here -- with version_urls
    mapping a version number to that version's page for this same
    provision. superseded, if given, is {"version", "current",
    "current_url", "as_at_printed"} for the banner saying this reprint
    is no longer the law. version_dates, if given, is {version -> the
    date that version states it incorporates amendments to}, which is
    what the "Compare with another version" choices are labelled with.

    parsed["version"], if present, is this reprint's own front matter --
    what the "Text as at" line states.

    unpublished_pages is the page ids whose provision hasn't been released
    to readers yet, marked in the outline the same way render_index marks
    them in the contents.

    show_review_badge is how much of this provision a human has checked --
    on for the dashboard, whose job is tracking that, and off on a site
    that only publishes checked provisions, where the badge would read
    "Fully reviewed" on every page and so say nothing (see render_index,
    which turns it off for the same reason)."""
    ctx = _build_context(parsed, act_title)
    sections = ctx["sections"]
    filenames_by_eid = ctx["filenames_by_eid"]

    target_filename = f"{section_slug}.md"
    match_index = next((i for i, (tn, _b) in enumerate(sections) if filenames_by_eid[tn["eid"]] == target_filename), None)
    if match_index is None:
        return None
    tree_node, breadcrumb = sections[match_index]
    node = tree_node["node"]

    linkify = _build_linkifier_html(ctx["section_files"], ctx["part_eids"], ctx["division_eids"], ctx["definitions"], base_url, ctx["secref_re"], own_title=act_title)
    title = page_title(node)
    verification = _collect_verification([tree_node])

    # The page's own chrome, outside the text column: the reading controls
    # above, the outline of the rest of the document beside. Both are
    # built from what this page already knows, so neither needs the caller
    # to pass anything new.
    out = [
        _readerbar_html(parsed.get("version") or {}, superseded, version_urls, version_dates),
        '<div class="reader-cols">',
        _outline_html(ctx, act_title, breadcrumb, target_filename, base_url,
                      bool(parsed.get("endnotes")), unpublished_pages),
        '<div class="reader-main">',
    ]
    crumb_bits = [f'<a href="{base_url}/">{_esc(ctx["index_link_text"])}</a>']
    crumb_bits.extend(_esc(_display_title(b["node"]["type"], b["node"].get("number"), b["node"].get("heading"))) for b in breadcrumb)
    out.append(f'<div class="breadcrumb">{" &raquo; ".join(crumb_bits)}</div>')
    if show_review_badge:
        out.append(_verification_badge(verification))
    out.append(f"<h1>{_esc(title)}</h1>")
    # Ordered the way a reader needs them: whether this is even the
    # current law first, then how this provision got to its present
    # wording, then where else it's explained. A crossref chip is no
    # use to someone reading the wrong reprint.
    if superseded:
        out.append(render_superseded_banner(
            superseded.get("version"), superseded.get("current"),
            superseded.get("current_url"), superseded.get("as_at_printed"),
        ))
    out.append(render_timeline(timeline or [], base_url, amendment_index, version_urls))
    out.append(_crossrefs_html(crossrefs or []))

    # The body reads the way the Act itself does: each provision
    # indented by its own nesting depth with its number hanging in the
    # left margin, rather than every subsection or paragraph becoming
    # its own <h4>/<h5> heading the way the Markdown export has to
    # (Markdown has no indentation of its own to carry structure with).
    # The anchors those headings used to provide are kept -- they're
    # what cross-references from other sections link into (see
    # _build_linkifier_html's `fragment`) -- just moved onto the
    # provision <div> itself.
    slugs = compute_section_slugs(tree_node)
    # Copying a provision into advice, a submission or an email is one of
    # the things people most often come here to do, so it's a button
    # rather than a careful drag-select that picks up the margin notes
    # and loses the indentation (see static/site/copy.js).
    out.append(
        '<button type="button" class="copy-section" id="copy-section-btn">Copy section</button>'
    )
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
        elif unit["header_text"] is None:
            # Body text with no number of its own -- a section's lead-
            # in, a note, a paragraph the parser couldn't number.
            # Nothing to hang in the margin, so it just sits in the
            # text column.
            classes.append("prov-nolabel")

        bits = []
        if unit["header_text"] is not None:
            # A defined term is set in bold italics where it's
            # introduced (the drafting convention -- see
            # rule_parser.py's _try_definition_start); every other
            # label is just the provision's own number, hanging left
            # of its text.
            label_class = "prov-term" if unit_node["type"] == "definition" else "prov-num"
            bits.append(f'<span class="{label_class}">{_esc(unit["header_text"])}</span>')
        if unit["text"] is not None:
            # A number's gutter is CSS (.prov-num's own width), but a
            # defined term runs straight on into its text, so it needs
            # a real space -- except where that text opens with
            # punctuation ("appear, in relation to a party, ..."),
            # which must sit tight against it.
            if bits and unit_node["type"] == "definition" and not unit["text"].lstrip().startswith((",", ".", ";", ":", ")", "\u2014", "-")):
                bits.append(" ")
            bits.append(linkify(_esc(unit["text"]), target_filename, slug))

        # bits are joined with no separator on purpose: the gutter
        # between a provision's number and its text is the label
        # span's own width and padding (see .prov-num), so an extra
        # space here would push the first line out of alignment with
        # the wrapped ones below it.
        out.append(f'<div class="{" ".join(classes)}"{id_attr} style="--depth:{unit["depth"]}">{"".join(bits)}</div>')
        # One margin cell per provision, empty or not: the two columns
        # are auto-placed rows of the same grid, so a note only stays
        # level with the provision it belongs to if every provision
        # contributes a cell.
        notes = _margin_notes_html(unit_node, base_url, amendment_index) if unit["clause_index"] == 0 else ""
        out.append(f'<div class="prov-notes">{notes}</div>')
    out.append("</div>")

    out.append(_section_nav_html(sections, match_index, base_url, filenames_by_eid, ctx["index_link_text"]))
    out.append("</div>")   # .reader-main
    out.append("</div>")   # .reader-cols

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Endnotes
# ---------------------------------------------------------------------------
# How many of an amending Act's own provision changes to list before
# the rest go behind a count. The Criminal Procedure Act's busiest
# amending Act touched 228 provisions; printing all of them for all 73
# amending Acts would make this page longer than several of the Act's
# own Parts.
_AMENDMENT_PROVISION_LIMIT = 40


def _amending_act_html(entry: dict, section_files: dict[str, str], base_url: str) -> str:
    record = entry["record"]
    rows = []
    for label, keys in (
        ("Assent", ("assent_date",)),
        ("Made", ("date_of_making",)),
        ("Commencement", ("commencement_date", "date_of_commencement")),
        ("Note", ("note",)),
        ("Current state", ("current_state",)),
    ):
        value = next((record[k] for k in keys if record.get(k)), None)
        if value:
            rows.append(f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>")
    if record.get("source") == "registry":
        # Named from the general Act registry rather than this Act's
        # own Table of Amendments, so it carries no assent or
        # commencement date -- say why, instead of showing an entry
        # that just looks incomplete.
        rows.append("<dt>Source</dt><dd>Named from the Act registry &mdash; not listed in this Act's own Table of Amendments</dd>")

    provisions = entry["provisions"]
    listed = provisions[:_AMENDMENT_PROVISION_LIMIT]
    chips = []
    for provision in listed:
        filename = section_files.get(str(provision["section_number"] or "").lower())
        label = _esc(provision["label"])
        chips.append(
            f'<a class="amend-prov" href="{base_url}/section/{_strip_md(filename)}">{label}</a>'
            if filename else f'<span class="amend-prov">{label}</span>'
        )
    more = f' <span class="amend-more">and {len(provisions) - len(listed)} more</span>' if len(provisions) > len(listed) else ""
    touched = (
        f'<details class="amend-provisions"><summary>{len(provisions)} provision(s) in this Act</summary>'
        f'<div class="amend-prov-list">{"".join(chips)}{more}</div></details>'
        if provisions else ""
    )
    cite = f'<span class="amend-cite">No. {_esc(record.get("citation") or "?")}</span>' if record.get("citation") else ""
    return (
        f'<div class="amend" id="{_esc(anchor_id(record.get("citation")))}">'
        f'<div class="amend-head">{cite}{_esc(record.get("title") or "")}</div>'
        f'<dl class="amend-fields">{"".join(rows)}</dl>{touched}</div>'
    )


def _endnote_blocks_html(section: dict) -> str:
    """One endnote section's prose, as the printed page sets it:
    paragraphs reflowed out of the PDF's own line wraps, its sub-
    headings as headings, its bulleted list as a list, and any
    provision it quotes set apart from the commentary around it (see
    ai_pipeline/endnotes.py's block builder).

    Falls back to the flat "text" field for an Act parsed before
    blocks existed -- that text still carries the source's wrap points,
    so it keeps the pre-line rendering that at least preserves its
    line breaks rather than running them all together."""
    blocks = section.get("blocks")
    if not blocks:
        text = section.get("text")
        return f'<div class="endnote-text endnote-raw">{_esc(text)}</div>' if text else ""
    # A run of bullets is one list, and a run of quoted lines is one
    # quotation -- the printed page sets a reproduced provision as a
    # single indented block, not a stack of unrelated paragraphs.
    wrappers = {"bullet": ('<ul class="endnote-bullets">', "</ul>"),
                "quote": ('<blockquote class="endnote-quote">', "</blockquote>")}
    out = ['<div class="endnote-text">']
    run = None
    for block in blocks:
        kind = block.get("kind")
        if run and run != kind:
            out.append(wrappers[run][1])
            run = None
        if kind in wrappers and not run:
            out.append(wrappers[kind][0])
            run = kind
        body = _esc(block.get("text") or "")
        if kind == "bullet":
            out.append(f"<li>{body}</li>")
        elif kind == "quote":
            out.append(f"<p>{body}</p>")
        elif kind == "heading":
            out.append(f'<div class="endnote-heading">{body}</div>')
        else:
            out.append(f"<p>{body}</p>")
    if run:
        out.append(wrappers[run][1])
    out.append("</div>")
    return "".join(out)


def render_endnotes(parsed: dict, act_title: str, base_url: str, summary: list[dict] | None = None) -> str | None:
    """The Act's own Endnotes, read the way the printed page reads them
    rather than as the wall of text they extract to: General
    information, the Table of Amendments as an actual table, and
    Explanatory details.

    `summary` is ai_pipeline/amendments.summarise_by_act's output --
    every amending Act this Act's margin notes actually cite, with the
    provisions each one touched. That's the Table of Amendments read
    the other way round, which is the question a reader actually has;
    the Acts in the table that no margin note cites are listed after
    it, unchanged.

    Returns None when this document has no endnotes (a Bill, an
    Explanatory Memorandum, an Act parsed before this was extracted),
    which the caller turns into a 404."""
    endnotes = parsed.get("endnotes")
    if not endnotes:
        return None
    ctx = _build_context(parsed, act_title)
    section_files = ctx["section_files"]
    summary = summary or []
    cited = {entry["citation"] for entry in summary}

    out = [
        f'<div class="breadcrumb"><a href="{base_url}/">{_esc(ctx["index_link_text"])}</a> &raquo; Endnotes</div>',
        "<h1>Endnotes</h1>",
    ]
    for section in endnotes.get("sections") or []:
        out.append(f'<h2 id="endnote-{_esc(section["number"])}">{_esc(section["number"])} {_esc(section["heading"])}</h2>')
        out.append(_endnote_blocks_html(section))
        if "table of amendments" not in (section["heading"] or "").lower():
            continue
        if summary:
            out.append('<h3>Acts that amended provisions of this Act</h3>')
            out.extend(_amending_act_html(entry, section_files, base_url) for entry in summary)
        uncited = [
            {"record": record, "provisions": []}
            for record in endnotes.get("amending_acts") or []
            if record.get("citation") not in cited
        ]
        if uncited:
            out.append(
                f'<h3>Also in the Table of Amendments ({len(uncited)})</h3>'
                '<p class="endnote-aside">Listed in the printed table, but not cited by any margin note in this '
                "Act &mdash; typically an amendment to a provision that has since been repealed.</p>"
            )
            out.extend(_amending_act_html(entry, section_files, base_url) for entry in uncited)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Hover previews
# ---------------------------------------------------------------------------
# How many provisions (and how much text) a preview card is allowed
# before it stops being a glance and turns into the page it's
# previewing. A card that hits either limit says so and offers the
# link instead.
_PREVIEW_MAX_UNITS = 6
_PREVIEW_MAX_CHARS = 650


def _preview_prov_html(unit: dict, base_depth: int) -> str:
    """One provision, in the same shape render_section emits -- minus
    the anchor id and the linkifier. A preview is a glance at where a
    link goes, not a second page: links inside one would invite
    previews of previews, and its ids would collide with the real
    page's own."""
    node = unit["tree_node"]["node"]
    classes = ["prov", f"prov-{_esc(node['type'])}"]
    if unit["text"] is None:
        classes.append("prov-heading")
    elif unit["header_text"] is None:
        classes.append("prov-nolabel")
    bits = []
    if unit["header_text"] is not None:
        label_class = "prov-term" if node["type"] == "definition" else "prov-num"
        bits.append(f'<span class="{label_class}">{_esc(unit["header_text"])}</span>')
    if unit["text"] is not None:
        if node["type"] == "definition" and bits and not unit["text"].lstrip().startswith((",", ".", ";", ":", ")", "\u2014", "-")):
            bits.append(" ")
        bits.append(_esc(unit["text"]))
    depth = max(unit["depth"] - base_depth, 0)
    return f'<div class="{" ".join(classes)}" style="--depth:{depth}">{"".join(bits)}</div>'


def render_preview(parsed: dict, act_title: str, section_slug: "str | None", fragment: "str | None") -> "dict | None":
    """The content behind one link, small enough to read in a hover
    card.

    Three shapes, matching the three kinds of link
    _build_linkifier_html produces: a defined term or a numbered
    provision (section_slug plus the fragment its anchor uses -- the
    provision itself and whatever nests under it), a bare "section N"
    reference (section_slug alone -- the Section's opening provisions),
    and a "Part N"/"Division N" reference (fragment alone -- that
    heading and the Sections beneath it).

    Returns {title, subtitle, html, truncated}, or None when the
    target doesn't resolve -- the caller turns that into a 404 and the
    hover card simply doesn't appear, which is the right outcome for a
    link into something that isn't there.
    """
    ctx = _build_context(parsed, act_title)

    if section_slug:
        sections = ctx["sections"]
        filenames_by_eid = ctx["filenames_by_eid"]
        target_filename = f"{section_slug}.md"
        match = next(((tn, b) for tn, b in sections if filenames_by_eid[tn["eid"]] == target_filename), None)
        if match is None:
            return None
        tree_node, breadcrumb = match
        node = tree_node["node"]
        title = page_title(node)
        subtitle = " » ".join(
            _display_title(b["node"]["type"], b["node"].get("number"), b["node"].get("heading")) for b in breadcrumb
        )

        units = list(_iter_body_units(tree_node))
        if fragment:
            slugs = compute_section_slugs(tree_node)
            start = next(
                (
                    i for i, u in enumerate(units)
                    if slugs.get((u["tree_node"]["eid"], u["clause_index"])) == fragment
                ),
                None,
            )
            if start is None:
                return None
            # The provision itself plus everything nested under it,
            # which is what makes a definition useful at a glance: a
            # term whose meaning is a list of (a)/(b) paragraphs is
            # only actually answered by showing them too.
            base_depth = units[start]["depth"]
            selected = [units[start]]
            for u in units[start + 1 :]:
                if u["depth"] <= base_depth:
                    break
                selected.append(u)
            if units[start]["header_text"]:
                title = units[start]["header_text"]
                subtitle = page_title(node)
        else:
            base_depth = 0
            selected = units

        truncated = len(selected) > _PREVIEW_MAX_UNITS
        selected = selected[:_PREVIEW_MAX_UNITS]
        html_bits, chars = [], 0
        for unit in selected:
            if chars >= _PREVIEW_MAX_CHARS:
                truncated = True
                break
            html_bits.append(_preview_prov_html(unit, base_depth))
            chars += len(unit["text"] or unit["header_text"] or "")
        return {"document": act_title, "title": title, "subtitle": subtitle, "html": "".join(html_bits), "truncated": truncated}

    if not fragment:
        return None

    # An index anchor: a Part, Division or Chapter heading. What's
    # useful here isn't its text (it has none) but what it contains, so
    # the card lists the Sections under it.
    index_slugs = ctx["index_slugs"]
    target = None
    for root in ctx["tree_roots"]:
        for tn in _iter_tree(root):
            if index_slugs.get(tn["eid"]) == fragment:
                target = tn
                break
        if target is not None:
            break
    if target is None:
        return None

    node = target["node"]
    listed = [
        tn["node"] for tn in _iter_tree(target)
        if tn["node"]["type"] == "section" and tn is not target
    ]
    truncated = len(listed) > _PREVIEW_MAX_UNITS
    rows = "".join(
        f'<div class="prov" style="--depth:0">'
        f'<span class="prov-num">{_esc(sec["number"])}</span>{_esc(sec.get("heading") or "")}</div>'
        for sec in listed[:_PREVIEW_MAX_UNITS]
    )
    return {
        "document": act_title,
        "title": _display_title(node["type"], node.get("number"), node.get("heading")),
        "subtitle": f"{len(listed)} section(s)" if listed else "",
        "html": rows,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# The page template
# ---------------------------------------------------------------------------
# The shell, the stylesheets and the browser-side scripts live in
# static/site/ as ordinary .html/.css/.js files rather than as string
# constants here. That is the point: they are the site's template, edited
# far more often than this module's rendering logic is, and a stylesheet
# is much easier to work on when an editor can highlight it, DevTools can
# name it, and the browser can cache it.
#
# Python only ever reads page.html (see page_shell). The CSS and JS are
# linked, not inlined, so they never pass through here at all -- which
# also means a reload picks up an edit to them without restarting the
# server. TEMPLATE_DIR is served as "/assets": by dashboard.py and
# review.py for the live browse pages, and copied into the build by
# export_static_site.py for the published site.
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "static" / "site"

_template_cache: dict[str, tuple[float, str]] = {}


def template_text(name: str) -> str:
    """One file from static/site/, re-read whenever it changes on disk.

    The mtime check costs one stat per page render and buys editing the
    template while a server is running, which is worth more than the
    stat -- and in a static build every page is rendered in one process
    anyway, so the read happens once."""
    path = TEMPLATE_DIR / name
    mtime = path.stat().st_mtime
    cached = _template_cache.get(name)
    if cached is None or cached[0] != mtime:
        _template_cache[name] = (mtime, path.read_text(encoding="utf-8"))
    return _template_cache[name][1]


_TEMPLATE_COMMENT_RE = re.compile(r"<!--.*?-->\n?", re.S)


def page_shell(title: str, body_html: str, previewbar_html: str = "",
               base_url: str | None = None, reader: bool = False,
               preview_source: str = "api", site_salt: str | None = None,
               site_prefix: str | None = None) -> str:
    """One page, built into static/site/page.html -- see that file for
    what each placeholder is.

    base_url is this document's own root (e.g. "/browse/crimes-act").
    Given, the page also gets link hover previews and the reading
    controls, both of which need it to tell a link into this document
    apart from any other href on the page. It is also what the asset and
    link prefixes derive from, so a page built for a GitHub Pages project
    site keeps its links inside that site.

    reader lays the page out as a section: an outline column beside the
    text, rather than one centred column (see static/site/reader.css).

    preview_source is where the hover cards get their content: "api" for
    a server that can render one on demand (the dashboard), or "static"
    for pre-built preview.json files beside each page (the published
    site, which has no server to ask). site_salt, on a gated build, is
    how preview.js finds the key the unlock page derived -- the preview
    data is encrypted with it like everything else.

    site_prefix is what the asset URLs and the Home link are built from
    for a page that has no base_url to derive them from -- the published
    site's own landing page, which belongs to no document. Without it that
    page reaches for the real domain root, which on a GitHub Pages project
    site is somebody else's."""
    body_attrs = ""
    if base_url:
        body_attrs = f' data-base-url="{_esc(base_url)}" data-preview="{_esc(preview_source)}"'
        if site_salt:
            body_attrs += f' data-site-salt="{_esc(site_salt)}"'
    prefix = _site_prefix(base_url or "") if site_prefix is None else site_prefix
    replacements = {
        "{{TITLE}}": _esc(title),
        "{{ASSETS}}": _esc(f"{prefix}/assets"),
        "{{HOME}}": _esc(f"{prefix}/"),
        "{{BODY_ATTRS}}": body_attrs,
        "{{MAIN_CLASS}}": "page page-reader" if reader else "page",
        "{{PREVIEWBAR}}": previewbar_html,
        # Last, so a stray "{{...}}" inside the page's own text -- a
        # provision quoting a template, say -- is never substituted.
        "{{BODY}}": body_html,
    }
    # Comments are stripped from the template, and only from the template:
    # they are notes to whoever edits page.html, and shipping them on
    # every page of a public register of the law would be neither useful
    # to a reader nor anything to make an editor think twice about writing.
    # Done before substitution, so a comment in the page's own body (or in
    # a provision quoting one) is left exactly as it was.
    page = _TEMPLATE_COMMENT_RE.sub("", template_text("page.html"))
    for placeholder, value in replacements.items():
        page = page.replace(placeholder, value)
    return page
