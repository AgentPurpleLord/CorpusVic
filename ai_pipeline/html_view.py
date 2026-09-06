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
builds those cards, and PREVIEW_SCRIPT is the browser side of it.

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
                return f'<a href="/browse/{known[0]}/">{text}</a>'
            registry = registry_by_lower.get(key)
            if registry:
                _title, entry = registry
                href = _legislation_href(entry)
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


def _legislation_href(citation: dict) -> str:
    """The standing address for a citation this pipeline detected but
    doesn't (yet) know how to name -- /legislation/<act_no>[-<year>]
    (see dashboard.py's legislation_resolver), which redirects to that
    Act's own parse once one exists, and otherwise says plainly that it
    hasn't been parsed yet. Every citation this pipeline notices becomes
    a link to *something*; this is the standing "something" for one
    that resolved to nothing more specific."""
    act_no = citation.get("act_no")
    year = citation.get("year")
    return f"/legislation/{act_no}-{year}" if year else f"/legislation/{act_no}"


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
        href = _legislation_href(citation)
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
                 superseded: dict | None = None) -> str:
    """base_url is this Act's own root, e.g. "/browse/crimes-act" (no
    trailing slash) -- every link rendered here and in render_section
    is built from it, so the caller controls the URL scheme entirely.

    superseded, if given, is {"version", "current", "current_url",
    "as_at_printed"} -- see render_superseded_banner."""
    ctx = _build_context(parsed, act_title)
    tree_roots = ctx["tree_roots"]
    structural_types = ctx["structural_types"]
    filenames_by_eid = ctx["filenames_by_eid"]
    index_slugs = ctx["index_slugs"]
    verification = _collect_verification(tree_roots)

    out = [f"<h1>{_esc(act_title)}</h1>", _verification_badge(verification)]
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


