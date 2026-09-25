"""Tests for corpus/html_view.py's pure rendering logic -- chiefly
render_preview, which decides what one hover card gets to show. The
dashboard endpoint that serves it, the section/index page renderers and
the browser-side hover behaviour itself were exercised end to end against
real parsed Act data and a real browser session instead."""
import re

from corpus.domain.amendments import build_amendment_index
from corpus.domain.diffing import provision_identity
from corpus.publishing.html_view import (
    build_page_index,
    render_endnotes,
    render_index,
    render_preview,
    render_section,
    render_history,
    render_superseded_banner,
)

from conftest import make_node


def _parsed(nodes: list[dict]) -> dict:
    return {"nodes": nodes, "hierarchy": None}


def _definitions_act() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3", "Definitions", "In this Act—"),
        make_node("definition", None, "accused", "means a person who—"),
        make_node("paragraph", "a", None, "is charged with an offence; or"),
        make_node("paragraph", "b", None, "is directed to be tried for perjury;"),
        make_node("definition", None, "appeal", "includes an application for leave to appeal;"),
        make_node("section", "4", "Meaning of sexual offence", "A sexual offence means—"),
    ]


def test_render_preview_of_a_defined_term_includes_its_own_paragraphs():
    # The whole point of previewing a definition: a term whose meaning is a
    # list of (a)/(b) paragraphs is only answered by showing them too.
    preview = render_preview(_parsed(_definitions_act()), "Test Act", "s3", "accused")

    assert preview["title"] == "accused"
    assert preview["subtitle"] == "3 Definitions"
    assert "is charged with an offence" in preview["html"]
    assert "is directed to be tried for perjury" in preview["html"]
    assert not preview["truncated"]


def test_render_preview_of_a_defined_term_stops_at_the_next_term():
    preview = render_preview(_parsed(_definitions_act()), "Test Act", "s3", "accused")

    assert "leave to appeal" not in preview["html"]


def test_render_preview_of_a_section_without_a_fragment_shows_its_opening():
    preview = render_preview(_parsed(_definitions_act()), "Test Act", "s4", None)

    assert preview["title"] == "4 Meaning of sexual offence"
    assert "A sexual offence means" in preview["html"]


def test_render_preview_indentation_is_relative_to_the_previewed_provision():
    # The card starts at the definition, so the definition itself is depth
    # 0 in it even though it sits below a Section on the real page.
    preview = render_preview(_parsed(_definitions_act()), "Test Act", "s3", "accused")

    assert 'class="prov prov-definition" style="--depth:0"' in preview["html"]
    assert 'class="prov prov-paragraph" style="--depth:1"' in preview["html"]


def test_render_preview_never_contains_links():
    # A preview is a glance, not a second page: links inside one would
    # invite previews of previews, and its ids would collide with the real
    # page's own anchors.
    nodes = [
        make_node("section", "3", "Definitions", "In this Act—"),
        make_node("definition", None, "accused", "has the meaning given in section 4;"),
        make_node("section", "4", "Meaning", "Text."),
    ]
    preview = render_preview(_parsed(nodes), "Test Act", "s3", "accused")

    assert "<a " not in preview["html"]
    assert ' id="' not in preview["html"]


def test_render_preview_of_an_index_anchor_lists_the_sections_under_it():
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes are—"),
        make_node("section", "2", "Commencement", "This Act comes into operation—"),
    ]
    preview = render_preview(_parsed(nodes), "Test Act", None, "part-1---preliminary")

    assert preview["title"] == "Part 1 - Preliminary"
    assert preview["subtitle"] == "2 section(s)"
    assert "Purposes" in preview["html"] and "Commencement" in preview["html"]


def test_render_preview_marks_a_long_section_as_truncated():
    nodes = [make_node("section", "5", "A long one", "Lead-in—")]
    nodes += [make_node("subsection", str(i), None, f"Subsection {i} text.") for i in range(1, 12)]
    preview = render_preview(_parsed(nodes), "Test Act", "s5", None)

    assert preview["truncated"]
    assert "Subsection 11 text" not in preview["html"]


def test_render_preview_returns_none_for_a_target_that_does_not_resolve():
    parsed = _parsed(_definitions_act())

    assert render_preview(parsed, "Test Act", "s99", None) is None
    assert render_preview(parsed, "Test Act", "s3", "no-such-term") is None
    assert render_preview(parsed, "Test Act", None, "no-such-heading") is None
    assert render_preview(parsed, "Test Act", None, None) is None


def _bill_nodes() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("clause", "1", "Purposes", "The purposes of this Bill are—"),
        make_node("paragraph", "a", None, "to do a thing."),
        make_node("clause", "2", "Commencement", "This Bill comes into operation on Royal Assent."),
    ]


def test_render_index_of_a_bill_lists_its_clauses():
    # A Bill's top-level provisions are "clause", not "section" -- which
    # used to make its whole index empty.
    html = render_index(_parsed(_bill_nodes()), "Test Bill", "/browse/test-bill")

    assert '/browse/test-bill/section/c1' in html
    assert "1 Purposes" in html


def test_render_section_of_a_bill_clause_names_its_type():
    body = render_section(_parsed(_bill_nodes()), "Test Bill", "/browse/test-bill", "c1")

    assert "<h1>Clause 1 Purposes</h1>" in body
    assert "to do a thing." in body


def test_render_index_of_an_em_falls_back_to_a_text_snippet():
    # EM entries have no headings, so a list of them would otherwise be a
    # column of bare numbers.
    nodes = [make_node("clause", "1", None, "sets out the purposes of the Bill, which are to consolidate the law.")]
    html = render_index(_parsed(nodes), "Test EM", "/browse/test-em")

    assert "Clause 1 sets out the purposes of the Bill" in html


def test_a_bill_page_calls_its_own_index_contents_not_act_index():
    body = render_section(_parsed(_bill_nodes()), "Test Bill", "/browse/test-bill", "c1")

    assert "Contents" in body
    assert "Act index" not in body


def _nested_act() -> list[dict]:
    """Part > Division > Subdivision > Section -- the depth the reader
    breadcrumb is actually about."""
    return [
        make_node("part", "I", "Offences"),
        make_node("division", "1", "Offences against the person"),
        make_node("subdivision", "4", "Offences against the person"),
        make_node("section", "16", "Causing serious injury intentionally",
                  "A person who causes serious injury is guilty of an offence."),
    ]


def test_every_part_of_the_breadcrumb_is_a_link():
    """"Act index >> Part I >> Division 1 >> Subdivision (4)" names four
    places a reader might want to be, and only the first of them used to
    be reachable from the page."""
    body = render_section(_parsed(_nested_act()), "Test Act", "/browse/a", "s16")

    crumb = re.search(r'<div class="breadcrumb">(.*?)</div>', body, re.S).group(1)
    links = dict((label, href) for href, label in
                 re.findall(r'<a href="([^"]+)">(.*?)</a>', crumb))

    assert links["Act index"] == "/browse/a/"
    assert links["Part I - Offences"] == "/browse/a/#part-i---offences"
    assert links["Division 1 - Offences against the person"] == \
        "/browse/a/#division-1---offences-against-the-person"
    assert links["Subdivision (4) - Offences against the person"] == \
        "/browse/a/#subdivision-4---offences-against-the-person"


def test_a_breadcrumb_link_lands_on_a_heading_that_exists():
    """The failure this could have: four links that all look right and
    scroll nowhere. So the anchors are checked against the index page
    rather than against the slug function that generated both."""
    parsed = _parsed(_nested_act())
    body = render_section(parsed, "Test Act", "/browse/a", "s16")
    index = render_index(parsed, "Test Act", "/browse/a")

    crumb = re.search(r'<div class="breadcrumb">(.*?)</div>', body, re.S).group(1)
    anchors = {href.split("#", 1)[1]
               for href, _label in re.findall(r'<a href="([^"]+)">(.*?)</a>', crumb)
               if "#" in href}
    ids = set(re.findall(r'<h[1-6] id="([^"]+)"', index))

    assert anchors and anchors <= ids


