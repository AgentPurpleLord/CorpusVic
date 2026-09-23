"""
Renders a parsed Act as a live, read-only, HTML browsing
view -- an index page (Part/Division/Subdivision headings, each Section
listed as a link, in document order) plus one page per Section, with
defined terms and Part/Division/"section N" cross-references
hyperlinked between them, the same way a real page reads.

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

A Section page also carries a bar of related documents: the Bill clause
it was enacted from and the Explanatory Memorandum's note on it, worked
out by corpus/commentary.py from run_bill_linking.py's link
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
from corpus import PROJECT_ROOT
import html
import json
import re
from pathlib import Path

from corpus.exporters.akn_export import _format_num, build_hierarchy_tree
from corpus.parsing.tables import split_rows
from corpus.domain.amendments import anchor_id, describe, linkify_note
from corpus.domain.diffing import node_diff, normalise, provision_identity, word_diff
from corpus.review.inheritance import relative_id
from corpus.domain.hierarchy import HIERARCHY_ORDER, SECTION_LEVEL_TYPES, schedule_is_pageable, schedule_numbers
from corpus.domain.act_registry import load_act_registry
from corpus.review.link_targets import load_known_acts
from corpus.domain.act_scope import ACT_TITLE_SPAN_RE, scope_by_unit
from corpus.exporters.markdown_export import (
    _DIVISION_REF_RE,
    _PART_REF_RE,
    _collect_verification,
    _display_title,
    _heading_level,
    _iter_body_units,
    _iter_tree,
    _structural_types,
    assign_filenames,
    apply_definition_overrides,
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


# One document's derived structure -- its tree, its page filenames, its
# definitions -- costs about 80ms to build on an Act the size of the
# Criminal Procedure Act, and every page and every hover card of that Act
# needs the same one. Built afresh each time, a full site build spent
# almost all of its time here: 6000 pages and 6000 previews, each
# rebuilding a tree over several thousand nodes.
#
# Keyed on the identity of the node list rather than its contents, since
# hashing several thousand dicts to avoid rebuilding a structure derived
# from them would cost what it saves. The list itself is held in the key
# so that it cannot be collected and its id() handed to something else --
# the standard hazard of an identity key, and a silent one, since the
# wrong context would render a real page for the wrong document. Callers
# only ever read the result (nothing here assigns into it), so one shared
# copy is safe.
_CONTEXT_CACHE: dict = {}
_CONTEXT_CACHE_MAX = 4


def _override_key(parsed: dict) -> tuple:
    """The part of the cache key that a person's decisions about defined
    terms contribute.

    In the key rather than beside it, because the node list does not
    change when somebody says "stop linking this word": the same list
    object comes back out of the document cache, and without this the
    context built from it before the decision would go on being served.
    A linkifier that quietly ignores an edit is worse than one that never
    offered the edit at all.

    created_at is left out: re-recording the same decision is not a
    different page."""
    return tuple(
        ((row.get("term") or "").strip().lower(), row.get("action"), row.get("section"))
        for row in (parsed.get("definition_overrides") or [])
    )


def _build_context(parsed: dict, act_title: str) -> dict:
    nodes = parsed["nodes"]
    key = (id(nodes), id(parsed.get("hierarchy")), act_title, _override_key(parsed))
    hit = _CONTEXT_CACHE.get(key)
    if hit is not None:
        return hit[1]
    context = _build_context_uncached(parsed, act_title)
    if len(_CONTEXT_CACHE) >= _CONTEXT_CACHE_MAX:
        _CONTEXT_CACHE.pop(next(iter(_CONTEXT_CACHE)))
    # The node list and the hierarchy travel with the entry, keeping both
    # alive for exactly as long as their ids are used as a key.
    _CONTEXT_CACHE[key] = ((nodes, parsed.get("hierarchy")), context)
    return context


def _build_context_uncached(parsed: dict, act_title: str) -> dict:
    nodes = parsed["nodes"]
    hierarchy_order = parsed.get("hierarchy") or HIERARCHY_ORDER
    structural_types = _structural_types(hierarchy_order)
    tree_roots, _collisions = build_hierarchy_tree(nodes, hierarchy_order)
    sections = collect_sections(tree_roots, structural_types)
    filenames_by_eid, section_files = assign_filenames(sections)
    definitions = collect_definitions(sections, filenames_by_eid, section_files)
    # After the patterns, never instead of them: an override is an answer
    # to what the matcher found, and the list a reviewer is shown on the
    # dashboard is this same before-and-after. Both are kept, so that
    # showing somebody what their decisions changed does not mean
    # building the whole structure a second time to find out.
    definitions_found = dict(definitions)
    apply_definition_overrides(definitions, parsed.get("definition_overrides"), section_files)
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
        "definitions_found": definitions_found,
        "index_slugs": index_slugs,
        "part_eids": part_eids,
        "division_eids": division_eids,
        "secref_re": secref_re,
        "index_link_text": index_link_text,
    }


# An Act or Bill's name as it appears inline. Defined once in
# corpus/domain/act_scope.py, which also decides when a provision hands
# the ones nested under it over to the Act it names (issue #57). Never
# trusted on its own: a match is kept only if it is found word for word
# in known_acts.yaml or act_registry.json, so an over-matched span fails
# to link rather than linking to the wrong place.
_ACT_TITLE_SPAN_RE = ACT_TITLE_SPAN_RE


def _foreign_context(slug: str) -> "dict | None":
    """The context of another Act this pipeline has parsed, so a
    reference handed to it by a lead-in (see corpus/domain/act_scope.py)
    can be resolved against that Act's own sections rather than guessed.

    Guessing is the thing to avoid here: _section_filename de-duplicates
    a collision with a "_2" suffix, so a slug built from the number alone
    is right until quietly it isn't. None where the parse isn't on disk,
    which leaves the reference unlinked -- never linked wrong."""
    hit = _FOREIGN_CACHE.get(slug)
    if hit is not None:
        return hit or None
    path = PROJECT_ROOT / "data" / "parsed" / f"{slug}.json"
    if not path.exists():
        # A reprinted Act is held per version ("criminal-procedure-act-v114")
        # while the site addresses it by its bare slug. Any version answers
        # the question being asked here -- where its sections live.
        versions = sorted(( PROJECT_ROOT / "data" / "parsed").glob(f"{slug}-v*.json"))
        if not versions:
            _FOREIGN_CACHE[slug] = {}
            return None
        path = versions[-1]
    parsed = json.loads(path.read_text(encoding="utf-8"))
    context = _build_context(parsed, load_known_acts().get(slug, slug))
    _FOREIGN_CACHE[slug] = context
    return context


_FOREIGN_CACHE: dict = {}


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
    corpus/known_acts.yaml (this pipeline's own parsed Acts, linked
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

    def foreign_ref(text: str, kind: str, scope: str) -> str:
        """A reference a lead-in handed to another Act (issue #57).

        The one thing that must never happen here is falling through to
        this Act's own maps: a list introduced by "...of the Crimes Act
        1958-" is Crimes Act sections, and resolving them here produced a
        link that looked right and went to the wrong law."""
        known = known_acts_by_lower.get(scope.lower())
        if known:
            slug = known[0]
            context = _foreign_context(slug)
            if context is None:
                return text   # that Act isn't parsed here; unlinked beats wrong
            elsewhere = f"{_site_prefix(base_url)}/browse/{slug}"
            if kind == "secref":
                num = re.search(r"\d+[A-Za-z]*", text).group(0)
                filename = context["section_files"].get(num.lower())
                if not filename:
                    return text   # no such section over there
                return f'<a href="{elsewhere}/section/{_strip_md(filename)}">{text}</a>'
            num = text.split(None, 1)[1]
            eids = context["part_eids"] if kind == "partref" else context["division_eids"]
            fragment = eids.get(num.lower())
            return f'<a href="{elsewhere}/#{fragment}">{text}</a>' if fragment else text
        registry = registry_by_lower.get(scope.lower())
        if registry:
            # Detected, named, and not parsed here: the standing resolver
            # says so, which beats both a wrong link and silence.
            href = _legislation_href(registry[1], base_url)
            return (f'<a class="unresolved" href="{href}" '
                    f'title="Not yet parsed into this pipeline">{text}</a>')
        return text

    def replace(m: re.Match, current_file: str, current_fragment: str | None, scope: "str | None") -> str:
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
        if scope and m.lastgroup in ("secref", "partref", "divref"):
            return foreign_ref(text, m.lastgroup, scope)
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

    def linkify(escaped_text: str, current_file: str, current_fragment: str | None = None,
                scope: "str | None" = None) -> str:
        """`scope` is the Act a lead-in above this provision handed it to
        (corpus/domain/act_scope.py). An Act naming itself governs
        nothing -- its own references belong here."""
        if scope and own_title and scope.lower() == own_title.lower():
            scope = None
        return master.sub(lambda m: replace(m, current_file, current_fragment, scope), escaped_text)

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


# The punctuation a defined term's own text can open with, which then
# sits tight against the term instead of taking a space after it
# ("appear, in relation to a party, ..."). static/site/copy.js keeps the
# same list, so what is copied reads exactly as what is on screen.
_TIGHT_AFTER_TERM = (",", ".", ";", ":", ")", "\u2014", "-")


def _provision_html(node_type: str, header_text: "str | None", text_html: "str | None",
                    depth: int, id_attr: str = "", extra_class: str = "") -> str:
    """One provision, in the markup every renderer here emits for one:

        <div class="prov prov-subsection" style="--depth:1">
          <span class="prov-num">(2)</span><span class="prov-text">...</span>
        </div>

    Two elements, two grid columns (see static/site/page.css), and no
    negative offsets anywhere. That last part is the whole design: the
    number used to be hung in the margin by a negative text-indent, which
    put it *outside* the div by construction -- so on a section page it
    was drawn over the outline beside it, further over the larger the
    reading size. A grid column cannot leak, so the div is now the
    absolute confine of everything in the provision, number included.

    The number and the text are separate elements because a grid needs
    them to be: contiguous text and each inline link in it would
    otherwise become a grid item of its own, and the prose would come
    apart into columns.

    A defined term is the exception that proves it. It is not a number in
    a margin -- it is the first words of its own sentence, set in italics
    where the drafting convention introduces it -- so it goes inside the
    text, and the provision spans both columns."""
    classes = ["prov", f"prov-{_esc(node_type)}"]
    if extra_class:
        classes.append(extra_class)
    is_definition = node_type == "definition"
    if text_html is None:
        classes.append("prov-heading")  # a heading-only provision (a Subdivision caption, say)
    elif header_text is None:
        # Body text with no number of its own -- a section's lead-in, a
        # note, a paragraph the parser couldn't number.
        classes.append("prov-nolabel")

    bits = []
    if header_text is not None and not is_definition:
        bits.append(f'<span class="prov-num">{_esc(header_text)}</span>')
    if text_html is not None:
        term = ""
        if header_text is not None and is_definition:
            gap = "" if text_html.lstrip().startswith(_TIGHT_AFTER_TERM) else " "
            term = f'<span class="prov-term">{_esc(header_text)}</span>{gap}'
        bits.append(f'<span class="prov-text">{term}{text_html}</span>')
    elif header_text is not None and is_definition:
        bits.append(f'<span class="prov-text"><span class="prov-term">{_esc(header_text)}</span></span>')
    return (
        f'<div class="{" ".join(classes)}"{id_attr} style="--depth:{depth}">'
        f'{"".join(bits)}</div>'
    )


# What the printed Act heads a note or an example with, and what the
# parser eats: the recognition rules match the bold "Note" line to find
# the thing, and the word itself is not kept on the node (see
# domain/rules/recognition/victorian-act.yaml). Put back here, where the
# page is set, because without it a note is a paragraph indistinguishable
# from the law it hangs off.
_CAPTIONED_TYPES = {"note": ("Note", "Notes"), "example": ("Example", "Examples")}


def _caption_html(units: list[dict], i: int, base_depth: int = 0) -> str:
    """The bold heading over a run of notes or examples, where units[i]
    is the first of one -- "" anywhere else.

    Plural from the length of the run, as the Act prints it: one note is
    headed "Note", several are headed "Notes" and then numbered."""
    def kind(unit):
        node = unit["tree_node"]["node"]
        return node["type"] if node["type"] in _CAPTIONED_TYPES else None

    here = kind(units[i])
    if here is None:
        return ""
    depth = units[i]["depth"]
    # A run is what a reader sees as one block: same type, same depth,
    # uninterrupted. Anything nested under a note (rare, but a note can
    # carry its own paragraphs) sits deeper and neither starts a run nor
    # breaks the count.
    if i and kind(units[i - 1]) == here and units[i - 1]["depth"] == depth:
        return ""
    run = 0
    for unit in units[i:]:
        if kind(unit) != here or unit["depth"] != depth:
            break
        run += 1
    singular, plural = _CAPTIONED_TYPES[here]
    label = plural if run > 1 else singular
    return (
        f'<div class="prov prov-caption prov-caption-{_esc(here)}"'
        f' style="--depth:{max(depth - base_depth, 0)}">'
        f'<span class="prov-text">{label}</span></div>'
    )


def _table_html(node: dict, depth: int, id_attr: str = "", render_cell=None) -> str:
    """A table, as a real table.

    Its rows are stored as text -- one per line, cells separated by a
    pipe (see corpus/tables.py) -- which is what makes a table as
    editable in review as any other provision. Here they go back to being
    columns, because that is the only form in which the thing can be read
    at all: "An offence against a child under the age of 16" means
    nothing without the defence printed beside it.

    The first row is the header. Every table in this corpus has one, and
    they say so in as many words ("Column 1 / Column 2", "Provisions of
    this Act / Subject-matter")."""
    render_cell = render_cell or _esc
    rows = split_rows(node.get("text") or "")
    if not rows:
        return ""
    caption = f"<caption>{_esc(node['heading'])}</caption>" if node.get("heading") else ""
    head = "".join(f"<th>{render_cell(cell)}</th>" for cell in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{render_cell(cell)}</td>" for cell in row) + "</tr>"
        for row in rows[1:]
    )
    return (
        f'<div class="prov prov-table"{id_attr} style="--depth:{depth}">'
        f"<table>{caption}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        "</div>"
    )


def _margin_notes_html(node: dict, base_url: str = "", amendment_index: dict | None = None) -> str:
    """This one provision's own amendment-history notes, for the right-
    hand margin column beside it -- the same place the source PDF
    prints them, rather than gathered into one list at the foot of the
    page. A note whose attachment was a guess (confidence "low" -- see
    tree.py's attach_history) is marked, so a reader can tell "the
    drafter put this here" apart from "the parser worked out where this
    probably goes".

    Given an amendment_index (see corpus/amendments.py), the
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
                 superseded: dict | None = None,
                 show_review_badge: bool = True, related: "list[dict] | None" = None,
                 ghosts: "list[dict] | None" = None) -> str:
    """base_url is this Act's own root, e.g. "/browse/crimes-act" (no
    trailing slash) -- every link rendered here and in render_section
    is built from it, so the caller controls the URL scheme entirely.

    superseded, if given, is {"version", "current", "current_url",
    "as_at_printed"} -- see render_superseded_banner.

    related, if given, is the Bill this Act was enacted from and that
    Bill's Explanatory Memorandum, as [{"title", "href", "kind"}]. They
    belong to the Act rather than standing beside it -- an Explanatory
    Memorandum is written about a Bill and is meaningless without it --
    so they are offered here, from the Act's own contents, rather than on
    the site's front page as if the three were separate publications.

    show_review_badge is how much of the Act a human has checked. That is
    what a reviewer wants to know, and the wrong shape for a reader: it
    is one verdict on a whole Act, where whether a human has read the
    provision in front of you is a fact about that provision. The site
    says it per provision instead, on the provision (see
    export_static_site.py); the dashboard, whose whole job is tracking
    the Act's progress, keeps the badge.

    ghosts, if given, are the provisions this version no longer has
    (dashboard._ghosts), each listed greyed where it used to sit -- after
    the provision it followed -- so a gap in the numbering reads as a
    repeal rather than as a mistake."""
    ctx = _build_context(parsed, act_title)
    ghosts_after: dict = {}
    for ghost in ghosts or []:
        ghosts_after.setdefault(ghost.get("after_page"), []).append(ghost)

    def ghost_items(after) -> str:
        return "".join(
            f'<li class="ghost" id="{_esc(g["page"])}"><a href="{base_url}/section/{_esc(g["page"])}">'
            f'{_esc(g["label"])}{" " + _esc(g["heading"]) if g.get("heading") else ""}</a> '
            f'<span class="ghost-tag">Repealed</span></li>'
            for g in ghosts_after.pop(after, []))
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
    # states it (see corpus/versions.py). This is just a statement
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
    if related:
        # The title alone. These used to carry "-- the Bill it was enacted
        # from" and "-- the Explanatory Memorandum written about that
        # Bill", which were there because both links read "Criminal
        # Procedure Bill 2008": an EM's front matter names the Bill, so
        # the two titles were identical and only the gloss told them
        # apart. The EM now says what it is in its own name, and a
        # sentence explaining a link that already explains itself is
        # noise.
        items = "".join(
            f'<li><a href="{_esc(doc["href"])}">{_esc(doc["title"])}</a></li>'
            for doc in related
        )
        out.append(f'<div class="related"><h2>Related documents</h2><ul class="section-list">{items}</ul></div>')
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
                out.append(ghost_items(None))
            # Anchored by the same slug its own page is addressed by, so
            # a link to a provision's place in these contents and a link
            # to the provision itself are the same name with and without
            # the "section/" in front.
            entry_id = _strip_md(filenames_by_eid[tree_node["eid"]])
            out.append(f'<li id="{_esc(entry_id)}"><a href="{href}">{_esc(label)}</a></li>')
            out.append(ghost_items(entry_id))
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

    # The skeleton beside the contents, so a contents page running to
    # hundreds of provisions can be moved around rather than only
    # scrolled. This is the reading page's outline, moved: a provision's
    # own page is now the provision and nothing else.
    outline = _index_outline_html(ctx, act_title, base_url, bool(parsed.get("endnotes")))
    if not outline:
        return "\n".join(out)
    return (
        f'<div class="reader-cols">{outline}'
        f'<div class="reader-main">{chr(10).join(out)}</div></div>'
    )


def _crossrefs_html(crossrefs: list[dict]) -> str:
    """The "where else this provision is explained" bar -- one chip per
    related document (the Bill clause this section was enacted from, an
    Explanatory Memorandum note about it). Each chip is an ordinary
    link into that document's own page, so hovering one previews it the
    same way every other link on the page does; the caller
    (dashboard.py, via corpus/commentary.py) works out what
    belongs here.

    The chips alone, with no label over them: each one names the
    document it goes to, so a word introducing them said nothing the
    chips did not already say."""
    if not crossrefs:
        return ""
    chips = "".join(
        f'<a class="crossref crossref-{_esc(ref.get("kind") or "other")}" href="{_esc(ref["href"])}"'
        f'{f" title=" + chr(34) + _esc(ref["title"]) + chr(34) if ref.get("title") else ""}>'
        f'{_esc(ref["label"])}</a>'
        for ref in crossrefs
    )
    return f'<div class="crossrefs">{chips}</div>'


# ---------------------------------------------------------------------------
# A provision's timeline
# ---------------------------------------------------------------------------
# An Act is reprinted every few weeks and each reprint restates the
# whole thing, so the only way to see how a provision's wording has
# moved is to compare the reprints (corpus/diffing.py). What comes
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


def _wording_units(nodes: list[dict], hierarchy_order: list[str]) -> list[dict]:
    """One wording's pieces, in the order and at the depths the page
    itself sets them -- the same walk render_section makes, so an old
    wording reads the way the current one does."""
    roots, _collisions = build_hierarchy_tree([dict(n) for n in nodes], hierarchy_order)
    if not roots:
        return []
    root = roots[0]
    root_name = root["node"].get("_node_id") or root["node"].get("id") or ""
    units = list(_iter_body_units(root))
    # Aligned on each piece's own label -- "(b)", or the last part of its
    # name for a piece with none -- rather than its full name, which
    # carries the nesting a parse inferred: one reprint read (b) as under
    # (ac) and the next did not, and full names made that a deletion and
    # an insertion of identical words.
    for unit in units:
        node = unit["tree_node"]["node"]
        name = node.get("_node_id") or node.get("id")
        rel = relative_id(name, root_name) if name else node["type"]
        unit["align"] = (unit["header_text"] or rel.rsplit("/", 1)[-1], unit["clause_index"])
    return units


def _units_html(units: list[dict], texts: "list[str] | None" = None, classes: "list[str] | None" = None) -> str:
    out = []
    for i, unit in enumerate(units):
        node = unit["tree_node"]["node"]
        text_html = texts[i] if texts is not None else (
            None if unit["text"] is None else _esc(unit["text"]))
        out.append(_provision_html(node["type"], unit["header_text"], text_html, unit["depth"],
                                   extra_class=classes[i] if classes else ""))
    return "".join(out)


def _compare_html(older: list[dict], newer: list[dict]) -> str:
    """Two wordings, piece by piece, with what went struck through in red
    and what arrived in green. Always read older to newer, whichever of
    the two a reader is looking at, so red means removed by Parliament
    and green means added -- never the other way about."""
    ops = node_diff([(u["align"], u["text"] or "") for u in older],
                    [(u["align"], u["text"] or "") for u in newer])
    # The heading is amended in its own right ("S. 366 (Heading) amended
    # by ..."), and a comparison that skipped it would say nothing changed.
    heading = ""
    old_heading, new_heading = (
        (units[0]["tree_node"]["node"].get("heading") or "") if units else "" for units in (older, newer))
    if old_heading != new_heading:
        heading = (f'<div class="hist-heading">'
                   f'{_diff_html(word_diff(normalise(old_heading), normalise(new_heading)))}</div>')
    units, texts, classes = [], [], []
    for op in ops:
        unit = newer[op["new"]] if op["new"] is not None else older[op["old"]]
        units.append(unit)
        texts.append(None if unit["text"] is None and not op["diff"] else _diff_html(op["diff"]))
        classes.append({"delete": "prov-gone", "insert": "prov-new"}.get(op["op"], ""))
    return heading + _units_html(units, texts, classes)


def _span_label(wording: dict) -> str:
    first, last = wording["from"], wording["to"]
    if first["version"] == last["version"]:
        when = f" (as at {_esc(first['as_at_printed'])})" if first.get("as_at_printed") else ""
        return f"Version {first['version']}{when}"
    when = ""
    if first.get("as_at_printed") and last.get("as_at_printed"):
        when = f" ({_esc(first['as_at_printed'])} to {_esc(last['as_at_printed'])})"
    return f"Versions {first['version']}\u2013{last['version']}{when}"


_ENDED_VERB = {"changed": "Amended", "inserted": "Inserted", "repealed": "Repealed"}


def _ended_html(wording: dict, base_url: str, amendment_index: "dict | None") -> str:
    """What brought this wording to an end, as the next version's own
    margin notes record it. The version and date are ours; the Act named
    is the reprint's own account, linked to its Endnotes entry."""
    ended = wording.get("ended_by")
    if not ended:
        return ""
    when = f" (as at {_esc(ended['as_at_printed'])})" if ended.get("as_at_printed") else ""
    verb = _ENDED_VERB.get(ended["change"], "Changed")
    notes = "".join(_timeline_note_html(raw, base_url, amendment_index) for raw in ended.get("notes") or [])
    if not notes:
        notes = '<span class="tl-note hist-quiet">The reprint does not say by what.</span>'
    return (f'<div class="hist-ended hist-{_esc(ended["change"])}">'
            f'<span class="tl-verb">{verb}</span> at Version {ended["version"]}{when}'
            f'<div class="tl-notes">{notes}</div></div>')


def render_history(history: "dict | None", base_url: str, amendment_index: "dict | None" = None,
                   version_urls: "dict | None" = None, hierarchy_order: "list[str] | None" = None,
                   anchor: str = "", open_: bool = False) -> tuple[str, str]:
    """A provision's wordings across the versions held here, as
    (the chip that opens them, the wordings themselves) -- ("", "") for a
    provision that has only ever read one way.

    `history` is dashboard._provision_timeline's: a chain of wordings
    from corpus/domain/lineage.py, oldest first, with "at" the one this
    page's own version carries.

    Every wording is rendered whole, and every comparison a reader can
    ask for is rendered too: the page must work as a static file with no
    server to ask, and a provision has a handful of wordings at most.
    Without JavaScript it is a list inside <details>; static/site/
    history.js makes it the side-by-side timeline, one earlier wording
    on the left and one later on the right.
    """
    wordings = (history or {}).get("wordings") or []
    if len(wordings) < 2:
        return "", ""
    order = hierarchy_order or HIERARCHY_ORDER
    at = history.get("at")
    units = [None if w["absent"] else _wording_units(w["provision"].get("nodes") or [], order)
             for w in wordings]
    newest = at == len(wordings) - 1
    here_label = "current" if newest else "this page"
    panel_id = f"hist-{_esc(anchor)}" if anchor else "hist"

    panels = []
    for n, wording in enumerate(wordings):
        classes = ["hist-panel"]
        if wording["absent"]:
            classes.append("hist-absent")
        if n == at:
            classes.append("hist-here")
        label = _span_label(wording)
        href = None if wording["absent"] else (version_urls or {}).get(wording.get("version"))
        stamp = f'<a class="tl-version" href="{_esc(href)}">{label}</a>' if href else f'<span class="tl-version">{label}</span>'
        if n == at:
            stamp += ' <span class="hist-tag">This page</span>'
        head = [f'<div class="hist-when">{stamp}</div>']
        if not wording["absent"] and not wording.get("checked"):
            head.append('<div class="hist-unchecked">Not yet checked against the printed Act.</div>')
        head.append(_ended_html(wording, base_url, amendment_index))

        views = []
        if wording["absent"]:
            words = ("Not yet in the Act." if n == 0 else
                     "Not in the Act: repealed." if n == len(wordings) - 1 else "Not in the Act.")
            views.append(f'<div class="hist-body" data-view="text"><p class="hist-gone">{words}</p></div>')
        else:
            heading = wording["provision"].get("heading")
            title = f'<div class="hist-heading">{_esc(heading)}</div>' if heading else ""
            views.append(f'<div class="hist-body hist-provisions" data-view="text">{title}{_units_html(units[n])}</div>')
            choices = []
            for view, other, label_text in (("here", at, f"with {here_label}"),
                                            ("prev", n - 1, "with previous"),
                                            ("next", n + 1, "with next")):
                if other is None or other == n or not (0 <= other < len(wordings)) or units[other] is None:
                    continue
                if view != "here" and other == at:
                    continue  # the same comparison as "with current", offered once, by that name
                older, newer = (units[other], units[n]) if other < n else (units[n], units[other])
                views.append(f'<div class="hist-body hist-provisions" data-view="{view}" hidden>'
                             f'{_compare_html(older, newer)}</div>')
                choices.append(f'<button type="button" class="hist-cmp" data-view="{view}">{label_text}</button>')
            if choices:
                head.append('<div class="hist-compare" role="group" aria-label="Compare">Compare '
                            + "".join(choices) + "</div>")
        panels.append(f'<section class="{" ".join(classes)}" data-n="{n}" aria-label="{_esc(label)}">'
                      f'<header class="hist-head">{"".join(head)}</header>{"".join(views)}</section>')

    count = len(wordings)
    chip = (f'<button type="button" class="history-chip" aria-controls="{panel_id}" aria-expanded="false">'
            f'History <span class="history-count">{count}</span></button>')
    body = (
        f'<details class="history" id="{panel_id}" data-at="{at if at is not None else count - 1}"'
        f'{" open" if open_ else ""}>'
        f'<summary class="history-summary">This provision has read {count} ways in the versions held here</summary>'
        f'<div class="hist-track">{"".join(panels)}</div>'
        "</details>"
    )
    return chip, body


def render_ghost(ghost: dict, act_title: str, base_url: str, amendment_index: "dict | None" = None,
                 version_urls: "dict | None" = None, hierarchy_order: "list[str] | None" = None,
                 version: "dict | None" = None, superseded: "dict | None" = None,
                 version_dates: "dict | None" = None) -> str:
    """The page of a provision this version no longer has.

    A section repealed outright leaves nothing in the reprint -- not even
    the asterisks a repealed subsection gets -- so a reader who wants to
    know whether an offence existed on the day it was committed finds a
    gap in the numbering and no way of knowing what was there. This page
    is at the address the provision had, and is nothing but its history,
    opened: every wording it had in the versions held here, and what
    removed it.

    `ghost` is dashboard._ghosts': {"page", "label", "history", ...}.
    """
    history = ghost["history"]
    _chip, history_html = render_history(history, base_url, amendment_index, version_urls,
                                         hierarchy_order, anchor=ghost["page"], open_=True)
    last = next(w for w in reversed(history["wordings"][:history["at"]]) if not w["absent"])
    ended = last.get("ended_by") or {}
    when = f" (as at {_esc(ended['as_at_printed'])})" if ended.get("as_at_printed") else ""
    title = f'{ghost["label"]} [Repealed]'
    this = f"Version {version['version']}" if (version or {}).get("version") is not None else "this version"
    out = [
        _readerbar_html(version or {}, superseded, version_urls, version_dates),
        '<div class="reader-main">',
        f'<article class="reader-section historical" data-section="{_esc(ghost["page"])}" data-title="{_esc(title)}">',
        f'<div class="breadcrumb"><a href="{base_url}/">{_esc(act_title)}</a></div>',
        f"<h1>{_esc(title)}</h1>",
        f'<div class="supersede ghost-banner" role="status">This provision is not in {_esc(this)}. '
        f'It was removed at Version {_esc(str(ended.get("version", "?")))}{when}; '
        f'below is how it read before that.</div>',
        history_html,
        "</article>",
        f'<nav class="section-nav"><a href="{base_url}/#{_esc(ghost["page"])}">Back to the contents</a></nav>',
        "</div>",
    ]
    return "\n".join(out)


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
# A section page carries two pieces of furniture the Endnotes page does
# not: a bar of reading controls above the text, and the provisions either
# side of this one below it. See static/site/reader.css and reader.js for
# the other half of each.
#
# It used to carry a third, an outline of the rest of the document beside
# the text. That is on the contents page now (_index_outline_html): a
# reader on a provision is reading it, and the breadcrumb, the next and
# previous links and reading on already reach everywhere the outline did.


def _index_outline_html(ctx: dict, act_title: str, base_url: str,
                        has_endnotes: bool = False) -> str:
    """The Act's own skeleton, beside its contents.

    Chapters, Parts and Divisions only -- the things a reader moves
    between in a contents page that runs to hundreds of provisions. Every
    line is an anchor into this same page, so following one scrolls the
    contents rather than leaving them; the provisions themselves are
    already listed in the column beside this, one click from their own
    page. Listing them here as well would put the page beside itself.

    Where each line points has to be decided the same way the contents
    beside it decide what to render, or the outline links to anchors that
    are not there. A Schedule long enough to be its own page (see
    hierarchy.schedule_is_pageable) is an entry in the contents list, not
    a heading over one, so it is anchored by the entry's own id and
    nothing inside it is listed -- the contents do not list it either.
    Everything else is a heading, anchored by the slug render_index puts
    on it, which is also what a provision page's breadcrumb links back to.
    """
    structural_types = ctx["structural_types"]
    index_slugs = ctx["index_slugs"]
    filenames_by_eid = ctx["filenames_by_eid"]

    def anchor(tree_node: dict) -> "tuple[str | None, bool]":
        """The id to link to, and whether to look inside."""
        node = tree_node["node"]
        if node["type"] in SECTION_LEVEL_TYPES or (
                node["type"] == "schedule" and schedule_is_pageable(tree_node)):
            name = filenames_by_eid.get(tree_node["eid"])
            return (_strip_md(name) if name else None), False
        return index_slugs.get(tree_node["eid"]), True

    def branch(tree_node: dict) -> str:
        items = []
        for child in tree_node["children"]:
            node = child["node"]
            if node["type"] not in (*structural_types, "heading_group"):
                continue
            title = _display_title(node["type"], node.get("number"), node.get("heading"))
            slug, descend = anchor(child)
            # No slug, no link -- an anchor that scrolls nowhere looks
            # like the page failed. Same rule the breadcrumb follows.
            label = f'<a href="#{_esc(slug)}">{_esc(title)}</a>' if slug else _esc(title)
            inside = branch(child) if descend else ""
            items.append(f'<li class="outline-struct">{label}{inside}</li>')
        return f'<ul class="outline-list">{"".join(items)}</ul>' if items else ""

    body = "".join(
        branch({"children": [root], "node": {"type": ""}, "eid": ""})
        for root in ctx["tree_roots"]
    )
    if not body:
        # A flat Act with no Parts has no skeleton to show, and an empty
        # column beside the contents is worse than no column.
        return ""
    endnotes = (
        f'<a class="outline-endnotes" href="{base_url}/endnotes">Endnotes</a>' if has_endnotes else ""
    )
    return (
        '<nav class="outline" aria-label="Contents">'
        f'<a class="outline-doc" href="{base_url}/">{_esc(act_title)}</a>'
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


def _scope_label(node: dict) -> str:
    """What a structural level is called on its own: "Part III",
    "Division 1", "Subdivision (1)" -- the name without its heading,
    which reading on sets separately from it."""
    kind = node["type"].capitalize()
    number = node.get("number")
    return f"{kind} {_format_num(node['type'], number)}" if number else kind


def render_section(
    parsed: dict, act_title: str, base_url: str, section_slug: str,
    crossrefs: list[dict] | None = None, amendment_index: dict | None = None,
    timeline: dict | None = None, version_urls: dict | None = None,
    superseded: dict | None = None, version_dates: dict | None = None,
    show_review_badge: bool = True, notice: "str | None" = None,
) -> str | None:
    """Renders the Section whose assign_filenames-computed id matches
    section_slug (the same string render_index links to), or None if no
    Section matches -- the caller (dashboard.py) turns that into a 404.

    crossrefs, if given, are the related-document chips described in
    _crossrefs_html; amendment_index, if given, is what lets each
    margin note name the Act behind its citation (see
    _margin_notes_html).

    timeline, if given, is this provision's history from
    dashboard._provision_timeline -- every wording it has had across
    the versions of the Act held here (see render_history) -- with version_urls
    mapping a version number to that version's page for this same
    provision. superseded, if given, is {"version", "current",
    "current_url", "as_at_printed"} for the banner saying this reprint
    is no longer the law. version_dates, if given, is {version -> the
    date that version states it incorporates amendments to}, which is
    what the "Compare with another version" choices are labelled with.

    parsed["version"], if present, is this reprint's own front matter --
    what the "Text as at" line states.

    notice, if given, is HTML set at the top of the reading column, above
    the provision's own heading -- what the reader has to know before the
    words below them mean anything. The published site uses it to say
    that a provision has not been checked by a human (see
    export_static_site.py); it is raw HTML because what needs saying is a
    sentence with a link in it, not a string.

    show_review_badge is how much of this provision a human has checked --
    on for the dashboard, whose job is tracking that. The site says the
    same thing through `notice`, on the provisions it is actually true
    of, rather than as a badge on every page (see render_index, which
    turns it off for the same reason)."""
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

    # One column, and the law in it. The outline that used to sit beside
    # the text now sits beside the contents instead (_index_outline_html):
    # a reader on a provision is reading it, and the breadcrumb, the
    # next/prev links and reading on already carry them everywhere the
    # outline did. The reading controls stay, because they are about how
    # this text is set rather than about where else to go.
    out = [
        _readerbar_html(parsed.get("version") or {}, superseded, version_urls, version_dates),
        '<div class="reader-main">',
    ]
    if notice:
        out.append(notice)
    # Every part of the trail is a link, not just the first. "Act index »
    # Part I » Division 1 » Subdivision (4)" names four places a reader
    # might want to be, and until now only one of them could be reached
    # from here.
    #
    # The anchors already exist: compute_index_slugs gives every Chapter,
    # Part, Division and Subdivision a heading slug, and render_index
    # emits each one as an id on its heading. This is the same
    # {base_url}/#{fragment} form _build_linkifier_html builds for prose
    # "Part 3" references, so the two cannot disagree about where a Part
    # lives.
    # Where this provision sits, whole: every structural level above it,
    # outermost first. Reading on compares one provision's chain with the
    # next one's to say what ended and what began (static/site/readon.js)
    # -- which needs the whole chain, not the innermost level of it,
    # because a section can end a Division and its Part at once and a
    # single scope can only report one of the two.
    #
    # Compared by eid, never by label: every Part of an Act has a
    # Division 1, so labels repeat and comparing them would miss the
    # break between one Part's last Division and the next Part's first.
    # Label and heading stay apart because the marker and the header set
    # them differently.
    scopes = [
        {"id": b["eid"],
         "label": _scope_label(b["node"]),
         "heading": b["node"].get("heading") or ""}
        for b in breadcrumb
    ]
    scope_attrs = f' data-scopes="{_esc(json.dumps(scopes))}"' if scopes else ""
    # Everything one provision is, in one element, so reading on can lift
    # the next one out of its own page and set it down after this. The
    # page around it -- the reading bar, the outline, the nav below -- is
    # the chrome of whichever page was served and is never duplicated.
    out.append(f'<article class="reader-section" data-section="{_esc(section_slug)}"'
               f' data-title="{_esc(title)}"{scope_attrs}>')

    index_slugs = ctx["index_slugs"]
    crumb_bits = [f'<a href="{base_url}/">{_esc(ctx["index_link_text"])}</a>']
    for b in breadcrumb:
        label = _esc(_display_title(b["node"]["type"], b["node"].get("number"),
                                    b["node"].get("heading")))
        slug = index_slugs.get(b["eid"])
        # No slug, no link. An anchor that scrolls nowhere is worse than
        # plain text: it looks like the page failed rather than like this
        # crumb was never a heading in the index.
        crumb_bits.append(f'<a href="{base_url}/#{_esc(slug)}">{label}</a>' if slug else label)
    out.append(f'<div class="breadcrumb">{" &raquo; ".join(crumb_bits)}</div>')
    if show_review_badge:
        out.append(_verification_badge(verification))
    history_chip, history_html = render_history(
        timeline, base_url, amendment_index, version_urls,
        parsed.get("hierarchy") or None, anchor=section_slug)
    if history_chip:
        out.append(f'<div class="section-head"><h1>{_esc(title)}</h1>{history_chip}</div>')
    else:
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
    out.append(history_html)
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
    # Materialised rather than walked, because a note's own heading is
    # decided by how many notes follow it (see _caption_html).
    units = list(_iter_body_units(tree_node))
    # Which Act a reference belongs to is decided across the whole section
    # before any one line is linked: a lead-in hands every provision
    # nested under it to the Act it names (issue #57).
    scopes = scope_by_unit(units)
    for i, unit in enumerate(units):
        unit_tree_node = unit["tree_node"]
        unit_node = unit_tree_node["node"]
        key = (unit_tree_node["eid"], unit["clause_index"])
        slug = slugs.get(key)
        id_attr = f' id="{_esc(slug)}"' if slug else ""

        caption = _caption_html(units, i)
        if caption:
            # Its own empty margin cell, for the same reason every
            # provision has one: the two columns are auto-placed rows of
            # one grid.
            out.append(caption)
            out.append('<div class="prov-notes"></div>')

        if unit_node["type"] == "table":
            out.append(_table_html(
                unit_node, unit["depth"], id_attr,
                lambda cell, scope=scopes[i]: linkify(_esc(cell), target_filename, slug, scope),
            ))
        else:
            out.append(_provision_html(
                unit_node["type"], unit["header_text"],
                None if unit["text"] is None else linkify(_esc(unit["text"]), target_filename, slug, scopes[i]),
                unit["depth"], id_attr,
            ))
        # One margin cell per provision, empty or not: the two columns
        # are auto-placed rows of the same grid, so a note only stays
        # level with the provision it belongs to if every provision
        # contributes a cell.
        notes = _margin_notes_html(unit_node, base_url, amendment_index) if unit["clause_index"] == 0 else ""
        out.append(f'<div class="prov-notes">{notes}</div>')
    out.append("</div>")   # .provisions
    out.append("</article>")

    out.append(_section_nav_html(sections, match_index, base_url, filenames_by_eid, ctx["index_link_text"]))
    out.append("</div>")   # .reader-main

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
    corpus/endnotes.py's block builder).

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

    `summary` is corpus/amendments.summarise_by_act's output --
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
    depth = max(unit["depth"] - base_depth, 0)
    if node["type"] == "table":
        return _table_html(node, depth)
    return _provision_html(
        node["type"], unit["header_text"],
        None if unit["text"] is None else _esc(unit["text"]),
        depth,
    )


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
            start = 0
            selected = units

        truncated = len(selected) > _PREVIEW_MAX_UNITS
        selected = selected[:_PREVIEW_MAX_UNITS]
        html_bits, chars = [], 0
        for offset, unit in enumerate(selected):
            if chars >= _PREVIEW_MAX_CHARS:
                truncated = True
                break
            # The card sets a note the way the page does, heading and
            # all: a preview that quietly dropped it would show the note
            # as the provision's own words.
            html_bits.append(_caption_html(units, start + offset, base_depth))
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
        _provision_html("section", sec["number"], _esc(sec.get("heading") or ""), 0)
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
TEMPLATE_DIR = PROJECT_ROOT / "static" / "site"

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


def template_html(name: str) -> str:
    """A template file, ready to put in a page.

    The same live re-read as template_text, with the authoring comments
    taken out. Those comments exist for whoever is editing the file --
    which placeholder is which, what must not be removed -- and are of no
    use to a reader, so they should not be served to one."""
    return _TEMPLATE_COMMENT_RE.sub("", template_text(name)).strip()


def _search_form_html(search_url: "str | None", query: str) -> str:
    """The header's search box, or nothing at all.

    A plain GET form: no script, no fetch, no JSON. A reference work
    about the law should still be searchable in a browser with
    JavaScript turned off, and making that the baseline costs nothing."""
    if not search_url:
        return ""
    return (
        f'<form class="sitesearch" action="{_esc(search_url)}" method="get" role="search">'
        f'<input type="search" name="q" value="{_esc(query)}" '
        'placeholder="Search" aria-label="Search the corpus">'
        "</form>"
    )


def page_shell(title: str, body_html: str, previewbar_html: str = "",
               base_url: str | None = None, reader: bool = False,
               preview_source: str = "api", site_salt: str | None = None,
               site_prefix: str | None = None, search_url: str | None = None,
               search_query: str = "", canonical: str | None = None) -> str:
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

    search_url is where the header's search box submits to, and giving
    none leaves the box out altogether -- which is what the archive build
    wants, since a static host has nothing to answer it and a box that
    404s is worse than no box. It is a plain GET form, so search works
    with JavaScript off.

    canonical is this page's own address, written into the head. Reading
    on changes the address bar as a section scrolls past (see
    static/site/readon.js), so each page saying which address it is the
    copy at keeps that answer the same however it was arrived at.

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
        # Which address this page is *the* copy at. Reading on changes
        # the address bar as a reader scrolls, so saying it here rather
        # than leaving it to be inferred is what keeps the answer the
        # same however the page was arrived at.
        "{{CANONICAL}}": (f'<link rel="canonical" href="{_esc(canonical)}">' if canonical else ""),
        "{{ASSETS}}": _esc(f"{prefix}/assets"),
        "{{HOME}}": _esc(f"{prefix}/"),
        "{{BODY_ATTRS}}": body_attrs,
        "{{MAIN_CLASS}}": "page page-reader" if reader else "page",
        "{{PREVIEWBAR}}": previewbar_html,
        "{{SEARCH}}": _search_form_html(search_url, search_query),
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
    page = template_html("page.html")
    for placeholder, value in replacements.items():
        page = page.replace(placeholder, value)
    return page