def render_section(
    parsed: dict, act_title: str, base_url: str, section_slug: str,
    crossrefs: list[dict] | None = None, amendment_index: dict | None = None,
    timeline: list[dict] | None = None, version_urls: dict | None = None,
    superseded: dict | None = None,
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
    is no longer the law."""
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

    out = []
    crumb_bits = [f'<a href="{base_url}/">{_esc(ctx["index_link_text"])}</a>']
    crumb_bits.extend(_esc(_display_title(b["node"]["type"], b["node"].get("number"), b["node"].get("heading"))) for b in breadcrumb)
    out.append(f'<div class="breadcrumb">{" &raquo; ".join(crumb_bits)}</div>')
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

    nav = []
    if match_index > 0:
        prev_filename = filenames_by_eid[sections[match_index - 1][0]["eid"]]
        nav.append(f'<a href="{base_url}/section/{_strip_md(prev_filename)}">&laquo; Previous</a>')
    nav.append(f'<a href="{base_url}/">{_esc(ctx["index_link_text"])}</a>')
    if match_index + 1 < len(sections):
        next_filename = filenames_by_eid[sections[match_index + 1][0]["eid"]]
        nav.append(f'<a href="{base_url}/section/{_strip_md(next_filename)}">Next &raquo;</a>')
    out.append(f'<div class="section-nav">{" | ".join(nav)}</div>')

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
            # The provision itself plus everything nested under it, which is
            # what makes a definition useful at a glance: a term whose
            # meaning is a list of (a)/(b) paragraphs is only answered by
            # showing them too.
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

    # An index anchor: a Part/Division/Chapter heading. What's useful here
    # isn't its text (it has none) but what it contains, so the card lists
    # the Sections under it.
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


# Inter over the previous Georgia/system-sans mix, everywhere in the GUI --
# a typeface drawn for screens, not print, at the small sizes a margin
# note or a badge is set in. Loaded once per page from Google Fonts
# (static/dashboard.html and static/review.html load it the same way);
# the fallback stack still applies if that request fails.
_FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
)

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
  --ins-bg: #dcfce7; --ins-fg: #14532d; --del-bg: #fee2e2; --del-fg: #7f1d1d;
  --warn-bg: #fef3c7; --warn-border: #d97706;
  --sans: 'Inter', ui-sans-serif, system-ui, sans-serif;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #16181d; --panel: #1e2126; --fg: #e8e8ea; --muted: #9aa1ab;
  --border: #34383f; --accent: #5b9bd9;
  --done: #34d17f; --pending: #8b93a0; --flagged: #f0ad4e;
  --verify-full-bg: #132a1c; --verify-partial-bg: #2c2410; --verify-none-bg: #262a31;
  --bar-bg: #05070a; --bar-fg: #b6bcc6; --bar-link: #7fb6ea;
  --ins-bg: #14321f; --ins-fg: #86efac; --del-bg: #3a1616; --del-fg: #fca5a5;
  --warn-bg: #2c2410; --warn-border: #f0ad4e;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font-family: var(--sans); line-height: 1.65; }
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
/* No number to hang, so no hanging indent -- the text just starts where a
   numbered sibling's text does, rather than its first line poking out
   into the empty number column. */
.prov-nolabel { text-indent: 0; }
/* Except a list item, which has no number because its source prints a
   bullet instead of one (an Explanatory Memorandum's lists are set that
   way -- see em_parser.py). It gets its marker back, hanging in the same
   column a lettered sibling's "(a)" would. */
.prov-paragraph.prov-nolabel,
.prov-subparagraph.prov-nolabel,
.prov-sub_subparagraph.prov-nolabel { text-indent: -2.4em; }
.prov-paragraph.prov-nolabel::before,
.prov-subparagraph.prov-nolabel::before,
.prov-sub_subparagraph.prov-nolabel::before {
  content: "•";
  display: inline-block; min-width: 1.9em; padding-right: 0.5em; color: var(--muted);
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

/* Endnotes page: the Table of Amendments as a table. */
.index-nav { font-family: var(--sans); font-size: 12.5px; color: var(--muted); margin: -8px 0 18px; }
/* Which version this pipeline's own parse of the Act is, stated plainly
   under its title -- the first thing a reader needs to know, and never
   claimed as "the Authorised Version" itself. */
.act-version { font-family: var(--sans); font-size: 12.5px; color: var(--muted); margin: -6px 0 14px; }
.endnote-text { margin-bottom: 18px; }
.endnote-text p { margin: 0 0 11px; }
/* Only an Act parsed before the endnote block builder existed falls back
   to this: its text still carries the source PDF's own wrap points, so
   honouring them beats running every line together. */
.endnote-raw { white-space: pre-line; }
.endnote-heading { font-family: var(--sans); font-weight: 600; font-size: 13.5px; margin: 18px 0 7px; }
.endnote-bullets { margin: 0 0 11px; padding-left: 20px; }
.endnote-bullets li { margin-bottom: 9px; }
/* A provision the endnotes reproduce verbatim -- the Act's own words, not
   the endnote's commentary about them. */
.endnote-quote {
  margin: 0 0 11px; padding: 2px 0 2px 14px;
  border-left: 3px solid var(--border); color: var(--fg);
}
/* The first line of a reproduced provision is its own heading ("64 How
   appeal is commenced"), the way the printed page sets it. */
.endnote-quote p:first-child { font-weight: 600; }
.endnote-quote p:last-child { margin-bottom: 0; }
.endnote-aside { font-family: var(--sans); font-size: 12.5px; color: var(--muted); }
.amend { border-top: 1px solid var(--border); padding: 12px 0 4px; }
.amend-head { font-family: var(--sans); font-weight: 600; font-size: 14px; margin-bottom: 6px; }
.amend-cite {
  display: inline-block; font-weight: 500; font-size: 11.5px; color: var(--muted);
  border: 1px solid var(--border); border-radius: 10px; padding: 1px 8px; margin-right: 8px;
}
.amend-fields { display: grid; grid-template-columns: 130px minmax(0, 1fr); gap: 2px 14px; margin: 0 0 8px; font-family: var(--sans); font-size: 12.5px; }
.amend-fields dt { color: var(--muted); }
.amend-fields dd { margin: 0; }
.amend-provisions { font-family: var(--sans); font-size: 12.5px; }
.amend-provisions summary { cursor: pointer; color: var(--accent); }
.amend-prov-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.amend-prov { font-size: 11.5px; border: 1px solid var(--border); border-radius: 4px; padding: 1px 6px; color: var(--fg); }
a.amend-prov:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
.amend-more { font-size: 11.5px; color: var(--muted); align-self: center; }
/* The citation inside a margin note, linked to that Act's own entry in the
   Endnotes. Inline, so the note still reads as the one line the source
   prints; the Act's full name and dates are in the link's title. */
.hist-act { color: var(--accent); text-decoration: none; border-bottom: 1px dotted currentColor; }
.hist-act:hover { text-decoration: none; border-bottom-style: solid; }
/* A citation detected but not resolved to anything more specific -- see
   html_view._linked_citation_html. Muted rather than accent-coloured, so
   it doesn't read as confidently as a citation this pipeline actually
   knows the name of. */
.hist-act.unresolved { color: var(--muted); border-bottom-style: dashed; }
.hist-act.unresolved:hover { color: var(--accent); }

/* "Explained in" chips under a Section's title: the Bill clause it was
   enacted from, and the Explanatory Memorandum's note on it. Ordinary
   links, so the hover preview above reads them like any other -- which is
   the whole point, since the question ("what does the EM say about this?")
   is one a reader wants answered without leaving the section. */
.crossrefs { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; margin: -4px 0 20px; font-family: var(--sans); }
.crossrefs-label { font-size: 11.5px; color: var(--muted); }
.crossref {
  font-size: 11.5px; padding: 2px 9px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--panel); color: var(--fg);
}
.crossref:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
.crossref-em { border-style: dashed; }

/* A provision's timeline -- see render_timeline. Collapsed by default:
   the summary answers "has this changed, and when last?" without opening,
   which is the question most readers actually have, and the words that
   moved are one click away for the ones who want them. */
.timeline { margin: 0 0 20px; font-family: var(--sans); }
.timeline-summary {
  cursor: pointer; font-size: 12.5px; padding: 6px 10px;
  border: 1px solid var(--border); border-left: 3px solid var(--flagged);
  border-radius: 4px; background: var(--panel); color: var(--fg);
}
.timeline-summary:hover { border-color: var(--accent); }
.timeline[open] .timeline-summary { border-radius: 4px 4px 0 0; }
.tl-count { color: var(--muted); }
.tl-list { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-top: none; }
.tl-entry { padding: 10px 12px; border-top: 1px solid var(--border); }
.tl-entry:first-child { border-top: none; }
.tl-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin-bottom: 5px; }
.tl-version { font-size: 12.5px; font-weight: 600; }
.tl-verb {
  font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.04em;
  padding: 1px 7px; border-radius: 9px; background: var(--verify-none-bg); color: var(--muted);
}
.tl-inserted .tl-verb { background: var(--ins-bg); color: var(--ins-fg); }
.tl-repealed .tl-verb { background: var(--del-bg); color: var(--del-fg); }
.tl-notes { font-size: 11.5px; color: var(--muted); margin-bottom: 6px; }
.tl-note { display: block; }
.tl-diff {
  font-family: var(--sans); font-size: 13.5px; line-height: 1.6;
  max-height: 20em; overflow-y: auto;
}
.d-ins { background: var(--ins-bg); color: var(--ins-fg); text-decoration: none; padding: 0 2px; border-radius: 2px; }
.d-del { background: var(--del-bg); color: var(--del-fg); padding: 0 2px; border-radius: 2px; }

/* The notice on a reprint that is no longer the law. Deliberately loud
   and at the top of the page: a reader on a superseded version is reading
   the wrong law, and that is worth interrupting them for. */
.supersede {
  font-family: var(--sans); font-size: 12.5px; line-height: 1.5;
  background: var(--warn-bg); border: 1px solid var(--warn-border); border-left-width: 3px;
  border-radius: 4px; padding: 8px 12px; margin: 0 0 16px;
}
.supersede-link { white-space: nowrap; }


/* Hover preview card -- see PREVIEW_SCRIPT. Positioned in page
   coordinates (not fixed) so it scrolls with the link it belongs to. */
.linkpeek {
  position: absolute; z-index: 40; display: none;
  width: min(420px, 90vw); max-height: 340px; overflow-y: auto;
  background: var(--panel); color: var(--fg);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px;
  box-shadow: 0 10px 34px rgba(0, 0, 0, 0.22);
  font-size: 13.5px; line-height: 1.55;
}
.linkpeek.open { display: block; }
.linkpeek .peek-title { font-family: var(--sans); font-weight: 600; font-size: 13px; margin-bottom: 2px; }
.linkpeek .peek-sub { font-family: var(--sans); font-size: 11.5px; color: var(--muted); margin-bottom: 9px; }
.linkpeek .prov { margin-bottom: 7px; padding-left: calc(var(--depth, 0) * 16px + 2.2em); text-indent: -2.2em; }
.linkpeek .prov-nolabel { text-indent: 0; }
.linkpeek .prov:last-child { margin-bottom: 0; }
.linkpeek .prov-num { min-width: 1.7em; padding-right: 0.5em; }
.linkpeek .peek-more {
  position: sticky; bottom: -12px;   /* cancels the card's own bottom padding */
  background: var(--panel);
  font-family: var(--sans); font-size: 11.5px; color: var(--muted);
  margin-top: 9px; padding: 7px 0 12px; border-top: 1px solid var(--border);
}
.linkpeek .peek-loading { font-family: var(--sans); font-size: 12px; color: var(--muted); }

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


# Hover previews. Every link on a browse page points either at a Section
# page (optionally with a provision's anchor) or at an index anchor, so the
# href alone says what to preview -- no data has to be embedded in the page.
# Deliberately hover-with-a-delay rather than click: the point is checking
# what a defined term means without losing your place, and a card that
# appeared instantly would flash open every time the pointer crossed a
# link mid-sentence. It also opens on keyboard focus, where there's no
# accidental-hover problem to guard against, so a card is reachable
# without a pointer.
PREVIEW_SCRIPT = r"""
(function () {
  var BASE = document.body.dataset.baseUrl;
  if (!BASE) return;
  // Everything above this document's own slug, e.g. "/browse". Previews
  // work for any document under it, not just this one -- a Section's
  // "Explained in" chips point at the Bill and its Explanatory
  // Memorandum, and those are exactly the links most worth previewing.
  var ROOT = BASE.slice(0, BASE.lastIndexOf("/"));
  var OPEN_DELAY = 500;   // long enough that skimming past a link doesn't trigger one
  var CLOSE_DELAY = 220;  // long enough to move the pointer from the link into the card
  var card = document.createElement("div");
  card.className = "linkpeek";
  document.body.appendChild(card);

  var cache = {};
  var openTimer = null, closeTimer = null, activeLink = null, requestSeq = 0;

  // Which link target this is, as the preview endpoint's two parameters.
  // Anything that isn't a link into this Act (the preview bar's own links,
  // an external href) returns null and is left alone.
  function targetOf(a) {
    var url;
    try { url = new URL(a.getAttribute("href"), location.href); } catch (e) { return null; }
    if (url.origin !== location.origin) return null;
    var fragment = decodeURIComponent(url.hash.replace(/^#/, ""));
    if (url.pathname.slice(0, ROOT.length + 1) !== ROOT + "/") return null;
    var parts = url.pathname.slice(ROOT.length + 1).replace(/\/$/, "").split("/");
    if (parts.length === 3 && parts[1] === "section") {
      return { base: ROOT + "/" + parts[0], section: parts[2], fragment: fragment };
    }
    if (parts.length === 1 && parts[0] && fragment) {
      return { base: ROOT + "/" + parts[0], section: "", fragment: fragment };
    }
    return null;
  }

  function render(data, crossDocument) {
    card.classList.add("open");  // must be laid out before the overflow check below can measure it
    var more = data.truncated
      ? '<div class="peek-more">Continues &mdash; open the link to read the rest.</div>' : "";
    // A link into another document (a Bill clause, an EM note) is named
    // by that document as well as by the provision -- without it a card
    // reading "Clause 5" gives no clue which of the three it came from.
    var subBits = [];
    if (crossDocument && data.document) subBits.push(data.document);
    if (data.subtitle) subBits.push(data.subtitle);
    var sub = subBits.length ? '<div class="peek-sub">' + escapeText(subBits.join(" \u00b7 ")) + "</div>" : "";
    card.innerHTML = '<div class="peek-title">' + escapeText(data.title) + "</div>" + sub + data.html + more;
    // A card can also overflow without the server having truncated
    // anything -- short provisions that simply wrap past its height. Say so
    // there too, so a clipped last line always reads as "there's more",
    // never as a rendering glitch.
    if (!more && card.scrollHeight > card.clientHeight) {
      card.insertAdjacentHTML("beforeend", '<div class="peek-more">Continues &mdash; scroll, or open the link.</div>');
    }
  }

  function escapeText(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  // Anchored to the link in page coordinates so the card scrolls with it,
  // flipped above when there isn't room below and nudged back inside the
  // viewport horizontally.
  function place(a) {
    var r = a.getBoundingClientRect();
    card.style.left = "0px";
    card.style.top = "0px";
    card.classList.add("open");
    var w = card.offsetWidth, h = card.offsetHeight;
    var left = Math.min(Math.max(r.left, 8), Math.max(window.innerWidth - w - 8, 8));
    var below = r.bottom + 8;
    var top = (below + h > window.innerHeight && r.top - h - 8 > 0) ? r.top - h - 8 : below;
    card.style.left = (left + window.scrollX) + "px";
    card.style.top = (top + window.scrollY) + "px";
  }

  function show(a) {
    var target = targetOf(a);
    if (!target) return;
    var href = a.getAttribute("href");
    activeLink = a;
    var seq = ++requestSeq;
    if (cache[href]) { render(cache[href], target.base !== BASE); place(a); return; }
    card.innerHTML = '<div class="peek-loading">Loading&hellip;</div>';
    place(a);
    var query = "section=" + encodeURIComponent(target.section) + "&fragment=" + encodeURIComponent(target.fragment);
    fetch("/api" + target.base + "/preview?" + query)
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (seq !== requestSeq || activeLink !== a) return;  // pointer moved on before this landed
        if (!data) { hide(); return; }
        cache[href] = data;
        render(data, target.base !== BASE);
        place(a);
      })
      .catch(function () { if (seq === requestSeq) hide(); });
  }

  function hide() {
    card.classList.remove("open");
    activeLink = null;
    requestSeq++;
  }

  function scheduleShow(a) {
    clearTimeout(closeTimer);
    clearTimeout(openTimer);
    if (activeLink === a) return;
    openTimer = setTimeout(function () { show(a); }, OPEN_DELAY);
  }

  function scheduleHide() {
    clearTimeout(openTimer);
    clearTimeout(closeTimer);
    closeTimer = setTimeout(hide, CLOSE_DELAY);
  }

  document.addEventListener("mouseover", function (e) {
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (a && a.closest(".page")) scheduleShow(a);
    else if (!e.target.closest || !e.target.closest(".linkpeek")) scheduleHide();
  });
  document.addEventListener("mouseout", function (e) {
    if (e.target.closest && (e.target.closest("a[href]") || e.target.closest(".linkpeek"))) scheduleHide();
  });
  card.addEventListener("mouseenter", function () { clearTimeout(closeTimer); });
  card.addEventListener("mouseleave", scheduleHide);
  document.addEventListener("focusin", function (e) {
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (a && a.closest(".page")) scheduleShow(a);
  });
  document.addEventListener("focusout", scheduleHide);
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  window.addEventListener("scroll", function () { if (activeLink) place(activeLink); }, { passive: true });
})();
"""


def page_shell(title: str, body_html: str, previewbar_html: str = "", base_url: str | None = None) -> str:
    """base_url is this Act's own root (e.g. "/browse/crimes-act"). Given
    one, the page also gets link hover previews -- the script needs it to
    tell a link into this Act from any other href on the page. Omitted,
    the page renders exactly as before, without them."""
    body_attr = f' data-base-url="{_esc(base_url)}"' if base_url else ""
    preview_script = f"<script>{PREVIEW_SCRIPT}</script>\n" if base_url else ""
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n{_FONT_LINKS}\n<style>{PAGE_CSS}</style>\n"
        f"<script>{THEME_HEAD_SCRIPT}</script>\n</head>\n<body{body_attr}>\n"
        f"{previewbar_html}"
        "<button class=\"theme-toggle\" id=\"theme-toggle-btn\" type=\"button\">&#127769;</button>\n"
        f"<div class=\"page\">\n{body_html}\n</div>\n"
        f"<script>{THEME_BODY_SCRIPT}</script>\n{preview_script}</body>\n</html>"
    )