def test_the_breadcrumb_follows_the_path_prefix_it_is_served_under():
    """render_section serves both the public site and the dashboard's
    /admin. A link built for the root is a 404 under the prefix, which is
    exactly how the admin search results broke."""
    body = render_section(_parsed(_nested_act()), "Test Act", "/admin/browse/a", "s16")

    crumb = re.search(r'<div class="breadcrumb">(.*?)</div>', body, re.S).group(1)

    assert 'href="/admin/browse/a/#part-i---offences"' in crumb
    assert 'href="/browse/' not in crumb


def test_a_crumb_with_no_heading_of_its_own_stays_plain_text():
    """An anchor that scrolls nowhere is worse than plain text: it reads
    as the page having failed rather than as this crumb never having been
    a heading in the index."""
    from corpus.publishing import html_view

    parsed = _parsed(_nested_act())
    ctx = html_view._build_context(parsed, "Test Act")
    # As if the Division had never been given an index heading.
    division = next(eid for eid, slug in ctx["index_slugs"].items()
                    if slug.startswith("division-1"))
    real_build = html_view._build_context

    def without_the_division(*a, **k):
        built = real_build(*a, **k)
        built["index_slugs"].pop(division, None)
        return built

    html_view._build_context = without_the_division
    try:
        body = render_section(parsed, "Test Act", "/browse/a", "s16")
    finally:
        html_view._build_context = real_build

    crumb = re.search(r'<div class="breadcrumb">(.*?)</div>', body, re.S).group(1)
    assert "Division 1 - Offences against the person" in crumb
    assert "division-1---offences" not in crumb


def test_render_section_renders_the_related_document_chips():
    """The chips and nothing over them: each one names the document it
    goes to, so a label introducing them said nothing they didn't."""
    crossrefs = [
        {"kind": "bill", "label": "Bill clause 5", "href": "/browse/b/section/c5", "title": "the clause"},
        {"kind": "em", "label": "EM on clause 5", "href": "/browse/b-em/section/c5"},
    ]
    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3", crossrefs=crossrefs)

    assert "crossrefs-label" not in body
    assert "Explained in" not in body
    assert '<a class="crossref crossref-bill" href="/browse/b/section/c5" title="the clause">Bill clause 5</a>' in body
    assert '<a class="crossref crossref-em" href="/browse/b-em/section/c5">EM on clause 5</a>' in body


def test_render_section_without_crossrefs_renders_no_bar():
    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3")

    assert "crossrefs" not in body


# --- notes and examples are announced, as the Act announces them --------
# The parser reads the bold "Note" line to recognise the thing and keeps
# nothing of the word, so without this a note is a paragraph
# indistinguishable from the provision it hangs off.

def _act_with_notes() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3", "Commencement", "This Act comes into operation—"),
        make_node("note", None, None, "Section 10 of the Interpretation Act applies."),
        make_node("section", "4", "Application", "This Act applies to—"),
        make_node("note", "1", None, "The first thing to know."),
        make_node("note", "2", None, "The second thing to know."),
        make_node("example", None, None, "A person who does the thing."),
    ]


def test_a_single_note_is_headed_note():
    body = render_section(_parsed(_act_with_notes()), "Test Act", "/browse/a", "s3")

    assert '<span class="prov-text">Note</span>' in body
    assert body.index('>Note<') < body.index("Section 10 of the Interpretation Act")


def test_a_run_of_notes_is_headed_once_and_in_the_plural():
    body = render_section(_parsed(_act_with_notes()), "Test Act", "/browse/a", "s4")

    assert body.count("prov-caption-note") == 1
    assert '<span class="prov-text">Notes</span>' in body


def test_examples_are_headed_the_same_way_and_separately_from_notes():
    body = render_section(_parsed(_act_with_notes()), "Test Act", "/browse/a", "s4")

    assert '<span class="prov-text">Example</span>' in body
    assert body.index("prov-caption-example") > body.index("The second thing to know")


def test_a_heading_is_a_row_of_the_same_grid_as_the_notes_under_it():
    """The provisions and their margin notes are auto-placed rows of one
    grid (see page.css), so a row that contributes no margin cell puts
    every note below it out of step with its provision -- and a copied
    section reads as the page does only because the heading is a .prov
    like any other (static/site/copy.js)."""
    body = render_section(_parsed(_act_with_notes()), "Test Act", "/browse/a", "s4")

    caption = re.search(r'<div class="prov prov-caption[^>]*>.*?</div>\n(.*)', body).group(1)
    assert caption.startswith('<div class="prov-notes">')


def test_build_page_index_maps_provisions_to_their_pages():
    parsed = _parsed(_bill_nodes())
    index = build_page_index(parsed, "Test Bill")

    assert index["by_node_index"] == {1: "c1", 3: "c2"}
    assert index["by_key"] == {
        provision_identity("clause", None, "1"): "c1",
        provision_identity("clause", None, "2"): "c2",
    }
    assert index["schedule_by_node_index"] == {1: None, 3: None}


def test_build_page_index_keeps_a_schedules_own_clause_off_the_body_page():
    # A Schedule numbers its own provisions from 1 again, so this Bill has
    # a clause 1 and a Schedule 1 clause 1 -- on different pages. Keyed by
    # number alone the lookup returned the body page for both, which is
    # how an EM note on Schedule 1 clause 11 ended up on section 11.
    nodes = _bill_nodes() + [
        make_node("schedule", "1", "Charges on a charge-sheet"),
        make_node("clause", "1", "Statement of offence", "A charge must state the offence."),
    ]
    index = build_page_index(_parsed(nodes), "Test Bill")

    assert index["by_key"][provision_identity("clause", None, "1")] == "c1"
    assert index["by_key"][provision_identity("clause", "1", "1")] == "c1_2"
    assert index["schedule_by_node_index"][5] == "1"


def test_build_page_index_keys_a_pageable_schedule_under_no_schedule_of_its_own():
    # A pageable Schedule (see hierarchy.schedule_is_pageable) is its own
    # page, not a clause of itself -- it must be found by its own number
    # with schedule=None, the same key an ordinary body section of that
    # number would use, discriminated instead by kind.
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("schedule", "3", "Persons who may witness statements", "1 A police officer."),
    ]
    index = build_page_index(_parsed(nodes), "Test Act")

    assert index["by_key"][provision_identity("section", None, "1")] == "s1"
    assert index["by_key"][provision_identity("schedule", None, "3")] == "s3"
    assert index["schedule_by_node_index"][2] is None


def test_build_page_index_tells_a_pageable_schedule_from_a_same_numbered_section():
    # A pageable Schedule numbered the same as a real body section (rare,
    # but real -- see hierarchy.schedule_is_pageable's own examples) must
    # not be found by the section's page, or vice versa: they share the
    # bare (schedule=None, number) pair the un-keyed lookup used to use.
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3", "Definitions", "In this Act—"),
        make_node("schedule", "3", "Persons who may witness statements", "1 A police officer."),
    ]
    index = build_page_index(_parsed(nodes), "Test Act")

    section_page = index["by_key"][provision_identity("section", None, "3")]
    schedule_page = index["by_key"][provision_identity("schedule", None, "3")]
    assert section_page != schedule_page


_ENDNOTES = {
    "sections": [
        {"number": "1", "heading": "General information", "text": "See www.legislation.vic.gov.au.",
         "page_start": 1, "page_end": 1},
        {"number": "2", "heading": "Table of Amendments", "text": "This publication incorporates amendments.",
         "page_start": 2, "page_end": 2},
    ],
    "amending_acts": [
        {"title": "Amending Act 2009", "citation": "68/2009", "act_no": "68", "year": "2009",
         "is_statutory_rule": False, "fields": {"assent_date": "24.11.09"}},
        {"title": "Uncited Act 2011", "citation": "29/2011", "act_no": "29", "year": "2011",
         "is_statutory_rule": False, "fields": {"assent_date": "21.6.11"}},
    ],
}


def _act_with_endnotes():
    return {"nodes": _definitions_act(), "hierarchy": None, "endnotes": _ENDNOTES}


