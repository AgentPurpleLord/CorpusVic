"""Tests for corpus/search.py -- full-text search over the corpus.

Against a real FTS5 index built in tmp_path, not a mock. What this module
is for is the handful of ways sqlite's query language differs from what
somebody types into a box, and a mock would only ever match what it was
told to. The index is small and builds in milliseconds at this size, so
there is no reason to pretend.
"""
import sqlite3

import pytest

from corpus import db, html_view, search

from conftest import make_node


class FakeSource:
    """The document lookups corpus/search.py builds an index through.

    The page index is computed for real rather than fabricated: where a
    provision lands is most of what an index row is, and a hand-written
    answer would agree with itself while disagreeing with the pages."""

    def __init__(self, documents):
        # {slug: (title, kind, as_at, nodes)}
        self.documents = documents

    def discover_slugs(self):
        return sorted(self.documents)

    def _act_title(self, slug):
        return self.documents[slug][0]

    def act_status(self, slug, publication=None):
        title, kind, as_at, _nodes = self.documents[slug]
        return {"kind": kind, "version_as_at": as_at, "parsed": True, "slug": slug}

    def _current_nodes(self, slug):
        return self.documents[slug][3], [], None

    def _page_index(self, slug):
        nodes, _notes, hierarchy = self._current_nodes(slug)
        return html_view.build_page_index({"nodes": nodes, "hierarchy": hierarchy},
                                          self._act_title(slug))


def _act(*nodes):
    return list(nodes)


@pytest.fixture
def corpus(tmp_path):
    """Two Acts and a superseded reprint of one of them."""
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    source = FakeSource({
        "crimes-act": ("Crimes Act 1958", "act", "1 January 2026", _act(
            make_node("part", "1", "Preliminary"),
            make_node("section", "3", "Definitions", "In this Act—"),
            make_node("subsection", "1", None, "An indictable offence may be heard summarily."),
            make_node("section", "4", "Reasonable excuse",
                      "It is a reasonable excuse that the person was elsewhere."),
        )),
        "evidence-act-v1": ("Evidence Act 2008", "act", "1 January 2020", _act(
            make_node("section", "59", "The hearsay rule", "Old wording about hearsay."),
        )),
        "evidence-act-v2": ("Evidence Act 2008", "act", "1 January 2026", _act(
            make_node("section", "59", "The hearsay rule", "New wording about hearsay."),
        )),
    })
    for slug in source.documents:
        (tmp_path / "data" / "parsed" / f"{slug}.json").write_text("{}", encoding="utf-8")
    for work in ("crimes-act", "evidence-act"):
        db.set_publication(work, True, tmp_path)
    return tmp_path, source


def _build(tmp_path, source):
    search.rebuild(tmp_path, source=source)
    return search.Index(tmp_path)


# ---------------------------------------------------------------------
# Finding things
# ---------------------------------------------------------------------


