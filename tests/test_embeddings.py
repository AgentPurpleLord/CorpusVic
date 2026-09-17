"""Tests for corpus/embeddings.py -- the optional semantic half of search.

The model itself is a 110 MB download and is not present in CI, on a
fresh clone, or here. So what is tested is everything around it: that its
absence changes nothing, that the fusion arithmetic is right, that the
sections are grouped the way the vectors assume, and -- with a stand-in
for the model -- that a provision the lexical side never saw is actually
pulled in and ranked.

That last one matters more than it looks. The whole purpose of the
semantic half is to reach a provision that shares no words with the
query, so the code path that fetches and merges it is the code path that
cannot be exercised by any query in the eval set without a model. A
stand-in exercises it exactly.
"""
import json

import pytest

from corpus import db, embeddings, html_view, search

from conftest import make_node
from test_search import FakeSource, _build


@pytest.fixture
def corpus(tmp_path):
    """Two Acts, one of which answers a question in words the question
    does not use -- which is the case this module exists for."""
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    source = FakeSource({
        "fv-act": ("Family Violence Protection Act 2008", "act", "1 January 2026", [
            make_node("section", "123", "Contravention of family violence intervention order",
                      "A person against whom a family violence intervention order has been "
                      "made must not contravene the order."),
            make_node("subsection", "1", None, "Penalty: Level 7 imprisonment."),
            make_node("section", "5", "Meaning of family violence",
                      "Family violence is behaviour towards a family member that is "
                      "physically or sexually abusive."),
        ]),
        "crimes-act": ("Crimes Act 1958", "act", "1 January 2026", [
            make_node("section", "322O", "Duress",
                      "A person is not guilty of an offence in respect of conduct carried "
                      "out under duress."),
        ]),
    })
    for slug in source.documents:
        (tmp_path / "data" / "parsed" / f"{slug}.json").write_text("{}", encoding="utf-8")
        db.set_publication(slug, True, tmp_path)
    return tmp_path, source


# ---------------------------------------------------------------------
# Absence, which is the ordinary state
# ---------------------------------------------------------------------


def test_no_model_means_no_semantic_half(corpus):
    """A fresh clone, CI, and any server that has not run the download.
    Search has to be exactly the search it was."""
    tmp_path, _source = corpus
    index = _build(tmp_path, _source)

    assert embeddings.model_present(tmp_path) is False
    assert index.vectors() is None
    assert index.search("family violence")["total"] >= 1


def test_status_reports_what_is_missing_rather_than_failing(tmp_path):
    state = embeddings.status(tmp_path)

    assert state == {"model": False, "vectors": False, "sections": None,
                     "signature": None, "model_name": None}


def test_building_without_a_model_says_which_step_was_skipped(tmp_path):
    with pytest.raises(FileNotFoundError) as raised:
        embeddings.build(tmp_path)

    assert "download_search_model.py" in str(raised.value)


# ---------------------------------------------------------------------
# What gets embedded
# ---------------------------------------------------------------------


def test_a_section_is_embedded_whole_rather_than_provision_by_provision(corpus):
    """A bare subsection -- "Penalty: Level 7 imprisonment" -- embeds
    into nothing useful on its own. Its section does."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    sections = embeddings.section_texts(index.connection())

    by_page = {(s["site_slug"], s["page"]): s["text"] for s in sections}
    text = by_page[("fv-act", "s123")]
    assert "Contravention of family violence intervention order" in text
    assert "Penalty: Level 7 imprisonment." in text
    assert len(sections) == 3


def test_a_sections_text_leads_with_what_it_is_called(corpus):
    """Because "Section 123 Contravention of family violence
    intervention order" says what the section is in the words somebody
    would use to ask for it."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)

    sections = embeddings.section_texts(index.connection())
    duress = next(s for s in sections if s["page"] == "s322o")

    assert duress["text"].startswith("Section 322O Duress")


# ---------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------


def _row(slug, page):
    return {"site_slug": slug, "page": page}


def test_fusion_is_of_positions_and_never_of_scores():
    """bm25 and cosine are not on the same scale and never will be. RRF
    sums 1/(k+rank), so there is no constant to tune and one side being
    strange cannot swamp the other."""
    lexical = [_row("a", "s1"), _row("b", "s2"), _row("c", "s3")]
    semantic = [{"site_slug": "c", "page": "s3", "score": 0.9}]

    fused = embeddings.fuse(lexical, semantic, lambda r: (r["site_slug"], r["page"]))

    # c is third lexically and first semantically: 1/63 + 1/61 beats a's
    # 1/61 alone.
    assert [r["page"] for r in fused] == ["s3", "s1", "s2"]


def test_a_row_in_neither_list_keeps_its_lexical_place():
    lexical = [_row("a", "s1"), _row("b", "s2")]

    fused = embeddings.fuse(lexical, [], lambda r: (r["site_slug"], r["page"]))

    assert [r["page"] for r in fused] == ["s1", "s2"]


def test_the_lexical_order_breaks_a_tie():
    """Two rows agreeing on everything should come out in the order the
    lexical side had them, not in whatever order the dictionary was
    built."""
    lexical = [_row("a", "s1"), _row("b", "s2")]
    semantic = [{"site_slug": "a", "page": "s1"}, {"site_slug": "b", "page": "s2"}]

    fused = embeddings.fuse(lexical, semantic, lambda r: (r["site_slug"], r["page"]))

    assert [r["page"] for r in fused] == ["s1", "s2"]