def test_render_endnotes_lists_each_amending_act_with_its_dates():
    summary = [{
        "citation": "68/2009",
        "record": {"title": "Amending Act 2009", "citation": "68/2009", "assent_date": "24.11.09",
                   "commencement_date": "1.1.10", "source": "endnotes"},
        "provisions": [{"label": "s. 3", "note": "S. 3 amended by No. 68/2009.", "section_number": "3"}],
        "count": 1,
    }]
    html = render_endnotes(_act_with_endnotes(), "Test Act", "/browse/a", summary)

    assert '<div class="amend" id="act-68-2009">' in html
    assert "<dt>Assent</dt><dd>24.11.09</dd>" in html
    assert "1 provision(s) in this Act" in html
    # a provision links to its own page
    assert '<a class="amend-prov" href="/browse/a/section/s3">s. 3</a>' in html


def test_render_endnotes_separates_acts_no_margin_note_cites():
    # In the printed table but never cited -- typically an amendment to a
    # provision since repealed. Shown, but not mixed in with the rest.
    html = render_endnotes(_act_with_endnotes(), "Test Act", "/browse/a", summary=[])

    assert "Also in the Table of Amendments (2)" in html
    assert 'id="act-29-2011"' in html


_BLOCK_ENDNOTES = {
    "sections": [{
        "number": "1", "heading": "General information", "page_start": 1, "page_end": 1,
        "text": "ignored when blocks are present",
        "blocks": [
            {"kind": "paragraph", "text": "See www.legislation.vic.gov.au."},
            {"kind": "heading", "text": "Style changes"},
            {"kind": "bullet", "text": "sections were renumbered;"},
            {"kind": "bullet", "text": "cross-references were updated."},
            {"kind": "paragraph", "text": "Section 64 reads as follows\u2014"},
            {"kind": "quote", "text": "64 How appeal is commenced"},
            {"kind": "quote", "text": "In section 255(5)(ab), insert a thing."},
        ],
    }],
    "amending_acts": [],
}


def test_render_endnotes_sets_each_block_as_the_printed_page_does():
    html = render_endnotes({"nodes": [], "hierarchy": None, "endnotes": _BLOCK_ENDNOTES}, "Test Act", "/browse/a")

    assert "<p>See www.legislation.vic.gov.au.</p>" in html
    assert '<div class="endnote-heading">Style changes</div>' in html


def test_render_endnotes_sets_a_run_of_bullets_as_one_list():
    html = render_endnotes({"nodes": [], "hierarchy": None, "endnotes": _BLOCK_ENDNOTES}, "Test Act", "/browse/a")

    assert html.count('<ul class="endnote-bullets">') == 1
    assert html.count("<li>") == 2


def test_render_endnotes_sets_a_run_of_quoted_lines_as_one_quotation():
    # A reproduced provision is one indented block on the printed page,
    # not a stack of unrelated paragraphs.
    html = render_endnotes({"nodes": [], "hierarchy": None, "endnotes": _BLOCK_ENDNOTES}, "Test Act", "/browse/a")

    assert html.count('<blockquote class="endnote-quote">') == 1
    assert "<p>64 How appeal is commenced</p><p>In section 255(5)(ab), insert a thing.</p></blockquote>" in html


def test_render_endnotes_falls_back_to_the_raw_text_without_blocks():
    # An Act parsed before the block builder existed: its stored text
    # still carries the source PDF's own wrap points, so those are
    # honoured rather than run together.
    html = render_endnotes(_act_with_endnotes(), "Test Act", "/browse/a", summary=[])

    assert 'class="endnote-text endnote-raw"' in html


def test_render_endnotes_returns_none_without_endnotes():
    # A Bill, an EM, or an Act parsed before endnotes were extracted.
    assert render_endnotes(_parsed(_definitions_act()), "Test Act", "/browse/a") is None


def test_render_index_links_to_the_endnotes_when_there_are_some():
    assert '/browse/a/endnotes' in render_index(_act_with_endnotes(), "Test Act", "/browse/a")
    assert "endnotes" not in render_index(_parsed(_definitions_act()), "Test Act", "/browse/a")


def test_a_margin_note_links_the_citation_where_it_stands():
    # The note itself is what the source prints in the margin; the
    # citation inside it is the link, and the Act's full name is the
    # tooltip -- not a second line spelling the Act out beside every note.
    nodes = _definitions_act()
    nodes[1]["history"] = [{"raw": "S. 3 amended by No. 68/2009 s. 51."}]
    index = build_amendment_index(_ENDNOTES, {})
    body = render_section({"nodes": nodes, "hierarchy": None}, "Test Act", "/browse/a", "s3", amendment_index=index)

    assert (
        'S. 3 amended by <a class="hist-act" href="/browse/a/endnotes#act-68-2009" '
        'title="Amending Act 2009 — assented 24.11.09">No. 68/2009</a> s. 51.'
    ) in body
    assert "Amending Act 2009</a>" not in body  # the name is in the tooltip, not the margin


def test_a_margin_note_without_an_index_still_links_to_the_legislation_resolver():
    # No amendment_index to check against at all -- still a link, not
    # inert text: the citation's shape is detected by pattern alone, and
    # /legislation/<act_no> is the standing address for exactly this case.
    nodes = _definitions_act()
    nodes[1]["history"] = [{"raw": "S. 3 amended by No. 68/2009 s. 51."}]
    body = render_section({"nodes": nodes, "hierarchy": None}, "Test Act", "/browse/a", "s3")

    assert '<a class="hist-act unresolved" href="/legislation/68-2009"' in body
    assert "S. 3 amended by" in body and "No. 68/2009</a> s. 51." in body


def test_the_index_states_which_version_this_parse_is():
    parsed = {
        "nodes": _definitions_act(), "hierarchy": None,
        "version": {"version": 114, "as_at": "2026-07-01", "as_at_printed": "1 July 2026",
                    "title": "Criminal Procedure Act 2009", "act_no": "7", "year": 2009},
    }
    html = render_index(parsed, "Criminal Procedure Act 2009", "/browse/cpa")

    # Never "the Authorised Version" -- that's the government's own
    # published text, and this is this pipeline's own reading of it.
    assert "Authorised Version" not in html
    assert "Version 114" in html
    assert "incorporating amendments as at 1 July 2026" in html


def test_an_unversioned_document_says_nothing_about_versions():
    # A Bill and an EM have no Authorised Version; an empty line reading
    # "Authorised Version No. None" would be worse than none at all.
    html = render_index({"nodes": _bill_nodes(), "hierarchy": None,
                         "version": {"version": None, "as_at": None, "as_at_printed": None}},
                        "Test Bill", "/browse/test-bill")

    assert "act-version" not in html


# ---------------------------------------------------------------------------
# A provision's history, and the superseded-version banner
#
# Which wordings a provision has had is lineage.py's to decide and is
# tested there; these check what render_history does with a chain of
# them, one of each shape.
# ---------------------------------------------------------------------------


def _wording(version, text, *, to=None, checked=True, ended=None, heading="Application of Division"):
    to = to or version
    nodes = [make_node("section", "366", heading, ""), make_node("subsection", "1", None, text)]
    nodes[0]["id"], nodes[1]["id"] = "s366", "s366/1"
    wording = {
        "absent": False,
        "from": {"version": version, "as_at_printed": f"{version - 100} April 2026"},
        "to": {"version": to, "as_at_printed": f"{to - 100} April 2026"},
        "versions": list(range(version, to + 1)), "version": to,
        "key": ("provision", None, "366"), "keys": {v: ("provision", None, "366") for v in range(version, to + 1)},
        "provision": {"heading": heading, "nodes": nodes}, "checked": checked,
    }
    if ended:
        wording["ended_by"] = {"version": to + 1, "as_at_printed": f"{to - 99} April 2026", **ended}
    return wording


def _history(*wordings, at=None):
    return {"wordings": list(wordings), "at": len(wordings) - 1 if at is None else at}


_AMENDED = {"change": "changed", "notes": ["S. 366 amended by No. 1/2026 s. 74."]}


def test_render_history_is_nothing_for_a_provision_with_one_wording():
    assert render_history(None, "/browse/cpa") == ("", "")
    assert render_history(_history(_wording(112, "alpha")), "/browse/cpa") == ("", "")


