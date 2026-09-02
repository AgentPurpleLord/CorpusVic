"""Tests for ai_pipeline/html_view.py's pure rendering logic -- chiefly
render_preview, which decides what one hover card gets to show. The
dashboard endpoint that serves it, the section/index page renderers and
the browser-side hover behaviour itself were exercised end to end against
real parsed Act data and a real browser session instead."""
from ai_pipeline.amendments import build_amendment_index
from ai_pipeline.html_view import (
    build_page_index,
    render_endnotes,
    render_index,
    render_preview,
    render_section,
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
    assert index["by_number"] == {"1": "c1", "2": "c2"}


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


def test_render_endnotes_returns_none_without_endnotes():
    # A Bill, an EM, or an Act parsed before endnotes were extracted.
    assert render_endnotes(_parsed(_definitions_act()), "Test Act", "/browse/a") is None


def test_render_index_links_to_the_endnotes_when_there_are_some():
    assert '/browse/a/endnotes' in render_index(_act_with_endnotes(), "Test Act", "/browse/a")
    assert "endnotes" not in render_index(_parsed(_definitions_act()), "Test Act", "/browse/a")


def test_a_margin_note_names_the_act_behind_its_citation():
    nodes = _definitions_act()
    nodes[1]["history"] = [{"raw": "S. 3 amended by No. 68/2009 s. 51."}]
    index = build_amendment_index(_ENDNOTES, {})
    body = render_section({"nodes": nodes, "hierarchy": None}, "Test Act", "/browse/a", "s3", amendment_index=index)

    assert 'href="/browse/a/endnotes#act-68-2009"' in body
    assert ">Amending Act 2009</a>" in body


def test_a_margin_note_without_an_index_is_left_as_the_bare_citation():
    nodes = _definitions_act()
    nodes[1]["history"] = [{"raw": "S. 3 amended by No. 68/2009 s. 51."}]
    body = render_section({"nodes": nodes, "hierarchy": None}, "Test Act", "/browse/a", "s3")

    assert "hist-act" not in body
    assert "S. 3 amended by No. 68/2009 s. 51." in body
