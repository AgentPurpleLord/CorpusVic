"""Tests for corpus/search_view.py -- the search form and its results,
rendered once for the two things that show them.

No index anywhere in here. The module is handed what corpus/search.py
returned and turns it into a page, and that separation is the reason the
whole of it can be tested on fabricated results -- including the states
a real index makes awkward to produce on demand, like a query sqlite
refused.
"""
import pytest

from corpus import search, search_view


def _hit(**overrides):
    hit = {
        "title": "Criminal Procedure Act 2009",
        "kind": "act",
        "as_at": "1 July 2024",
        "is_current": True,
        "version": 114,
        "label": "Section 242 Committal proceeding",
        "breadcrumb": "Chapter 4 › Part 4.9",
        "snippet_html": "an <mark>indictable</mark> offence",
        "slug": "criminal-procedure-act-v114",
        "site_slug": "criminal-procedure-act",
        "page": "s242",
        "fragment": "s242-1",
        "href": "/browse/criminal-procedure-act/section/s242#s242-1",
    }
    hit.update(overrides)
    return hit


def _found(results=None, total=None, truncated=False, **extra):
    results = [_hit()] if results is None else results
    found = {"query": "q", "parsed": "q", "results": results,
             "total": len(results) if total is None else total,
             "truncated": truncated}
    found.update(extra)
    return found


# ---------------------------------------------------------------------
# Where a result points
# ---------------------------------------------------------------------


def test_a_result_links_where_the_index_says():
    html = search_view.results_html(_found(), "indictable", search.Scope(), 0, "/search")

    assert "href='/browse/criminal-procedure-act/section/s242#s242-1'" in html


def test_a_result_is_addressed_by_the_name_the_site_serves_it_under():
    """The index records two names for a document: the work's own, which
    is where the newest reprint is served, and the parse's own. A link
    built from the second is a 404 on a page of results that otherwise
    look exactly right -- which is how this was found the first time."""
    html = search_view.results_html(_found(), "indictable", search.Scope(), 0, "/search")

    assert "href='/browse/criminal-procedure-act/section/s242#s242-1'" in html
    assert "criminal-procedure-act-v114" not in html


def test_the_form_submits_where_it_is_told():
    assert "action='/admin/search'" in search_view.form_html("/admin/search", "", search.Scope())
    assert "action='/search'" in search_view.form_html("/search", "", search.Scope())


def test_the_pager_keeps_the_query_and_the_scope():
    """Page two of a search that included Bills, quietly not including
    them, is the kind of thing somebody notices as "the results changed
    when I paged"."""
    html = search_view.pager_html(_found(truncated=True), "indictable offence",
                                  search.Scope(bills=True, superseded=True), 20,
                                  "/admin/search")

    assert ("/admin/search?q=indictable+offence&amp;offset=40"
            "&amp;bills=1&amp;superseded=1") in html
    assert "Previous" in html  # offset is past the first page


def test_an_ordinary_search_keeps_an_ordinary_url():
    """Only the toggles that are on go into a link, so the address of a
    normal search is not a list of everything switched off."""
    html = search_view.pager_html(_found(truncated=True), "x", search.Scope(), 0, "/search")

    assert "bills" not in html and "superseded" not in html and "em=" not in html


def test_the_first_page_has_no_previous_link():
    html = search_view.pager_html(_found(truncated=True), "x", search.Scope(superseded=False), 0, "/search")

    assert "Previous" not in html
    assert "Next" in html


def test_the_last_page_has_no_next_link():
    html = search_view.pager_html(_found(truncated=False), "x", search.Scope(superseded=False), 20, "/search")

    assert "Next" not in html
    assert "Previous" in html


# ---------------------------------------------------------------------
# Escaping -- the one real trap in here
# ---------------------------------------------------------------------


def test_what_somebody_typed_is_escaped_back_into_the_box():
    html = search_view.form_html("/search", "<script>alert(1)</script>", search.Scope())

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_provisions_own_angle_brackets_are_escaped():
    html = search_view.results_html(
        _found([_hit(label="Section 5 <dangerous> heading",
                     title="Act <with> brackets")]),
        "x", False, 0, "/search")

    assert "<dangerous>" not in html
    assert "&lt;dangerous&gt;" in html
    assert "&lt;with&gt;" in html


def test_the_snippet_is_not_escaped_twice():
    """corpus/search.py escaped the provision's text and then turned
    sqlite's marks into <mark> tags. Escaping it again here would publish
    the tags as visible text -- which is what the other order of those two
    steps does, and why this is worth pinning down in both places."""
    html = search_view.results_html(_found(), "indictable", search.Scope(), 0, "/search")

    assert "<mark>indictable</mark>" in html
    assert "&lt;mark&gt;" not in html