def test_a_phrase_matches_the_phrase_and_not_the_words_apart(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    assert index.search('"reasonable excuse"')["total"] >= 1
    assert index.search('"excuse reasonable"')["total"] == 0


def test_a_result_addresses_the_provision_not_just_the_page(corpus):
    """A section is one page and many provisions. Somebody searching for
    words in a subsection wants that subsection."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    hit = index.search("indictable summarily")["results"][0]
    assert "/browse/crimes-act/section/" in hit["href"]
    assert hit["label"].startswith("Section 3")


def test_a_heading_match_outranks_a_body_match(corpus):
    """Somebody searching "reasonable excuse" usually wants the provision
    called that, not the ones that merely mention it."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    first = index.search("reasonable excuse")["results"][0]
    assert "Reasonable excuse" in first["label"]


def test_the_breadcrumb_says_where_in_the_act_a_hit_sits(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    hit = index.search("indictable")["results"][0]
    assert "Part 1" in hit["breadcrumb"]


# ---------------------------------------------------------------------
# What is in scope
# ---------------------------------------------------------------------


def test_a_work_that_is_not_on_the_site_is_not_searchable(corpus):
    """The index holds what the site serves. A search that surfaced a
    provision the site would not serve is a leak, not a feature."""
    tmp_path, source = corpus
    db.set_publication("crimes-act", False, tmp_path)
    index = _build(tmp_path, source)

    assert index.search("indictable")["total"] == 0
    assert index.search("hearsay")["total"] >= 1


def test_publishing_a_work_again_brings_it_back(corpus):
    tmp_path, source = corpus
    db.set_publication("crimes-act", False, tmp_path)
    _build(tmp_path, source)
    db.set_publication("crimes-act", True, tmp_path)
    index = _build(tmp_path, source)

    assert index.search("indictable")["total"] >= 1


def test_superseded_reprints_are_left_out_by_default(corpus):
    """Five reprints of one Act would otherwise answer nearly every
    query five times over."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    current = index.search("hearsay")
    assert current["total"] == 1
    assert "New wording" in current["results"][0]["snippet_html"]


def test_superseded_reprints_can_be_asked_for(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    both = index.search("hearsay", include_superseded=True)
    assert both["total"] == 2
    # Current text first, always: an older reprint is never the better
    # answer to a question somebody asked today.
    assert both["results"][0]["is_current"] is True


def test_the_newest_reprint_holds_the_works_address(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    hit = index.search("hearsay")["results"][0]
    assert hit["href"].startswith("/browse/evidence-act/")


# ---------------------------------------------------------------------
# Snippets, and the escaping trap in them
# ---------------------------------------------------------------------


def test_the_snippet_marks_what_matched(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    snippet = index.search("indictable")["results"][0]["snippet_html"]
    assert "<mark>indictable</mark>" in snippet


def test_markup_in_the_legislation_is_escaped_and_the_marks_survive(tmp_path):
    """sqlite inserts the marks into raw text, so the text has to be
    escaped after it is marked and before the marks become tags. Escaping
    first destroys the marks; not escaping at all publishes any "<" in
    the legislation as markup."""
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    source = FakeSource({"test-act": ("Test Act", "act", None, [
        make_node("section", "1", "Scripts",
                  "A dangerous <script>alert(1)</script> thing to publish."),
    ])})
    (tmp_path / "data" / "parsed" / "test-act.json").write_text("{}", encoding="utf-8")
    db.set_publication("test-act", True, tmp_path)
    index = _build(tmp_path, source)

    snippet = index.search("dangerous")["results"][0]["snippet_html"]

    assert "<script>" not in snippet
    assert "&lt;script&gt;" in snippet
    assert "<mark>dangerous</mark>" in snippet


# ---------------------------------------------------------------------
# Turning what somebody typed into something sqlite will accept
# ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "s 3(1)", '"unclosed', "*", "a AND", "AND", "NEAR/5", "()", "-", "- -",
    "NOT everything", "a OR OR b", '""', "OR", "🙂", "x" * 4000, "a*b*c",
    "section 3(1)(a)(ii)", "R v Smith [2019] VSCA 1", "; DROP TABLE doc;--",
])
def test_nothing_anybody_types_can_make_sqlite_refuse(corpus, raw):
    """FTS5's MATCH is a query language, not a string, and legislation is
    full of characters that are punctuation to it. Raw input never
    reaches MATCH -- this is the test that keeps that true."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    result = index.search(raw)

    assert "error" not in result, result.get("error")
    assert isinstance(result["total"], int)


def test_an_empty_search_is_not_an_error(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    assert index.search("   ")["total"] == 0
    assert index.search("   ")["results"] == []


def test_a_prefix_search_is_kept(corpus):
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    assert index.search("indict*")["total"] >= 1


def test_a_phrase_is_kept_as_a_phrase():
    assert search.parse_query('"reasonable excuse"') == '"reasonable excuse"'


def test_punctuation_becomes_words_rather_than_syntax():
    assert search.parse_query("s 3(1)") == '"s" "3 1"'


# ---------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------


def test_a_reader_notices_the_index_has_been_replaced(corpus):
    """The builder writes a new file and moves it over the old one, so a
    connection opened earlier goes on reading the file it opened -- by an
    inode that still exists because something has it open. That is an
    index that is stale forever and looks exactly like a working one."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)
    assert index.search("indictable")["total"] >= 1

    source.documents["crimes-act"][3][2]["text"] = "Something else entirely."
    search.rebuild(tmp_path, source=source)

    assert index.search("indictable")["total"] == 0
    assert index.search("entirely")["total"] >= 1


def test_the_signature_changes_when_the_data_does(corpus):
    tmp_path, source = corpus
    before = search.signature(tmp_path)
    db.set_publication("crimes-act", False, tmp_path)

    assert search.signature(tmp_path) != before


def test_reading_an_index_that_was_never_built_says_so(tmp_path):
    """Rather than a 500 on a page somebody is looking at."""
    with pytest.raises(search.SearchUnavailable):
        search.Index(tmp_path).search("anything")


def test_a_half_built_index_is_never_readable(corpus, monkeypatch):
    """Built to one side and moved into place, so a reader either sees
    the whole previous index or the whole new one."""
    tmp_path, source = corpus
    _build(tmp_path, source)
    seen = {}
    real_replace = search.os.replace

    def watching(src, dst):
        # At the moment of the move, what is at the destination is still
        # the complete previous index.
        seen["before"] = sqlite3.connect(str(dst)).execute(
            "SELECT count(*) FROM node_fts").fetchone()[0]
        return real_replace(src, dst)

    monkeypatch.setattr(search.os, "replace", watching)
    search.rebuild(tmp_path, source=source)

    assert seen["before"] > 0
