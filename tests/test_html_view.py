"""Tests for corpus/html_view.py's pure rendering logic -- chiefly
render_preview, which decides what one hover card gets to show. The
dashboard endpoint that serves it, the section/index page renderers and
the browser-side hover behaviour itself were exercised end to end against
real parsed Act data and a real browser session instead."""
import re

from corpus.amendments import build_amendment_index
from corpus.diffing import provision_identity
from corpus.html_view import (
    build_page_index,
    render_endnotes,
    render_index,
    render_preview,
    render_section,
    render_superseded_banner,
    render_timeline,
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


def test_render_section_renders_the_explained_in_chips():
    crossrefs = [
        {"kind": "bill", "label": "Bill clause 5", "href": "/browse/b/section/c5", "title": "the clause"},
        {"kind": "em", "label": "EM on clause 5", "href": "/browse/b-em/section/c5"},
    ]
    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3", crossrefs=crossrefs)

    assert '<span class="crossrefs-label">Explained in</span>' in body
    assert '<a class="crossref crossref-bill" href="/browse/b/section/c5" title="the clause">Bill clause 5</a>' in body
    assert '<a class="crossref crossref-em" href="/browse/b-em/section/c5">EM on clause 5</a>' in body


def test_render_section_without_crossrefs_renders_no_bar():
    body = render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3")

    assert "crossrefs" not in body


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
# A provision's timeline, and the superseded-version banner
#
# The comparison logic itself belongs to diffing.py and is tested there;
# these check what render_timeline and render_superseded_banner do with
# what diffing hands them, since that's the boundary a live check can't
# easily exercise for every case (the real Act only has 13 changes to
# look at, not one of each shape).
# ---------------------------------------------------------------------------


def _changed_entry(**overrides) -> dict:
    entry = {
        "key": ("provision", None, "366"), "kind": "provision", "type": "section",
        "number": "366", "schedule": None, "heading": "Application of Division",
        "version": 112, "as_at": "2026-04-26", "as_at_printed": "26 April 2026",
        "change": "changed",
        "diff": [{"op": "equal", "text": "alpha"}, {"op": "delete", "text": "beta"},
                 {"op": "insert", "text": "gamma"}],
        "new_history": ["S. 366 amended by No. 1/2026 s. 74."],
    }
    entry.update(overrides)
    return entry


def test_render_timeline_is_empty_for_a_provision_with_no_history():
    assert render_timeline([], "/browse/cpa") == ""


def test_render_timeline_names_the_newest_change_first():
    older = _changed_entry(version=111, as_at_printed="1 April 2026")
    newer = _changed_entry(version=112, as_at_printed="26 April 2026")
    html = render_timeline([older, newer], "/browse/cpa")

    assert html.index("Version 112") < html.index("Version 111")
    assert "2 changes" in html


def test_render_timeline_marks_deletions_and_insertions():
    html = render_timeline([_changed_entry()], "/browse/cpa")

    assert '<del class="d-del">beta</del>' in html
    assert '<ins class="d-ins">gamma</ins>' in html


def test_render_timeline_links_the_amending_act_named_in_the_note():
    index = build_amendment_index(
        {"amending_acts": [{"citation": "1/2026", "title": "Justice Legislation Amendment Act 2026",
                            "act_no": "1", "year": 2026, "fields": {"assent_date": "10 Feb 2026"}}]},
    )
    html = render_timeline([_changed_entry()], "/browse/cpa", amendment_index=index)

    assert 'class="hist-act"' in html
    assert "/browse/cpa/endnotes#" in html
    assert "No. 1/2026" in html


def test_render_timeline_links_a_note_the_index_cant_name_to_the_resolver():
    entry = _changed_entry(new_history=["S. 366 amended by No. 999/2026 s. 1."])
    html = render_timeline([entry], "/browse/cpa", amendment_index=build_amendment_index({}))

    assert '<a class="hist-act unresolved" href="/legislation/999-2026"' in html


def test_render_timeline_links_each_version_to_its_own_page():
    html = render_timeline([_changed_entry()], "/browse/cpa",
                           version_urls={112: "/browse/cpa-v112/section/s366"})

    assert '<a class="tl-version" href="/browse/cpa-v112/section/s366">' in html


def test_render_timeline_names_an_insertion_by_the_acts_own_word_for_it():
    html = render_timeline([_changed_entry(change="inserted", diff=[], new_history=["New s. 464 inserted by No. 1/2026 s. 83."])],
                           "/browse/cpa")

    assert "Inserted" in html
    assert '<div class="tl-diff">' not in html


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
    import corpus.html_view as html_view_module
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
    import corpus.html_view as html_view_module
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
    import corpus.html_view as html_view_module
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
    import corpus.html_view as html_view_module
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
    import corpus.html_view as html_view_module
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
    import corpus.html_view as html_view_module
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
    import corpus.html_view as html_view_module
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

    assert 'id="copy-section-btn"' in body
    # Before the provisions, so tabbing into the page reaches it without
    # first walking the whole section.
    assert body.index("copy-section-btn") < body.index('<div class="provisions">')


def test_the_copy_script_ships_with_the_page():
    from corpus.html_view import page_shell

    page = page_shell("Test Act", render_section(_parsed(_definitions_act()), "Test Act", "/browse/a", "s3"))

    assert "copy-section-btn" in page
    assert '<script src="/assets/copy.js"></script>' in page


def test_one_level_of_nesting_is_one_word_tab_stop():
    """36pt is half an inch -- the default tab stop in both Word and
    Google Docs, so a copied paragraph lands where a reader's own tab
    key would have put it."""
    from corpus.html_view import template_text

    assert "var INDENT_PT = 36;" in template_text("copy.js")


def test_a_defined_term_keeps_its_own_punctuation_tight():
    """"amended, in relation to ..." -- the term runs into a comma, and
    the copy must not open a gap the page itself doesn't show. Both
    clipboard flavours go through the same rule, which is why there is
    one helper rather than two spellings of it."""
    from corpus.html_view import template_text

    copy_js = template_text("copy.js")
    assert copy_js.count("function gap(") == 1
    assert copy_js.count("gap(p.text)") == 2


# The page template. Since static/site/page.html is a file rather than a
# string constant, the thing most likely to break is the seam between the
# two: a placeholder renamed in one and not the other leaves a literal
# "{{...}}" in a published page, which no amount of CSS review would
# catch.
def _shell(**kwargs) -> str:
    from corpus.html_view import page_shell

    return page_shell("Test Act", "<p>body</p>", **kwargs)


def test_no_placeholder_survives_into_a_rendered_page():
    for kwargs in ({}, {"base_url": "/browse/a"}, {"base_url": "/browse/a", "reader": True},
                   {"previewbar_html": '<div class="previewbar">preview</div>'}):
        page = _shell(**kwargs)
        assert "{{" not in page and "}}" not in page, (kwargs, page)


def test_asset_urls_stay_inside_a_project_site():
    """On GitHub Pages the site is served under /<repo>/, so an asset URL
    that assumed the domain root would reach for the real root instead."""
    page = _shell(base_url="/vic-legislation-parser/browse/a")

    assert '<link rel="stylesheet" href="/vic-legislation-parser/assets/tokens.css">' in page
    assert '<script src="/vic-legislation-parser/assets/reader.js"></script>' in page
    # Junicode is reached relative to tokens.css, so no prefix belongs in
    # the stylesheet itself -- that is what makes one file serve both.
    from corpus.html_view import template_text

    assert "url('fonts/Junicode-Roman.woff2')" in template_text("tokens.css")


def test_a_section_page_asks_for_the_reader_layout():
    assert 'class="page page-reader"' in _shell(base_url="/browse/a", reader=True)
    assert 'class="page"' in _shell(base_url="/browse/a")


def test_the_template_is_read_again_after_it_changes(tmp_path, monkeypatch):
    """Editing a stylesheet or the shell while a server is running has to
    show up on the next reload -- that is most of the reason the template
    is a file at all."""
    import corpus.html_view as html_view_module

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


def _outline_of(section_slug: str, **kwargs) -> str:
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", section_slug, **kwargs)
    return body.split('<nav class="outline"')[1].split("</nav>")[0]


def test_the_outline_is_only_the_branch_you_are_reading():
    """The immediate neighbourhood, not the Act: a whole contents list in
    every sidebar would be a second contents page (the Criminal Procedure
    Act alone would put over a thousand links on every page of itself)."""
    outline = _outline_of("s10")

    # The Part and Division this section is in...
    assert "Part 2 - Offences" in outline and "Division 1 - Assault" in outline
    # ...and the sections beside it, with this one marked.
    assert "11 Aggravated assault" in outline
    assert outline.count('aria-current="page"') == 1
    # Nothing else: not another Division's sections, not another Part's,
    # and not the other Parts themselves.
    assert "20 Theft" not in outline
    assert "30 Powers" not in outline
    assert "Part 1 - Preliminary" not in outline
    assert "Part 3 - Enforcement" not in outline
    assert "Division 2 - Theft" not in outline


def test_the_outline_still_offers_the_way_back_out():
    """Going further than the immediate context is what these are for, so
    they have to be there whatever the outline is showing."""
    outline = _outline_of("s10")

    assert 'class="outline-doc" href="/browse/a/"' in outline
    assert 'class="outline-contents" href="/browse/a/">Act index' in outline


def test_the_outline_names_the_document():
    """Otherwise a section page never says which Act it is: the heading is
    the provision's, and "Act index" doesn't say which index."""
    assert ">Test Act</a>" in _outline_of("s10")


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
    from corpus.html_view import page_shell

    assert "<!-- kept -->" in page_shell("T", "<p>a <!-- kept --> note</p>")


def test_the_outline_marks_a_provision_that_is_not_published_yet():
    """Same reason the contents page marks it: a line that looks like
    every other one, and turns out to be a page saying the text isn't
    there, is worse than one that says so first."""
    body = render_section(_parsed(_three_part_act()), "Test Act", "/browse/a", "s10",
                          unpublished_pages={"s11"})
    outline = body.split('<nav class="outline"')[1].split("</nav>")[0]

    assert '<a href="/browse/a/section/s11" class="unpublished">' in outline
    # The words are there for a screen reader, which has no dot to see.
    assert "(not yet published)" in outline
    assert 'href="/browse/a/section/s11"' in outline, "still linked, not hidden"
    # And a published neighbour is left alone.
    assert 'section/s10" aria-current="page">' in outline


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
    from corpus.html_view import template_text

    import re

    # Comments only explain the rules; it is the rules themselves that
    # have to be clean of it (this note's own prose included).
    css = re.sub(r"/\*.*?\*/", "", template_text("page.css"), flags=re.S)
    prov_rule = css.split(".prov {")[1].split("}")[0]

    assert "display: grid" in prov_rule
    assert "text-indent" not in css, "a hanging indent can paint outside the box it belongs to"


def test_the_timeline_says_when_it_cannot_compare_versions():
    """"No changes" and "not comparable" are different answers, and on a
    register of the law the difference matters. Two versions read by
    different parsers would report the parsers' own disagreements as
    amendments -- so nothing is reported, and it says so."""
    from corpus.html_view import render_timeline

    assert render_timeline([], "/browse/a") == ""
    unavailable = render_timeline([], "/browse/a", unavailable=True)
    assert "can't be shown yet" in unavailable
    assert "Re-parse every version" in unavailable


def test_an_act_offers_its_bill_and_em_from_its_own_contents():
    related = [
        {"slug": "crimes-bill", "kind": "bill", "title": "Crimes Bill 1957", "href": "/browse/crimes-bill/"},
        {"slug": "crimes-bill-em", "kind": "em", "title": "Crimes Bill 1957 EM", "href": "/browse/crimes-bill-em/"},
    ]
    page = render_index(_parsed(_definitions_act()), "Crimes Act 1958", "/browse/a", related=related)

    assert "Related documents" in page
    assert 'href="/browse/crimes-bill/"' in page and 'href="/browse/crimes-bill-em/"' in page
    assert "the Bill it was enacted from" in page
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