def test_render_history_offers_a_chip_and_every_wording_oldest_first():
    chip, body = render_history(_history(_wording(111, "alpha beta", ended=_AMENDED),
                                         _wording(112, "alpha gamma")), "/browse/cpa", anchor="s366")

    assert 'class="history-chip"' in chip and 'aria-controls="hist-s366"' in chip
    assert body.index("Version 111") < body.index("Version 112")
    assert 'data-at="1"' in body


def test_render_history_says_what_ended_a_wording_and_links_the_act():
    index = build_amendment_index(
        {"amending_acts": [{"citation": "1/2026", "title": "Justice Legislation Amendment Act 2026",
                            "act_no": "1", "year": 2026, "fields": {"assent_date": "10 Feb 2026"}}]},
    )
    _chip, body = render_history(_history(_wording(111, "alpha beta", ended=_AMENDED), _wording(112, "alpha")),
                                 "/browse/cpa", amendment_index=index)

    assert "Amended</span> at Version 112" in body
    assert 'class="hist-act"' in body and "/browse/cpa/endnotes#" in body


def test_render_history_links_a_note_the_index_cant_name_to_the_resolver():
    ended = {"change": "changed", "notes": ["S. 366 amended by No. 999/2026 s. 1."]}
    _chip, body = render_history(_history(_wording(111, "a", ended=ended), _wording(112, "b")),
                                 "/browse/cpa", amendment_index=build_amendment_index({}))

    assert '<a class="hist-act unresolved" href="/legislation/999-2026"' in body


def test_a_comparison_strikes_what_went_and_marks_what_arrived_in_its_subsection():
    _chip, body = render_history(_history(_wording(111, "alpha beta", ended=_AMENDED),
                                          _wording(112, "alpha gamma")), "/browse/cpa")

    compare = body.split('data-view="here" hidden>')[1].split("</div></section>")[0]
    assert '<del class="d-del">beta</del>' in compare
    assert '<ins class="d-ins">gamma</ins>' in compare
    assert '<span class="prov-num">(1)</span>' in compare


def test_a_comparison_reads_older_to_newer_from_either_side():
    """Red is always what Parliament removed, even viewed from the old page."""
    _chip, body = render_history(_history(_wording(111, "alpha beta", ended=_AMENDED),
                                          _wording(112, "alpha gamma"), at=0), "/browse/cpa")

    assert '<del class="d-del">beta</del>' in body
    assert '<del class="d-del">gamma</del>' not in body


def test_render_history_links_each_wording_to_its_own_version():
    _chip, body = render_history(_history(_wording(111, "a", ended=_AMENDED), _wording(112, "b")),
                                 "/browse/cpa", version_urls={111: "/browse/cpa-v111/section/s366"})

    assert '<a class="tl-version" href="/browse/cpa-v111/section/s366">' in body


def test_an_insertion_is_an_absent_wording_named_by_the_acts_own_word():
    absent = {"absent": True, "from": {"version": 110}, "to": {"version": 111}, "versions": [110, 111],
              "ended_by": {"version": 112, "change": "inserted", "notes": ["New s. 366 inserted by No. 1/2026 s. 83."]}}
    _chip, body = render_history(_history(absent, _wording(112, "a")), "/browse/cpa")

    assert "Not yet in the Act." in body
    assert "Inserted</span> at Version 112" in body


def test_an_unchecked_wording_says_so():
    _chip, body = render_history(_history(_wording(111, "a", checked=False, ended=_AMENDED), _wording(112, "b")),
                                 "/browse/cpa")

    assert body.count("Not yet checked") == 1


def test_a_section_with_history_carries_the_chip_beside_its_heading():
    nodes = [make_node("part", "1", "Preliminary"), make_node("section", "366", "Application", ""),
             make_node("subsection", "1", None, "alpha gamma")]
    history = _history(_wording(111, "alpha beta", ended=_AMENDED), _wording(112, "alpha gamma"))
    slug = build_page_index(_parsed(nodes), "Test Act")["by_node_index"][1]

    html = render_section(_parsed(nodes), "Test Act", "/browse/t", slug, timeline=history)

    assert '<div class="section-head"><h1>' in html and "history-chip" in html
    assert 'class="history"' in html


def test_render_superseded_banner_is_silent_on_the_current_version():
    assert render_superseded_banner(114, 114, "/browse/cpa-v114/") == ""


def test_render_superseded_banner_is_silent_without_version_context():
    # A Bill or EM: version is None, so there is nothing to be superseded by.
    assert render_superseded_banner(None, None, None) == ""


def test_render_superseded_banner_names_the_current_version_and_links_to_it():
    html = render_superseded_banner(112, 114, "/browse/cpa-v114/", as_at_printed="26 April 2026")

    assert "Version 112" in html
    assert "26 April 2026" in html
    assert 'href="/browse/cpa-v114/"' in html
    assert "Version 114" in html



# ---------------------------------------------------------------------------
# Linking a mention of another Act by name in body prose
#
# section 366 of the real Criminal Procedure Act reads "...an offence
# against section 21A(1) of the Crimes Act 1958 (stalking)" -- before this,
# "the Crimes Act 1958" sat there as plain text even though this pipeline
# has parsed the Crimes Act and knows exactly where to send a reader.
# ---------------------------------------------------------------------------

def test_a_known_acts_own_name_is_linked_in_body_prose(monkeypatch):
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {"crimes-act": "Crimes Act 1958"})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Stalking", "an offence against the Crimes Act 1958."),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s1")

    assert '<a href="/browse/crimes-act/">Crimes Act 1958</a>' in body


def test_a_capitalised_leading_the_is_not_part_of_the_link(monkeypatch):
    # "The Crimes Act 1958" at a sentence's own start is a valid Capitalised
    # word run in its own right, so the span pattern includes "The" in the
    # match -- but no real title is recorded with a leading "The", so the
    # lookup strips it first (matching link_targets.resolve_act_citation's
    # own convention for a reviewer-labelled citation span).
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {"crimes-act": "Crimes Act 1958"})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Stalking", "The Crimes Act 1958 governs this."),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s1")

    assert '<a href="/browse/crimes-act/">The Crimes Act 1958</a>' in body


def _provisions_of(body: str) -> str:
    """Just the text column of a section page. The outline beside it names
    the document and links to its own contents, so a whole-page search for
    "<Act name></a>" now finds that rather than a link in the prose."""
    return body.split('<div class="provisions">')[1]


def test_an_acts_own_name_is_not_linked_inside_its_own_pages(monkeypatch):
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {"crimes-act": "Crimes Act 1958"})
    monkeypatch.setattr(html_view_module, "load_act_registry", lambda: {"Crimes Act 1958": {"act_no": "6231", "year": "1958"}})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "This Crimes Act 1958 does this."),
    ]
    body = render_section(_parsed(nodes), "Crimes Act 1958", "/browse/crimes-act", "s1")

    provisions = _provisions_of(body)
    assert "Crimes Act 1958</a>" not in provisions
    assert "This Crimes Act 1958 does this." in provisions


def test_an_acts_own_name_is_excluded_from_the_registry_fallback_too(monkeypatch):
    # own_title is filtered out of known_acts *and* the registry
    # separately -- an Act not yet parsed here (so absent from
    # known_acts.yaml) must still not link its own name to itself via the
    # registry fallback.
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {})
    monkeypatch.setattr(html_view_module, "load_act_registry",
                        lambda: {"Sentencing Act 1991": {"act_no": "49", "year": "1991"}})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "This Sentencing Act 1991 does this."),
    ]
    body = render_section(_parsed(nodes), "Sentencing Act 1991", "/browse/sentencing-act", "s1")

    provisions = _provisions_of(body)
    assert "Sentencing Act 1991</a>" not in provisions
    assert "This Sentencing Act 1991 does this." in provisions


def test_an_act_in_the_general_registry_links_to_the_legislation_resolver(monkeypatch):
    # Not in known_acts.yaml (this pipeline hasn't parsed it), but a real
    # Act the general registry knows -- links to the standing resolver
    # rather than sitting as plain text, the same treatment an unresolved
    # margin-note citation gets (see _linked_citation_html).
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {})
    monkeypatch.setattr(html_view_module, "load_act_registry",
                        lambda: {"Public Administration Act 2004": {"act_no": "108", "year": "2004"}})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Heading", "under the Public Administration Act 2004."),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s1")

    assert '<a class="unresolved" href="/legislation/108-2004"' in body
    assert "Public Administration Act 2004</a>" in body


