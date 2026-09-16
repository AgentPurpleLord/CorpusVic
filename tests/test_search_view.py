"""Tests for corpus/search_view.py -- the search form and its results,
rendered once for the two things that show them.

No index anywhere in here. The module is handed what corpus/search.py
returned and turns it into a page, and that separation is the reason the
whole of it can be tested on fabricated results -- including the states
a real index makes awkward to produce on demand, like a query sqlite
refused.
"""
import pytest

from corpus import search_view


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
    html = search_view.results_html(_found(), "indictable", False, 0, "/search")

    assert "href='/browse/criminal-procedure-act/section/s242#s242-1'" in html


def test_the_admin_tool_addresses_a_document_by_its_parse_name():
    """The public site serves an Act's newest reprint at the work's own
    name; the admin tool serves every parse under its own. A link built
    the public way is a 404 on a page of results that otherwise look
    exactly right -- which is how this was found."""
    html = search_view.results_html(_found(), "indictable", False, 0,
                                    "/admin/search", base="/admin", slug_key="slug")

    assert "href='/admin/browse/criminal-procedure-act-v114/section/s242#s242-1'" in html
    assert "criminal-procedure-act/section" not in html


def test_the_base_goes_in_front_of_every_address():
    """The index stores addresses for a site served at the domain root,
    because that is the one form both callers can derive their own from.
    The admin tool is not served there."""
    html = search_view.results_html(_found(), "indictable", False, 0,
                                    "/admin/search", base="/admin")

    assert "href='/admin/browse/criminal-procedure-act/section/s242#s242-1'" in html
    assert "href='/browse/" not in html


def test_the_form_submits_where_it_is_told():
    assert "action='/admin/search'" in search_view.form_html("/admin/search", "", False)
    assert "action='/search'" in search_view.form_html("/search", "", False)


def test_the_pager_keeps_the_query_and_the_scope():
    html = search_view.pager_html(_found(truncated=True), "indictable offence",
                                  True, 20, "/admin/search")

    assert "/admin/search?q=indictable+offence&amp;offset=40&amp;superseded=1" in html
    assert "Previous" in html  # offset is past the first page


def test_the_first_page_has_no_previous_link():
    html = search_view.pager_html(_found(truncated=True), "x", False, 0, "/search")

    assert "Previous" not in html
    assert "Next" in html


def test_the_last_page_has_no_next_link():
    html = search_view.pager_html(_found(truncated=False), "x", False, 20, "/search")

    assert "Next" not in html
    assert "Previous" in html


# ---------------------------------------------------------------------
# Escaping -- the one real trap in here
# ---------------------------------------------------------------------


def test_what_somebody_typed_is_escaped_back_into_the_box():
    html = search_view.form_html("/search", "<script>alert(1)</script>", False)

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
    html = search_view.results_html(_found(), "indictable", False, 0, "/search")

    assert "<mark>indictable</mark>" in html
    assert "&lt;mark&gt;" not in html


def test_a_query_with_markup_is_escaped_in_the_count_line():
    html = search_view.results_html(_found(), "<b>bold</b>", False, 0, "/search")

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

    body = search_view.page_body(index, "   ", False, 0, "/search")

    assert index.asked == []
    assert "search-page-form" in body


def test_a_missing_index_says_so_rather_than_failing():
    index = _Index(raises=_Unavailable("The search index hasn't been built yet."))

    body = search_view.page_body(index, "anything", False, 0, "/search",
                                 unavailable=_Unavailable)

    assert "hasn&#x27;t been built" in body or "hasn't been built" in body


def test_a_negative_offset_cannot_reach_the_index():
    """It arrives from a query string, so it is whatever somebody typed."""
    index = _Index()

    search_view.page_body(index, "x", False, -50, "/search")

    assert index.asked[0]["offset"] == 0


def test_the_page_size_is_the_one_the_pager_counts_in():
    index = _Index()

    search_view.page_body(index, "x", False, 0, "/search")

    assert index.asked[0]["limit"] == search_view.PAGE_SIZE


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("on", True), ("true", True), ("TRUE", True), ("yes", True),
    ("", False), ("0", False), ("off", False), (None, False), ("no", False),
])
def test_what_the_checkbox_sends_is_read_the_way_browsers_send_it(value, expected):
    assert search_view.wants_superseded(value) is expected