def test_a_query_with_markup_is_escaped_in_the_count_line():
    html = search_view.results_html(_found(), "<b>bold</b>", search.Scope(), 0, "/search")

    assert "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


# ---------------------------------------------------------------------
# The states that are not "here are your results"
# ---------------------------------------------------------------------


def test_nothing_matching_says_so():
    html = search_view.results_html(_found([], total=0), "zzz", False, 0, "/search")

    assert "Nothing matches" in html
    assert "<ol" not in html


def test_a_query_sqlite_refused_is_not_reported_as_no_matches():
    """They are different answers. "Nothing matches" invites somebody to
    conclude the corpus does not contain what they are looking for."""
    html = search_view.results_html(_found([], total=0, error="fts5: syntax error"),
                                    "a AND", False, 0, "/search")

    assert "could not be read" in html
    assert "Nothing matches" not in html


def test_a_superseded_result_says_that_it_is():
    html = search_view.results_html(_found([_hit(is_current=False)]),
                                    "x", False, 0, "/search")

    assert "superseded" in html


def test_a_result_without_a_date_or_a_breadcrumb_renders_cleanly():
    html = search_view.results_html(
        _found([_hit(as_at=None, breadcrumb="", snippet_html="")]),
        "x", False, 0, "/search")

    assert "as at" not in html
    assert "search-crumb" not in html
    assert "search-snippet" not in html
    assert "None" not in html


# ---------------------------------------------------------------------
# The whole body
# ---------------------------------------------------------------------


class _Index:
    def __init__(self, found=None, raises=None):
        self.found = found if found is not None else _found()
        self.raises = raises
        self.asked = []

    def search(self, query, include_superseded=False, limit=20, offset=0):
        if self.raises:
            raise self.raises
        self.asked.append({"query": query, "include_superseded": include_superseded,
                           "limit": limit, "offset": offset})
        return self.found


class _Unavailable(Exception):
    pass


def test_an_empty_query_asks_the_index_nothing():
    index = _Index()

    body = search_view.page_body(index, "   ", search.Scope(), 0, "/search")

    assert index.asked == []
    assert "search-page-form" in body


def test_a_missing_index_says_so_rather_than_failing():
    index = _Index(raises=_Unavailable("The search index hasn't been built yet."))

    body = search_view.page_body(index, "anything", search.Scope(), 0, "/search",
                                 unavailable=_Unavailable)

    assert "hasn&#x27;t been built" in body or "hasn't been built" in body


def test_a_negative_offset_cannot_reach_the_index():
    """It arrives from a query string, so it is whatever somebody typed."""
    index = _Index()

    search_view.page_body(index, "x", search.Scope(), -50, "/search")

    assert index.asked[0]["offset"] == 0


def test_the_page_size_is_the_one_the_pager_counts_in():
    index = _Index()

    search_view.page_body(index, "x", search.Scope(), 0, "/search")

    assert index.asked[0]["limit"] == search_view.PAGE_SIZE


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("on", True), ("true", True), ("TRUE", True), ("yes", True),
    ("", False), ("0", False), ("off", False), (None, False), ("no", False),
])
def test_what_the_checkbox_sends_is_read_the_way_browsers_send_it(value, expected):
    """A browser sends "on" for a ticked box, a hand-typed URL is as
    likely to say "1" or "true", and an absent box sends nothing at
    all."""
    assert search.Scope.from_params({"bills": value}).bills is expected


# ---------------------------------------------------------------------
# What a search is allowed to look at
# ---------------------------------------------------------------------


def test_the_default_offers_the_extras_without_having_applied_them():
    html = search_view.form_html("/search", "", search.Scope())

    assert "Also search" in html
    assert "Bills" in html and "Explanatory memoranda" in html
    assert "Superseded reprints" in html
    assert " checked" not in html


def test_a_toggle_that_is_on_is_shown_as_on_and_the_panel_is_open():
    """Somebody who followed a link, or came back to a page, has to see
    why there are Bills in their results. A filter you cannot see is a
    filter you blame the search for."""
    html = search_view.form_html("/search", "arrest", search.Scope(bills=True))

    assert "<details class='search-scope' open>" in html
    assert "name='bills' value='1' checked" in html
    assert "name='em' value='1'>" in html


def test_a_result_from_a_bill_says_so():
    """A Bill and its Act say nearly the same thing in nearly the same
    words. Which one you are reading is not a detail."""
    html = search_view.results_html(_found([_hit(kind="bill")]), "x",
                                    search.Scope(bills=True), 0, "/search")

    assert "<span class='search-kind'>Bill</span>" in html


def test_a_result_from_an_act_is_not_tagged():
    """Nearly every result is an Act. A tag on all of them says
    nothing."""
    html = search_view.results_html(_found(), "x", search.Scope(), 0, "/search")

    assert "search-kind" not in html