def test_an_act_in_neither_source_is_left_as_plain_text(monkeypatch):
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {})
    monkeypatch.setattr(html_view_module, "load_act_registry", lambda: {})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Heading", "under the Public Administration Act 2004."),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s1")

    assert "Public Administration Act 2004</a>" not in body
    assert "Public Administration Act 2004" in body


def test_a_run_on_sentence_does_not_get_swallowed_into_a_false_act_name(monkeypatch):
    # The whole reason the span pattern requires every word to be
    # Capitalised or a connector: ordinary sentence prose must not be
    # captured as if it were one long Act title. A registry entry for
    # ordinary lowercase sentence words would prove nothing (they'd never
    # even reach the "is this a real Act" check) -- what has to be shown
    # is that the leading, lowercase-heavy run of the sentence is excluded
    # from the match at all, not merely that the match then fails to
    # resolve.
    import corpus.publishing.html_view as html_view_module
    monkeypatch.setattr(html_view_module, "load_known_acts", lambda: {})
    monkeypatch.setattr(html_view_module, "load_act_registry", lambda: {})
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Heading",
                 "a person authorised by or under section 229 of the Transport (Compliance and Miscellaneous) Act 1983."),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s1")

    assert "a person authorised by or under section 229 of the" in body
    # Ordinary lowercase sentence words must never end up as a link's own
    # text -- that would mean the actref pattern swallowed them into what
    # it thought was an Act title.
    linked_text = re.findall(r'<a\b[^>]*>([^<]*)</a>', body)
    assert not any("person" in t or "authorised" in t for t in linked_text)


# The "Copy section" button. Its JavaScript reads the rendered page and
# writes two clipboard flavours, so what it produces can only really be
# judged in a browser -- and was: both flavours were read back out of a
# real clipboard on the Interpretation of Legislation Act's s3 (defined
# terms) and s14 (subsections nested two deep), confirming the
# "(1) / (a) / (i)" progression arrives in Word and Docs as 0pt / 36pt /
# 72pt paragraph indents. What is worth pinning down here is the wiring
# either side of that: the button exists on a section page, the script
# that gives it behaviour is shipped with every page, and the indent step
# stays one Word tab stop.
def test_a_section_page_offers_a_copy_button():
    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3")

    assert 'class="copy-section"' in body
    # Before the provisions, so tabbing into the page reaches it without
    # first walking the whole section.
    assert body.index('class="copy-section"') < body.index('<div class="provisions">')


def test_every_copy_button_on_a_read_on_page_works():
    """Reading on brings further sections in, each with its own button.
    A listener bound by id at load reached only the first, so the rest
    did nothing; and the first read every section on the page."""
    from corpus.publishing.html_view import template_text

    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3")
    copy_js = template_text("copy.js")

    assert "id=" not in body[body.index('class="copy-section"') - 40:body.index("Copy section</button>")]
    assert "getElementById" not in copy_js
    assert 'document.addEventListener("click"' in copy_js
    assert 'btn.closest(".reader-section")' in copy_js


def test_a_copied_button_says_so():
    from corpus.publishing.html_view import template_text

    assert 'classList.toggle("copied"' in template_text("copy.js")
    assert ".copy-section.copied" in template_text("page.css")


def test_the_copy_script_ships_with_the_page():
    from corpus.publishing.html_view import asset_version, page_shell

    page = page_shell("Test Act", render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3"))

    assert 'class="copy-section"' in page
    assert f'<script src="/assets/copy.js?v={asset_version()}"></script>' in page


def test_one_level_of_nesting_is_one_word_tab_stop():
    """36pt is half an inch -- the default tab stop in both Word and
    Google Docs, so a copied paragraph lands where a reader's own tab
    key would have put it."""
    from corpus.publishing.html_view import template_text

    assert "var INDENT_PT = 36;" in template_text("copy.js")


def test_a_defined_term_keeps_its_own_punctuation_tight():
    """"amended, in relation to ..." -- the term runs into a comma, and
    the copy must not open a gap the page itself doesn't show. Both
    clipboard flavours go through the same rule, which is why there is
    one helper rather than two spellings of it."""
    from corpus.publishing.html_view import template_text

    copy_js = template_text("copy.js")
    assert copy_js.count("function gap(") == 1
    assert copy_js.count("gap(p.text)") == 2


# The page template. Since static/site/page.html is a file rather than a
# string constant, the thing most likely to break is the seam between the
# two: a placeholder renamed in one and not the other leaves a literal
# "{{...}}" in a published page, which no amount of CSS review would
# catch.
def _shell(**kwargs) -> str:
    from corpus.publishing.html_view import page_shell

    return page_shell("Test Act", "<p>body</p>", **kwargs)


def test_no_placeholder_survives_into_a_rendered_page():
    for kwargs in ({}, {"base_url": "/browse/a"}, {"base_url": "/browse/a", "reader": True},
                   {"previewbar_html": '<div class="previewbar">preview</div>'}):
        page = _shell(**kwargs)
        assert "{{" not in page and "}}" not in page, (kwargs, page)


def test_asset_urls_stay_inside_a_project_site():
    """On GitHub Pages the site is served under /<repo>/, so an asset URL
    that assumed the domain root would reach for the real root instead."""
    from corpus.publishing.html_view import asset_version

    page = _shell(base_url="/corpusvic/browse/a")

    v = asset_version()
    assert f'<link rel="stylesheet" href="/corpusvic/assets/tokens.css?v={v}">' in page
    assert f'<script src="/corpusvic/assets/reader.js?v={v}"></script>' in page
    assert '<link rel="preload" href="/corpusvic/assets/fonts/Junicode-Roman.woff2"' in page
    # Junicode is reached relative to tokens.css, so no prefix belongs in
    # the stylesheet itself -- that is what makes one file serve both.
    from corpus.publishing.html_view import template_text

    assert "url('fonts/Junicode-Roman.woff2')" in template_text("tokens.css")


def test_a_section_page_asks_for_the_reader_layout():
    assert 'class="page page-reader"' in _shell(base_url="/browse/a", reader=True)
    assert 'class="page"' in _shell(base_url="/browse/a")


def test_the_template_is_read_again_after_it_changes(tmp_path, monkeypatch):
    """Editing a stylesheet or the shell while a server is running has to
    show up on the next reload -- that is most of the reason the template
    is a file at all."""
    import corpus.publishing.html_view as html_view_module

    monkeypatch.setattr(html_view_module, "TEMPLATE_DIR", tmp_path)
    monkeypatch.setattr(html_view_module, "_template_cache", {})
    page = tmp_path / "page.html"
    page.write_text("first {{BODY}}", encoding="utf-8")
    assert html_view_module.page_shell("T", "x") == "first x"
    # A rewrite within the same clock tick has to be noticed too, so the
    # mtime is moved explicitly rather than trusted to differ.
    page.write_text("second {{BODY}}", encoding="utf-8")
    import os

    os.utime(page, (0, 0))
    assert html_view_module.page_shell("T", "x") == "second x"


# ---------------------------------------------------------------------------
# The section reading view
# ---------------------------------------------------------------------------
def _three_part_act() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("section", "2", "Commencement", "This Act comes into operation—"),
        make_node("part", "2", "Offences"),
        make_node("division", "1", "Assault"),
        make_node("section", "10", "Common assault", "A person must not—"),
        make_node("section", "11", "Aggravated assault", "A person must not—"),
        make_node("division", "2", "Theft"),
        make_node("section", "20", "Theft", "A person must not—"),
        make_node("part", "3", "Enforcement"),
        make_node("section", "30", "Powers", "An officer may—"),
    ]


def _index_outline() -> str:
    body = render_index(_parsed(_three_part_act()), "Test Act", "/browse/a")
    return body.split('<nav class="outline"')[1].split("</nav>")[0]