def test_every_provision_of_a_section_shares_its_semantic_rank():
    """Sections are what is embedded, so the semantic side has no opinion
    about which provision within one is the answer. The lexical side
    decides that, which is what it is good at."""
    lexical = [_row("a", "s1"), _row("b", "s9"), _row("a", "s1")]
    semantic = [{"site_slug": "a", "page": "s1"}]

    fused = embeddings.fuse(lexical, semantic, lambda r: (r["site_slug"], r["page"]))

    assert [(r["site_slug"], r["page"]) for r in fused[:2]] == [("a", "s1"), ("a", "s1")]


# ---------------------------------------------------------------------
# The path that only a model would otherwise exercise
# ---------------------------------------------------------------------


class _StandInVectors:
    """A model's opinion, without a model.

    Says a particular section is what the query is about, which is what
    an embedding model does and the only thing search asks of it."""

    def __init__(self, nearest, fail=False):
        self._nearest = nearest
        self._fail = fail

    def available(self):
        return True

    def nearest(self, query, limit=embeddings.SEMANTIC_DEPTH):
        if self._fail:
            raise RuntimeError("the model did not load")
        return self._nearest


def test_a_provision_sharing_no_words_with_the_query_is_still_reached(corpus, monkeypatch):
    """The whole point, and the one path no query can exercise without a
    model. "I was forced to commit the crime by threats" is answered by a
    section headed "Duress" which shares not one word with it, so it is
    not in the lexical pool at any depth -- merging it in is the only way
    it can ever be an answer.

    Stood up here with a query that provably cannot match it, so that
    what is being tested is the merge and not a lucky word."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)
    lexical = index.search("family violence")
    assert "s322o" not in [h["page"] for h in lexical["results"]]

    monkeypatch.setattr(index, "vectors",
                        lambda: _StandInVectors([{"site_slug": "crimes-act", "page": "s322o"}]))
    fused = index.search("family violence")

    assert fused["results"][0]["page"] == "s322o"
    # And the lexical results are still all there, below it: the semantic
    # side adds a candidate, it never removes one.
    assert set(h["page"] for h in lexical["results"]) <= set(h["page"] for h in fused["results"])


def test_a_model_that_fails_leaves_search_exactly_as_it_was(corpus, monkeypatch):
    """A search that quietly loses its semantic half is better than one
    that 500s on a page somebody is looking at."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)
    without = index.search("family violence")

    monkeypatch.setattr(index, "vectors", lambda: _StandInVectors([], fail=True))
    with_broken_model = index.search("family violence")

    assert [h["page"] for h in with_broken_model["results"]] == \
        [h["page"] for h in without["results"]]


def test_a_query_written_in_fts5s_own_language_is_left_to_fts5(corpus, monkeypatch):
    """Somebody who typed a quoted phrase or a NOT meant it, and fusing a
    model's opinion into an exact query is taking it off them."""
    tmp_path, source = corpus
    index = _build(tmp_path, source)
    asked = []

    class _Watching(_StandInVectors):
        def nearest(self, query, limit=embeddings.SEMANTIC_DEPTH):
            asked.append(query)
            return self._nearest

    monkeypatch.setattr(index, "vectors",
                        lambda: _Watching([{"site_slug": "crimes-act", "page": "s322o"}]))
    index.search('"family violence"')

    assert asked == []


# ---------------------------------------------------------------------
# The stored vectors
# ---------------------------------------------------------------------


def test_vectors_that_disagree_with_their_manifest_are_refused(tmp_path):
    """A matrix and a list of sections that are different lengths
    describe a corpus neither of them holds. Loading it would answer
    questions about the wrong provisions -- silently, and for ever."""
    numpy = pytest.importorskip("numpy")
    (tmp_path / "data").mkdir(parents=True)
    with open(embeddings.vectors_path(tmp_path), "wb") as handle:
        numpy.save(handle, numpy.zeros((3, embeddings.DIMENSIONS), "float32"))
    embeddings.manifest_path(tmp_path).write_text(json.dumps({
        "sections": [{"site_slug": "a", "page": "s1"}]}), encoding="utf-8")

    assert embeddings.Vectors(tmp_path).load() is False


def test_nearest_says_nothing_rather_than_failing_when_there_is_nothing(tmp_path):
    assert embeddings.Vectors(tmp_path).nearest("anything") == []


def test_vectors_without_the_libraries_read_as_absent(tmp_path, monkeypatch):
    """A half-finished setup: the vectors were copied across but numpy
    was never installed. That should read as "no semantic half", not as
    a traceback on a page somebody is looking at."""
    import builtins

    numpy = pytest.importorskip("numpy")
    (tmp_path / "data").mkdir(parents=True)
    with open(embeddings.vectors_path(tmp_path), "wb") as handle:
        numpy.save(handle, numpy.zeros((1, embeddings.DIMENSIONS), "float32"))
    embeddings.manifest_path(tmp_path).write_text(
        json.dumps({"sections": [{"site_slug": "a", "page": "s1"}]}), encoding="utf-8")

    real_import = builtins.__import__

    def without_numpy(name, *args, **kwargs):
        if name.split(".")[0] in {"numpy", "onnxruntime", "tokenizers"}:
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_numpy)

    assert embeddings.Vectors(tmp_path).load() is False
