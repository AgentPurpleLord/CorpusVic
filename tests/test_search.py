"""Tests for corpus/search.py -- full-text search over the corpus.

Against a real FTS5 index built in tmp_path, not a mock. What this module
is for is the handful of ways sqlite's query language differs from what
somebody types into a box, and a mock would only ever match what it was
told to. The index is small and builds in milliseconds at this size, so
there is no reason to pretend.
"""
import sqlite3

import pytest

from corpus import db, html_view, query, search

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
            # A section that is a heading and nothing else -- common in
            # real Acts, where the words are all in the subsections.
            make_node("section", "9", "Short title", ""),
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


def test_a_heading_only_match_has_no_snippet(corpus):
    """The label already shows those words. Repeating them underneath as
    an "extract" reads as a bug rather than as an extract."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    hit = index.search('"short title"')["results"][0]
    assert hit["label"] == "Section 9 Short title"
    assert hit["snippet_html"] == ""


def test_a_provisions_own_words_are_shown_where_it_has_some(corpus):
    """A heading match on a section that does have text still shows that
    text: it is context for the hit, not a repeat of the label."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    hit = index.search("Definitions")["results"][0]
    assert hit["label"].startswith("Section 3")
    assert "In this Act" in hit["snippet_html"]


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
    """`s 3(1)` is a syntax error to FTS5. It becomes three harmless
    words, OR'd -- a provision matching two of them still appears, which
    is the whole change: requiring every term is what made a question
    typed in plain words return nothing at all."""
    assert search.parse_query("s 3(1)") == '"s" OR "3" OR "1"'


# ---------------------------------------------------------------------
# Questions, and the provisions headed like answers to them
# ---------------------------------------------------------------------


def test_a_question_asks_for_the_provisions_headed_like_its_answer():
    a = query.analyse("who can appeal a family violence order")

    assert a.asks == "who"
    assert search._heading_match(a) == (
        'heading: ("who" AND ("appeal" OR "family" OR "violence" OR "order"))')


def test_a_query_that_is_not_a_question_asks_for_nothing_extra():
    """The second query is only worth running when the shape of the
    question says where to look."""
    assert search._heading_match(query.analyse("hearsay rule")) == ""
    assert search._heading_match(query.analyse("indictable offence")) == ""


def test_a_heading_has_to_open_with_the_question_word():
    """"Who may appeal" answers "who can appeal". "Protection for
    children who have become family members" merely contains the word."""
    a = query.analyse("who can appeal a family violence order")

    assert search._answers("Who may appeal", a)
    assert not search._answers("Protection for children who have become family members", a)
    assert not search._answers("Appeal to the County Court", a)


def test_a_provision_buried_by_bm25_is_still_found_when_it_answers(tmp_path):
    """The whole point of the second query. In the real corpus, "who can
    appeal a family violence order" put Section 114 "Who may appeal" at
    bm25 position 1,109 -- below every one of the six thousand provisions
    that mention a family violence order -- so no re-ranking depth would
    have reached it.

    Reproduced here in miniature: one provision headed like the answer,
    and enough noise mentioning the query's other words to bury it."""
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    nodes = [make_node("section", "114", "Who may appeal",
                       "An affected family member may appeal.")]
    for n in range(60):
        nodes.append(make_node("section", str(200 + n),
                               f"Family violence intervention order {n}",
                               "A family violence intervention order is an order about "
                               "family violence, made under this family violence Act."))
    source = FakeSource({"fv-act": ("Family Violence Protection Act 2008", "act",
                                    "1 January 2026", nodes)})
    (tmp_path / "data" / "parsed" / "fv-act.json").write_text("{}", encoding="utf-8")
    db.set_publication("fv-act", True, tmp_path)
    index = _build(tmp_path, source)

    found = index.search("who can appeal a family violence order", limit=5)

    assert found["results"][0]["label"] == "Section 114 Who may appeal"


def test_an_answering_provision_cannot_push_down_a_better_lexical_hit():
    """The rows the second query brings in are scored by a different
    MATCH expression, so their bm25 is not on the same scale as the
    pool's. Merging the two numbers put a provision that merely mentions
    police above the one saying when police may issue a safety notice, so
    a merged row comes in below the pool and only the boost can lift it."""
    analysis = query.analyse("who can appeal a family violence order")
    pool = [
        {"rank": -9.0, "heading": "Appeals generally", "ntype": "section"},
        # Merged: placed at the floor, and it does not answer the question.
        {"rank": -1.0, "heading": "Orders about family violence", "ntype": "section"},
    ]

    ranked = search._rerank(pool, analysis)

    assert ranked[0]["heading"] == "Appeals generally"