def test_a_provision_page_is_the_provision_and_nothing_beside_it():
    """The outline used to sit beside every provision. A reader on one is
    reading it, and the breadcrumb, the next/previous links and reading on
    already reach everywhere the outline did."""
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s10")

    assert '<nav class="outline"' not in body
    assert '<div class="reader-cols">' not in body, "one column, not two"
    # What a reader does need on a provision is still there.
    assert 'class="breadcrumb"' in body
    assert '<nav class="section-nav"' in body


def test_the_contents_carry_the_acts_skeleton():
    """Moved here, where a contents list running to hundreds of provisions
    is worth being able to move around."""
    outline = _index_outline()

    assert "Part 1 - Preliminary" in outline
    assert "Part 2 - Offences" in outline
    assert "Division 1 - Assault" in outline
    assert "Part 3 - Enforcement" in outline


def test_the_skeleton_scrolls_the_contents_rather_than_leaving_them():
    """The whole point of it being here: following a line moves the page
    you are on, instead of navigating into a provision."""
    outline = _index_outline()

    assert 'href="#' in outline
    assert "/browse/a/section/" not in outline, "no line here leaves the contents"


def test_the_skeleton_does_not_list_the_provisions_themselves():
    """They are already in the column beside it, one click from their own
    page. Listing them here would put the page beside itself -- and the
    Criminal Procedure Act alone would put over a thousand links in it."""
    outline = _index_outline()

    assert "10 Common assault" not in outline
    assert "11 Aggravated assault" not in outline


def test_the_skeleton_names_the_document():
    assert ">Test Act</a>" in _index_outline()


def _dead_anchors(body: str) -> list:
    """Lines in the outline pointing at an id the page does not have."""
    outline = body.split('<nav class="outline"')[1].split("</nav>")[0]
    ids = set(re.findall(r'id="([^"]+)"', body))
    return [a[1:] for a in re.findall(r'href="(#[^"]+)"', outline) if a[1:] not in ids]


def test_every_line_in_the_skeleton_lands_somewhere():
    """An anchor that scrolls nowhere looks like the page is broken."""
    assert _dead_anchors(render_index(_parsed(_three_part_act()), "Test Act", "/browse/a")) == []


def test_every_entry_in_the_contents_is_anchorable():
    """Each provision's place in the contents is addressed by the same
    name its own page is, so the two cannot drift apart."""
    body = render_index(_parsed(_three_part_act()), "Test Act", "/browse/a")

    assert '<li id="s10"><a href="/browse/a/section/s10">' in body


def test_a_flat_act_gets_no_empty_column():
    """An Act with no Parts has no skeleton to show, and an empty column
    beside the contents is worse than no column."""
    nodes = [make_node("section", "1", "Purposes", "The purposes of this Act are—")]
    body = render_index(_parsed(nodes), "Flat Act", "/browse/a")

    assert '<nav class="outline"' not in body
    assert '<div class="reader-cols">' not in body


def test_the_nearby_provisions_are_named():
    """"Next" alone makes a reader click to find out where they are
    going."""
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s10")
    nav = body.split('<nav class="section-nav"')[1]

    assert "2 Commencement" in nav      # the previous section, across a Part boundary
    assert "11 Aggravated assault" in nav
    assert 'href="/browse/a/section/s2"' in nav and 'href="/browse/a/section/s11"' in nav


def test_the_first_provision_has_no_previous():
    nav = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s1").split(
        '<nav class="section-nav"')[1]

    assert "nav-prev" not in nav
    assert "nav-next" in nav


def test_the_reading_bar_states_the_date_the_text_is_as_at():
    parsed = dict(_parsed(_three_part_act()), version={"version": 24, "as_at_printed": "1 March 2024"})
    body = render_section(parsed, "Test Act", "/browse/a", "s10")

    assert "Text as at" in body
    assert "<strong>1 March 2024</strong>" in body


def test_a_document_with_no_version_states_nothing_rather_than_guessing():
    """A Bill and an Explanatory Memorandum have no "as at" date at all,
    and the date a file happened to be parsed is not one."""
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s10")

    assert "Text as at" not in body
    assert 'class="readerctl"' in body, "the reading controls are not version-dependent"


def test_comparing_versions_offers_the_other_versions_of_this_provision():
    parsed = dict(_parsed(_three_part_act()), version={"version": 113, "as_at_printed": "1 March 2024"})
    body = render_section(
        parsed, "Test Act", "/browse/a", "s10",
        version_urls={112: "/browse/act-v112/section/s10", 113: "/browse/act-v113/section/s10",
                      114: "/browse/act-v114/section/s10"},
        version_dates={112: "1 January 2023", 113: "1 March 2024", 114: "1 July 2025"},
    )
    choices = body.split('<details class="versions">')[1].split("</details>")[0]

    # Newest first, and the version you are already reading is not offered.
    assert choices.index("Version 114") < choices.index("Version 112")
    assert "Version 113" not in choices
    # Dated, because a reader has a date in mind rather than a version number.
    assert "as at 1 July 2025" in choices
    # The same provision in that version, not that version's front page.
    assert 'href="/browse/act-v114/section/s10"' in choices


def test_a_document_held_in_one_version_offers_no_comparison():
    """A control with nothing behind it is worse than no control."""
    parsed = dict(_parsed(_three_part_act()), version={"version": 1, "as_at_printed": "1 March 2024"})
    body = render_section(parsed, "Test Act", "/browse/a", "s10",
                          version_urls={1: "/browse/a/section/s10"})

    assert "Compare with another version" not in body
    # And nothing is claimed to be current when there is nothing to be
    # current against.
    assert "asat-tag" not in body


def test_the_templates_own_comments_do_not_ship():
    """page.html's comments are notes to whoever edits it. A public
    register of the law has no use for them on every page, and an editor
    should not have to weigh that before writing one."""
    page = _shell(base_url="/browse/a")

    assert "MAIN_CLASS" not in page
    assert "<!--" not in page


def test_a_comment_in_the_page_itself_is_left_alone():
    """Only the template's own comments go: the body is the document's
    text, and a provision that quotes one is still quoting it."""
    page = _shell(base_url="/browse/a")
    from corpus.publishing.html_view import page_shell

    assert "<!-- kept -->" in page_shell("T", "<p>a <!-- kept --> note</p>")


def test_the_contents_read_the_same_for_every_provision():
    """Every provision the parser found is published with its text, so
    nothing in the contents is held back and nothing is marked as held
    back. What a reader needs to know -- whether a human has checked the
    provision in front of them -- is a fact about that provision, said on
    it (see render_section's `notice`), not a mark beside its neighbours
    in a list."""
    body = render_index(_parsed(_three_part_act()), "Test Act", "/browse/a")

    assert '<a href="/browse/a/section/s11">' in body
    assert "not yet published" not in body


def test_a_notice_is_set_above_the_provisions_own_heading():
    """It qualifies every word below it, so it has to be read before
    them rather than found after them."""
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s10",
                          notice='<div class="disclaimer">Not checked.</div>')
    main = body.split('<div class="reader-main">')[1]

    assert 'class="disclaimer">Not checked.' in main
    assert main.index("Not checked.") < main.index("<h1>")


def test_no_notice_leaves_the_page_alone():
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s10")
    assert "disclaimer" not in body


# ---------------------------------------------------------------------------
# A provision's own box
# ---------------------------------------------------------------------------
# The number used to be hung in the margin with a negative text-indent,
# which put it outside the div by construction: on a section page it was
# painted over the outline beside it, further over the larger the reading
# size. The markup is now two elements in two grid columns, and what these
# pin down is that nothing reintroduces an offset that can leak.
def test_a_numbered_provision_keeps_its_number_in_its_own_element():
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "10", "Common assault", "A person must not—"),
        make_node("paragraph", "a", None, "do a thing; or"),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s10")

    # The number and the words are two elements, both inside the
    # provision's own div -- nothing is left to position outside it.
    assert ('<div class="prov prov-paragraph" id="a" style="--depth:0">'
            '<span class="prov-num">(a)</span>'
            '<span class="prov-text">do a thing; or</span></div>') in body


def test_a_provision_with_no_number_still_has_a_text_element():
    """It is what puts the words in the second column -- without it they
    would start under the numbers rather than beside them."""
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "This Act has purposes."),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s1")

    assert '<span class="prov-text">This Act has purposes.</span>' in body


