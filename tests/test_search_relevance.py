"""How well search actually finds things.

Against the real corpus and the real eval set, not a fabricated one --
relevance is a property of this legislation and this vocabulary, and a
synthetic index would only tell us that the code runs. The behaviours
themselves (stopwords, intent, correction) are unit-tested in
tests/test_search.py; this file is the scoreboard.

The floor below is a ratchet. Raise it when a change earns it; never
lower it to make a change pass.
"""
import pytest

from corpus import relevance, search

# Where the search stood when this file was written: MRR 0.2946, four of
# fourteen queries at rank 1, and eight finding nothing at all.
#
# Then the query layer was rewritten -- stopwords dropped, terms OR'd
# rather than all required, and the question's shape read against the
# node types and heading conventions the parser already records. That
# took it to MRR 0.771, ten of fourteen at rank 1, and one query finding
# nothing.
#
# That last one was "who can appeal a family violence order", answered by
# a section headed "Who may appeal" which bm25 put at position 1,109. A
# second query over headings alone now finds it: MRR 0.785, still ten at
# rank 1, and thirteen of fourteen in the top five. Every number here is
# one the suite measured, not a target somebody picked.
FLOOR_MRR = 0.785
FLOOR_AT_1 = 10
FLOOR_AT_5 = 13


@pytest.fixture(scope="session")
def real_index(tmp_path_factory):
    """One index over the whole real corpus, built once for the session.

    Written to a temporary directory rather than the repository's own, so
    running the tests never disturbs the index a developer is using."""
    import dashboard

    out = tmp_path_factory.mktemp("relevance")
    works = {
        dashboard.split_document_slug(slug)[0]
        for slug in dashboard.discover_slugs()
        if (dashboard.BASE_DIR / "data" / "parsed" / f"{slug}.json").exists()
    }
    search.rebuild(out, source=dashboard, published=works)
    return search.Index(out)


@pytest.fixture(scope="session")
def eval_rows():
    from pathlib import Path

    return relevance.load_eval(Path(__file__).resolve().parent.parent / "data" / "search_eval.yaml")


@pytest.fixture(scope="session")
def scored(real_index, eval_rows):
    return relevance.score(lambda q: real_index.search(q, limit=20)["results"], eval_rows)


def test_the_eval_set_is_about_documents_that_exist(real_index, eval_rows):
    """An eval entry pointing at a provision that is not in the corpus
    scores zero for ever and looks like a relevance problem."""
    conn = real_index.connection()
    for row in eval_rows:
        found = conn.execute(
            "SELECT 1 FROM node_fts f JOIN doc d ON d.id = f.doc_id "
            "WHERE d.site_slug = ? AND f.page = ? LIMIT 1", (row["doc"], row["page"])).fetchone()
        assert found, f"{row['query']!r} expects {row['doc']}/{row['page']}, which is not indexed"


def test_relevance_does_not_regress(scored):
    assert scored["mrr"] >= FLOOR_MRR, (
        f"MRR fell to {scored['mrr']:.3f} from {FLOOR_MRR}\n\n" + relevance.report(scored))
    assert scored["at_5"] >= FLOOR_AT_5, (
        f"queries answered in the top five fell to {scored['at_5']} from {FLOOR_AT_5}\n\n"
        + relevance.report(scored))
    assert scored["at_1"] >= FLOOR_AT_1, (
        f"queries answered at rank 1 fell to {scored['at_1']} from {FLOOR_AT_1}\n\n"
        + relevance.report(scored))


def test_the_statutes_own_words_still_work(scored):
    """The easy queries, which use the vocabulary the Act itself uses.
    Making the hard ones work must not cost these."""
    easy = {"meaning of family violence", "meaning of relative",
            "hearsay rule", "meaning of penalty units"}
    for entry in scored["queries"]:
        if entry["query"] in easy:
            assert entry["rank"] == 1, f"{entry['query']!r} fell to rank {entry['rank']}"