# ---------------------------------------------------------------------
# Typos, and the one rule that makes correcting them safe
# ---------------------------------------------------------------------
#
# Correction is always on, which is only tolerable because of a single
# invariant: a word the corpus contains is never second-guessed. That is
# what stops a precise query being quietly softened into a vague one, and
# it is too important to leave to a docstring. Everything below is that
# invariant, from both sides.


def _vocab(**counts) -> dict:
    """A vocabulary as fts5vocab hands it over: term -> how often."""
    return dict(counts)


def test_a_word_the_corpus_contains_is_never_corrected():
    """The invariant, stated directly.

    "excuse" is in the corpus, and "excuses" is in it ten times more
    often and one edit away. A search engine that helps here is a search
    engine that cannot be trusted with a term of art."""
    analysis = query.analyse("excuse", _vocab(excuse=3, excuses=30))

    assert analysis.corrections == {}
    assert '"excuse"' in analysis.match
    assert "excuses" not in analysis.match


def test_an_unknown_word_is_read_as_the_nearest_one_the_corpus_has():
    analysis = query.analyse("indictble offence",
                             _vocab(indictable=40, offence=90))

    assert analysis.corrections == {"indictble": "indictable"}


def test_a_correction_adds_to_the_search_and_never_takes_from_it():
    """Both go to FTS5, OR'd. A correction can only ever widen what is
    found -- so being wrong about one costs a few extra results rather
    than the one the reader was after."""
    analysis = query.analyse("hearsy", _vocab(hearsay=12))

    assert '"hearsy"' in analysis.match
    assert '"hearsay"' in analysis.match
    assert " OR " in analysis.match


def test_a_word_near_nothing_at_all_is_left_as_it_was():
    """Rather than dragged to whatever the corpus happens to contain.
    Nothing found is an honest answer; the wrong provision is not."""
    analysis = query.analyse("zygomorphic", _vocab(offence=90, hearsay=12))

    assert analysis.corrections == {}
    assert '"zygomorphic"' in analysis.match


def test_how_far_a_correction_reaches_depends_on_the_length_of_the_word():
    """Two edits in a six-letter word is a different word; in a twelve
    letter word it is a typo. So the bound goes by length."""
    # One edit, and short: reached.
    short = query.analyse("offenc", _vocab(offence=90))
    assert short.corrections == {"offenc": "offence"}

    # Two edits, and short: not reached.
    assert query.analyse("offanc", _vocab(offence=90)).corrections == {}

    # Two edits, and long enough that a slip is likelier than a different
    # word being meant.
    long = query.analyse("intrpretaton", _vocab(interpretation=25))
    assert long.corrections == {"intrpretaton": "interpretation"}


def test_a_word_too_short_to_correct_safely_is_not_corrected():
    """At three letters nearly everything is one edit from everything
    else, and the nearest word is close to a coin toss."""
    assert query.analyse("act", _vocab(act=50, ac=2, art=9)).corrections == {}


def test_a_tie_goes_to_the_word_the_corpus_uses_more():
    """Two candidates, both one edit away. The frequent one is the one
    more likely to have been meant."""
    analysis = query.analyse("offenceX".replace("X", "e"),
                             _vocab(offences=200, offencer=3))

    assert analysis.corrections == {"offencee": "offences"}


def test_a_query_written_in_fts5s_own_language_is_not_corrected():
    """Somebody who typed a quoted phrase meant it. Correcting inside it
    would be rewriting a query that was already exact."""
    analysis = query.analyse('"reasonable excus"', _vocab(excuse=30))

    assert analysis.verbatim
    assert analysis.corrections == {}
    assert analysis.match == '"reasonable excus"'


def test_without_a_vocabulary_nothing_is_corrected():
    """Which is what happens when fts5vocab is unavailable: search goes
    on working, exactly as it did before correction existed."""
    assert query.analyse("indictble").corrections == {}


