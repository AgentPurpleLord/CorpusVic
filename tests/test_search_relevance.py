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
# rank 1, and thirteen of fourteen in the top five.
#
# Then the default scope narrowed to the Acts as they stand. Beside each
# Act sit the Bill it began as and that Bill's explanatory memorandum,
# restating the same provisions in almost the same words, and they had
# been in every search -- answering a question twice over with drafts of
# itself and pushing the Act's own provisions down to make room. Taking
# them out of the default moved five queries up and none down, and every
# one of the fourteen is now in the top five. Every number here is one
# the suite measured, not a target somebody picked.
#
# The floors below are per group, and that matters. The eval set was then
# extended with six queries where the reader's words and the statute's
# words are simply different -- "breach" for contravention, "call a
# lawyer" for "communicate with a legal practitioner" -- and search
# answers none of them: MRR 0.019, nothing at rank 1, and five of the six
# finding nothing at all in twenty results.
#
# Averaging the two groups would produce a number that falls whenever a
# known weakness is written down and rises whenever it is deleted, which
# is the opposite of what a scoreboard is for. So the group that works
# has a ratchet and the group that does not has a record.
FLOOR_MRR = 0.823
FLOOR_AT_1 = 10
FLOOR_AT_5 = 14

# What the vocabulary gap scores today. Not a target and not a ratchet
# -- it is here so that anything which moves it, in either direction,
# shows up as a failing test that has to be looked at and re-stated.
#
# Re-stated once, from 0.019, when the Crimes Act was re-parsed: "can
# police take my fingerprints" went from nowhere to rank 16. The corpus
# moved, not the search. That is the honest reason and it is written down
# rather than absorbed -- a number nobody can account for is a number
# that stops meaning anything.
#
# It was briefly 0.029 against a *broken* corpus too, while two thirds of
# the Crimes Act was missing (see finished_units in review.py). Coming
# out at the same figure either way is a coincidence of these six
# queries, not a sign the corpus does not matter: the test that caught
# the breakage was test_the_eval_set_is_about_documents_that_exist, which
# noticed s322O had disappeared.
#
# Re-stated again, from 0.029, when the Evidence Act was re-parsed and
# came back some 25,000 lines longer. Exactly one query moved: "can
# police take my fingerprints", from rank 16 to rank 19. It still finds
# the right provision and finds it no worse -- there is simply more
# material sitting between the reader and it. Five of the six are
# untouched, four of them still nowhere at all, and the core group did
# not move (0.824). The corpus moved, not the search, which is the same
# reason as last time and is why this is a record rather than a ratchet:
# a number that quietly absorbed this would not be telling anybody
# anything.
GAP_MRR_TODAY = 0.027


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
    core = relevance.by_group(scored)["core"]
    assert core["mrr"] >= FLOOR_MRR, (
        f"MRR fell to {core['mrr']:.3f} from {FLOOR_MRR}\n\n" + relevance.report(scored))
    assert core["at_5"] >= FLOOR_AT_5, (
        f"queries answered in the top five fell to {core['at_5']} from {FLOOR_AT_5}\n\n"
        + relevance.report(scored))
    assert core["at_1"] >= FLOOR_AT_1, (
        f"queries answered at rank 1 fell to {core['at_1']} from {FLOOR_AT_1}\n\n"
        + relevance.report(scored))


def test_the_vocabulary_gap_is_where_it_was(scored):
    """Not a ratchet. These are the queries search cannot answer, and
    this asserts the number has not moved in either direction -- so that
    improving it is a deliberate act with a new number written down,
    rather than something nobody notices either way."""
    gap = relevance.by_group(scored)["vocabulary-gap"]

    assert round(gap["mrr"], 3) == GAP_MRR_TODAY, (
        f"the vocabulary gap now scores {gap['mrr']:.3f}, not {GAP_MRR_TODAY}. If this is "
        f"an improvement, say so here and record the new number.\n\n" + relevance.report(scored))


def test_the_statutes_own_words_still_work(scored):
    """The easy queries, which use the vocabulary the Act itself uses.
    Making the hard ones work must not cost these."""
    easy = {"meaning of family violence", "meaning of relative",
            "hearsay rule", "meaning of penalty units"}
    for entry in scored["queries"]:
        if entry["query"] in easy:
            assert entry["rank"] == 1, f"{entry['query']!r} fell to rank {entry['rank']}"