def test_a_defined_terms_own_words_stay_with_its_text():
    """A term is not a number in a margin: it is the first words of its
    own sentence, so it belongs in the text column, not the number's."""
    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3")

    assert '<span class="prov-text"><span class="prov-term">accused</span> means a person' in body
    # And the tight-punctuation rule still applies inside it.
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "3", "Definitions", "In this Act—"),
        make_node("definition", None, "amended", ", in relation to an instrument, includes varied;"),
    ]
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s3")
    assert '<span class="prov-term">amended</span>, in relation to' in body


def test_nothing_in_a_provision_is_positioned_outside_it():
    """The structural guarantee, asserted against the stylesheet itself:
    a negative offset on a provision is what let the number escape, and a
    grid column is what replaced it."""
    from corpus.publishing.html_view import template_text

    import re

    # Comments only explain the rules; it is the rules themselves that
    # have to be clean of it (this note's own prose included).
    css = re.sub(r"/\*.*?\*/", "", template_text("page.css"), flags=re.S)
    prov_rule = css.split(".prov {")[1].split("}")[0]

    assert "display: grid" in prov_rule
    assert "text-indent" not in css, "a hanging indent can paint outside the box it belongs to"


def test_an_act_offers_its_bill_and_em_from_its_own_contents():
    """Each named, and nothing else. These used to carry a sentence
    apiece explaining which was which, because both read "Crimes Bill
    1957": an EM's front matter names the Bill it is about, so the two
    titles were identical and only the gloss told them apart. The EM says
    what it is in its own name now (see dashboard._named_as_an_em), and a
    sentence explaining a link that explains itself is noise."""
    related = [
        {"slug": "crimes-bill", "kind": "bill", "title": "Crimes Bill 1957",
         "href": "/browse/crimes-bill/"},
        {"slug": "crimes-bill-em", "kind": "em",
         "title": "Crimes Bill 1957 \u2014 Explanatory Memorandum",
         "href": "/browse/crimes-bill-em/"},
    ]
    page = render_index(_parsed(_definitions_act()), "Crimes Act 1958", "/browse/a", related=related)

    assert "Related documents" in page
    assert 'href="/browse/crimes-bill/">Crimes Bill 1957</a>' in page
    assert "Crimes Bill 1957 \u2014 Explanatory Memorandum</a>" in page
    assert "the Bill it was enacted from" not in page
    assert "written about that Bill" not in page
    # Nothing at all where there is no related document to offer.
    assert "Related documents" not in render_index(_parsed(_definitions_act()), "A", "/browse/a")


# ---------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------

def _table_act() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "7A", "Time limits removed", ""),
        make_node("subsection", "3", None, "...described in column 1 of the Table..."),
        make_node(
            "table", None, "Table",
            "Column 1 | Column 2\n"
            "An offence against a child | A defence under section 45(4)\n"
            "An offence against a 16 year old | A defence under section 48(2)",
        ),
    ]


def test_a_table_renders_as_a_table():
    """Its rows are stored as text, which is what makes it editable in
    review. Here they have to go back to being columns: "An offence
    against a child" means nothing without the defence printed beside
    it."""
    body = render_section(_parsed(_table_act()), "Test Act", "/browse/a", "s7a")

    assert '<div class="prov prov-table"' in body
    assert "<th>Column 1</th><th>Column 2</th>" in body
    assert "<td>An offence against a child</td>" in body
    assert "<caption>Table</caption>" in body
    # The stored form is never what a reader sees.
    assert "Column 1 | Column 2" not in body


def test_a_tables_cells_are_linkified_like_any_other_text():
    nodes = _definitions_act()
    # Inside section 3, not after the section that follows it.
    nodes.insert(-1, make_node("table", None, None, "Term | Meaning\nthe accused | a person charged"))
    body = render_section(_parsed(nodes), "Test Act", "/browse/a", "s3")

    assert re.search(r"<td>[^<]*<a[^>]*>accused</a>[^<]*</td>", body), (
        "a defined term inside a cell should link the same way it does in prose"
    )


# ---------------------------------------------------------------------------
# The document-context cache
# ---------------------------------------------------------------------------
# Every page and every hover card of a document needs the same derived
# structure, and building it is most of what rendering a site costs. It
# is cached on the identity of the node list, which is fast and which
# fails in exactly one way worth pinning down: handing back one
# document's structure for another.

def test_two_documents_do_not_share_a_context():
    """The cache is keyed on identity, so the failure to rule out is a
    page of one Act rendered from another Act's tree."""
    from corpus.publishing.html_view import _build_context

    a = {"nodes": [make_node("section", "1", "Alpha", "text of alpha")], "hierarchy": None}
    b = {"nodes": [make_node("section", "1", "Beta", "text of beta")], "hierarchy": None}

    ctx_a = _build_context(a, "Act A")
    ctx_b = _build_context(b, "Act B")

    assert ctx_a is not ctx_b
    assert ctx_a["sections"][0][0]["node"]["heading"] == "Alpha"
    assert ctx_b["sections"][0][0]["node"]["heading"] == "Beta"
    # And the first is still itself, not overwritten by the second.
    assert _build_context(a, "Act A")["sections"][0][0]["node"]["heading"] == "Alpha"


def test_the_same_document_is_built_once():
    from corpus.publishing.html_view import _build_context

    parsed = {"nodes": [make_node("section", "1", "Alpha", "text")], "hierarchy": None}

    assert _build_context(parsed, "Act A") is _build_context(parsed, "Act A")


def test_the_same_nodes_under_a_different_title_are_built_again():
    """The title is part of what the context derives (index anchors are
    computed against it), so it has to be part of the key."""
    from corpus.publishing.html_view import _build_context

    parsed = {"nodes": [make_node("section", "1", "Alpha", "text")], "hierarchy": None}

    assert _build_context(parsed, "Act A") is not _build_context(parsed, "Act B")


def test_the_cache_does_not_grow_without_bound():
    """It holds whole document trees, and a site build walks fifteen of
    them. An unbounded cache would keep every one alive at once."""
    from corpus.publishing import html_view

    kept = [{"nodes": [make_node("section", "1", f"Doc {i}", "text")], "hierarchy": None}
            for i in range(html_view._CONTEXT_CACHE_MAX + 3)]
    for i, parsed in enumerate(kept):
        html_view._build_context(parsed, f"Act {i}")

    assert len(html_view._CONTEXT_CACHE) <= html_view._CONTEXT_CACHE_MAX


def test_a_rendered_page_is_the_same_whether_or_not_the_cache_was_warm():
    from corpus.publishing import html_view

    parsed = _parsed(_three_part_act())
    html_view._CONTEXT_CACHE.clear()
    cold = render_section(parsed, "Test Act", "/browse/a", "s10")
    warm = render_section(parsed, "Test Act", "/browse/a", "s10")

    assert cold == warm


# ---------------------------------------------------------------------------
# A Schedule in the outline
# ---------------------------------------------------------------------------
# A Schedule whose content is prose rather than numbered clauses is a
# page in its own right (hierarchy.schedule_is_pageable), and it sits at
# the top level of the tree beside the Parts. The outline matched it as a
# leaf with no check that it was anywhere near the page being read, so it
# appeared in the sidebar of every section page of the document -- all
# 620 of the Criminal Procedure Act's. Deeper leaves were never wrong:
# they are only reached by recursing into an open Part or Division, and
# the top level had no such gate.

def _act_with_a_prose_schedule() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("section", "2", "Commencement", "This Act comes into operation—"),
        make_node("schedule", "3", "Persons who may witness statements"),
        make_node("repealed", None, None, "* * * * *"),
    ]


def test_a_prose_schedule_is_a_page_of_its_own_in_the_contents():
    """A Schedule whose content is prose rather than numbered clauses is
    addressed as a page (hierarchy.schedule_is_pageable), so the contents
    link to it like any other provision."""
    body = render_index(_parsed(_act_with_a_prose_schedule()), "Test Act", "/browse/a")

    assert "Persons who may witness" in body
    assert "/browse/a/section/" in body