def test_the_vocabulary_is_the_corpus_words_and_not_their_stems(corpus):
    """The bug the `word` table exists to prevent, kept as a test.

    node_fts is tokenized with `porter`, so fts5vocab over it hands back
    stems -- "indict", "famili", "offenc". Correcting against that list
    answers "no" when asked whether the corpus contains "family", which
    turns the one safety rule inside out: correction fires on nearly
    every word anybody types, and what it offers back is a stem."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    vocab = search.vocabulary(index.connection())

    assert "indictable" in vocab
    assert "indict" not in vocab
    assert "excuse" in vocab and "excus" not in vocab


def test_a_footnote_marker_glued_to_a_word_is_not_a_word():
    """Real corpus text contains "offence8" and "definitions5", where a
    marker has ended up against the word. Offering one of those as a
    correction would read as the search being broken."""
    words: dict = {}
    search._count_words(words, "Definitions5", "An offence8 under this Act.")

    assert words.get("definitions") == 1
    assert words.get("offence") == 1
    assert "offence8" not in words


def test_an_index_with_no_word_table_corrects_nothing(corpus):
    """An index built before the table existed. Search goes on working;
    it simply stops offering corrections, which is what it did before
    correction existed at all."""
    tmp_path, source = corpus
    _build(tmp_path, source)
    writable = sqlite3.connect(str(search.index_path(tmp_path)))
    with writable:
        writable.execute("DROP TABLE word")
    writable.close()

    found = search.Index(tmp_path).search("indictble")

    assert found["corrections"] == {}
    assert isinstance(found["total"], int)


def test_a_typo_finds_the_provision_the_word_would_have(corpus):
    """End to end, against a real index and its real vocabulary."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    typed = index.search("indictble")

    assert typed["total"] >= 1
    assert typed["corrections"] == {"indictble": "indictable"}
    assert typed["results"][0]["label"] == index.search("indictable")["results"][0]["label"]


def test_a_precise_query_reports_no_corrections(corpus):
    """The invariant again, this time against the vocabulary of a real
    index rather than a hand-written one."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    found = index.search("hearsay")

    assert found["total"] >= 1
    assert found["corrections"] == {}


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


def test_the_cached_vocabulary_is_dropped_when_the_index_is(corpus):
    """The vocabulary is read once per index rather than per query, which
    is a cache -- and a cache of what words the corpus contains is a
    cache that can go quietly wrong. It hangs off the same stamp the
    connection does, so a rebuild drops it."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)
    assert "hearsay" in index.vocabulary()

    # A new document, with a word the old index never saw.
    source.documents["new-act"] = ("Trespass Act 2026", "act", "1 January 2026", _act(
        make_node("section", "1", "Trespass", "A person must not commit trespass."),
    ))
    (tmp_path / "data" / "parsed" / "new-act.json").write_text("{}", encoding="utf-8")
    db.set_publication("new-act", True, tmp_path)
    search.rebuild(tmp_path, source=source)

    assert "trespass" in index.vocabulary()
    assert index.search("trespas")["corrections"] == {"trespas": "trespass"}


def test_the_signature_changes_when_the_data_does(corpus):
    tmp_path, source = corpus
    before = search.signature(tmp_path)
    db.set_publication("crimes-act", False, tmp_path)

    assert search.signature(tmp_path) != before


def test_an_empty_write_ahead_log_does_not_make_the_index_stale(corpus, monkeypatch):
    """A -wal appears when a connection opens and goes when the last one
    closes. Stamping its existence meant the index read stale whenever
    nothing happened to have the database open at that moment -- and a
    staleness light that comes on by itself is one you stop reading.

    Which works are published is held still here: reading that opens the
    database, and so moves the very file under examination."""
    tmp_path, _source = corpus
    monkeypatch.setattr(db, "published_works", lambda _base: {"crimes-act"})
    wal = tmp_path / "data" / "legislation.db-wal"

    wal.write_bytes(b"")
    empty = search.signature(tmp_path)
    wal.unlink()

    assert search.signature(tmp_path) == empty


def test_decisions_still_sitting_in_the_write_ahead_log_do(corpus, monkeypatch):
    """Which is the reason the log is stamped at all: pages written but
    not yet checkpointed are data the index was not built from."""
    tmp_path, _source = corpus
    monkeypatch.setattr(db, "published_works", lambda _base: {"crimes-act"})
    wal = tmp_path / "data" / "legislation.db-wal"
    wal.write_bytes(b"")
    empty = search.signature(tmp_path)

    wal.write_bytes(b"not checkpointed yet")

    assert search.signature(tmp_path) != empty


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