def test_the_skeleton_anchors_a_prose_schedule_to_its_contents_entry():
    """It is an entry in the list, not a heading over one, so the heading
    anchor the outline would otherwise use is never rendered. Linking to
    it anyway left 28 lines across 13 documents scrolling nowhere -- the
    Schedules, and every heading inside one, which the contents do not
    list either."""
    body = render_index(_parsed(_act_with_a_prose_schedule()), "Test Act", "/browse/a")

    assert _dead_anchors(body) == []
    outline = body.split('<nav class="outline"')[1].split("</nav>")[0]
    assert "Persons who may witness" in outline, "still named in the skeleton"


def test_a_flat_acts_sections_are_all_in_its_contents():
    """An Act whose sections hang straight off the root still lists every
    one of them, with no Parts to group them under."""
    nodes = [
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("section", "2", "Commencement", "This Act comes into operation—"),
    ]
    body = render_index(_parsed(nodes), "Flat Act", "/browse/a")

    assert "1 Purposes" in body
    assert "2 Commencement" in body


# ---------------------------------------------------------------------------
# A provision this version no longer has
# ---------------------------------------------------------------------------


def _ghost():
    absent = {"absent": True, "from": {"version": 113}, "to": {"version": 114}, "versions": [113, 114]}
    history = _history(_wording(112, "an offence", ended={"change": "repealed",
                                                          "notes": ["S. 366 repealed by No. 9/2026 s. 4."]}),
                       absent, at=1)
    return {"page": "s366", "after_page": "s365", "label": "Section 366", "heading": "Old offence",
            "history": history}


def test_the_contents_list_a_removed_provision_where_it_used_to_sit():
    nodes = [make_node("part", "1", "Preliminary"), make_node("section", "365", "Before", "a"),
             make_node("section", "367", "After", "b")]
    parsed = _parsed(nodes)
    pages = build_page_index(parsed, "Test Act")["by_node_index"]
    ghost = {**_ghost(), "after_page": pages[1]}

    html = render_index(parsed, "Test Act", "/browse/t", ghosts=[ghost])

    assert html.index(f'id="{pages[1]}"') < html.index('class="ghost"') < html.index(f'id="{pages[2]}"')
    assert 'href="/browse/t/section/s366"' in html and "Repealed" in html


def test_a_removed_provisions_page_is_its_history_opened():
    from corpus.publishing.html_view import render_ghost

    html = render_ghost(_ghost(), "Test Act", "/browse/t", version={"version": 114})

    assert "Section 366 [Repealed]" in html
    assert "not in Version 114" in html and "removed at Version 113" in html
    assert 'class="reader-section historical"' in html
    assert "<details class=\"history\"" in html and " open>" in html
    assert "an offence" in html and "S. 366 repealed by" in html


# -- History per piece, opened from the piece's own margin notes ---------------

_APPEAL_NOTE = 'S. 3 def. of "appeal" amended by No. 1/2020 s. 4.'
_OLD_NOTE = 'S. 3 def. of "old term" repealed by No. 2/2021 s. 5.'


def _definitions(version, appeal, *, old=True, to=None, ended=None, noted=()):
    """s 3 in one version: "accused" as ever, "appeal" as given, and
    "old term" until it is repealed. `noted` are the terms whose margin
    note this version prints."""
    nodes = [make_node("section", "3", "Definitions", ""), make_node("subsection", "1", None, "In this Act—"),
             make_node("definition", None, "accused", "means a person charged"),
             make_node("definition", None, "appeal", appeal),
             make_node("paragraph", "a", None, "an appeal proper; or")]
    ids = ["s3", "s3/1", "s3/1/accused", "s3/1/appeal", "s3/1/appeal/a"]
    if old:
        nodes.append(make_node("definition", None, "old term", "means something since dropped"))
        ids.append("s3/1/old-term")
    for node, name in zip(nodes, ids):
        node["id"] = name
        node["path"] = {"section": "3", "subsection": "1"}
    if "appeal" in noted:
        nodes[3]["history"] = [{"raw": _APPEAL_NOTE}]
    wording = _wording(version, "", to=to, ended=ended)
    wording["provision"] = {"heading": "Definitions", "nodes": nodes}
    return wording


def _s3_history():
    return _history(
        _definitions(110, "means a hearing", ended={"change": "changed", "notes": [_APPEAL_NOTE]}),
        _definitions(111, "includes—", noted={"appeal"}, ended={"change": "changed", "notes": [_OLD_NOTE]}),
        _definitions(112, "includes—", old=False, noted={"appeal"}))


def test_a_piece_has_a_history_of_its_own():
    from corpus.publishing.html_view import piece_history

    appeal = piece_history(_s3_history(), "1/appeal")
    assert [(w["from"]["version"], w["to"]["version"]) for w in appeal["wordings"]] == [(110, 110), (111, 112)], \
        "versions 111 and 112 read the same, so are one"
    assert appeal["wordings"][0]["ended_by"]["notes"] == [_APPEAL_NOTE], "only the note printed against it"
    assert appeal["at"] == 1

    old = piece_history(_s3_history(), "1/old-term")
    assert [w["absent"] for w in old["wordings"]] == [False, True]
    assert old["wordings"][0]["ended_by"] == {**old["wordings"][0]["ended_by"], "change": "repealed",
                                              "notes": [_OLD_NOTE]}

    assert piece_history(_s3_history(), "1/accused") is None, "it never changed"


def test_a_margin_note_opens_its_pieces_history_in_its_place():
    """A long section is hard to compare whole; the note the Act prints
    beside the piece that changed opens that piece's history, set where
    the piece is -- the rest of the section left as it reads -- with each
    step between neighbouring wordings side by side, what went struck
    through on the left and what arrived marked on the right."""
    history = _s3_history()
    nodes = [make_node("part", "1", "Preliminary"), *[dict(n) for n in history["wordings"][-1]["provision"]["nodes"]]]
    nodes[0]["id"] = "pt1"
    slug = build_page_index(_parsed(nodes), "Test Act")["by_node_index"][1]

    html = render_section(_parsed(nodes), "Test Act", "/browse/t", slug, timeline=history)

    notes = re.search(r'<div class="prov-notes" data-hist="(piece-[^"]+)"[^>]*>.*?amended', html, re.S)
    assert notes, "the note is the button"
    anchor = notes.group(1)[len("piece-"):]
    start = html.find(f'<div class="piece-hist" id="piece-{anchor}"')
    row = html.find(f'<div class="prov prov-definition" id="appeal" data-in="{anchor}"')
    assert 0 <= start < row, "set just before the piece's own row, in its place; its rows are what opening it hides"
    body = html[start:row]
    assert "The definition of \u201cappeal\u201d" in body
    assert re.findall(r'class="ph-point[^"]*"[^>]*>([^<]+)<', body) == ["Version 110", "Current"]
    assert body.count('class="ph-step"') == 1, "one step for two wordings"
    old_side, new_side = re.search(r'class="ph-side ph-old">(.*?)class="ph-side ph-new">(.*)', body, re.S).groups()
    assert '<del class="d-del">means a hearing</del>' in old_side and "d-ins" not in old_side
    assert '<ins class="d-ins">includes' in new_side and "d-del" not in new_side
    assert "history-chip" in html, "and the whole section's history is still there"


def test_a_piece_inserted_later_reads_not_in_the_act_before_it():
    from corpus.publishing.html_view import render_piece_history

    absent = {"absent": True, "from": {"version": 1}, "to": {"version": 1}, "versions": [1]}
    present = {"absent": False, "from": {"version": 2}, "to": {"version": 2}, "versions": [2], "version": 2,
               "checked": True, "provision": {"heading": None, "nodes": [
                   {"type": "section", "number": "3", "_node_id": "s3", "text": ""},
                   {"type": "subsection", "number": "2", "_node_id": "s3/2", "text": "new words"}]}}
    html = render_piece_history({"wordings": [absent, present], "at": 1}, "(2)", "s3-2", "/browse/t")

    assert "Not in the Act at Version 1." in html
    assert '<ins class="d-ins">new words</ins>' in html, "arrived whole: all of it marked"
    assert '<div class="prov prov-section"' not in html, "the provision's own number is not the piece's"
