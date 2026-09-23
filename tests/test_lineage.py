"""Tests for corpus/domain/lineage.py -- one provision followed through
the versions of a work, and what a reviewer still has to check in each.

The promise is that adding a version costs only its differences: a
provision whose raw words match a version someone already reviewed is
reviewed, and one that differs is put in front of a person."""
from corpus.domain import diffing
from corpus.domain.lineage import (
    FOLLOWS, INHERITS, REVIEWED, TO_REVIEW,
    loose_notes, provision_chains, review_status, signatures, wording_at,
)

from conftest import make_node

S1 = ("provision", None, "1")
S2 = ("provision", None, "2")
S3 = ("provision", None, "3")


def _act(**sections) -> list[dict]:
    """Each keyword is a section number (s1=...) and its one subsection's text."""
    nodes = [make_node("part", "1", "Preliminary")]
    for name, text in sections.items():
        nodes.append(make_node("section", name[1:], "Heading", ""))
        nodes.append(make_node("subsection", "1", None, text))
    return nodes


def _raw(**versions) -> dict:
    return {int(v[1:]): signatures(nodes) for v, nodes in versions.items()}


# ---------------------------------------------------------------------
# What a version still needs reviewed
# ---------------------------------------------------------------------

def test_an_older_version_inherits_what_the_current_one_reviewed():
    raw = _raw(v1=_act(s1="a", s2="b"), v2=_act(s1="a", s2="b"))

    status = review_status([1, 2], raw, {2: {S1, S2}})

    assert status[1][S1] == {"status": INHERITS, "source": 2, "source_key": S1}
    assert status[2][S1]["status"] == REVIEWED


def test_only_a_difference_lands_in_an_older_versions_queue():
    raw = _raw(v1=_act(s1="a", s2="old words"), v2=_act(s1="a", s2="new words"))

    status = review_status([1, 2], raw, {2: {S1, S2}})

    assert status[1][S2]["status"] == TO_REVIEW
    assert status[1][S1]["status"] == INHERITS


def test_an_older_version_follows_the_current_one_until_it_is_reviewed():
    """Not the older version's work: the same words are reviewed once, in
    the current version, and the older one picks that up."""
    raw = _raw(v1=_act(s1="a"), v2=_act(s1="a"))

    status = review_status([1, 2], raw, {})

    assert status[1][S1] == {"status": FOLLOWS, "source": 2, "source_key": S1}
    assert status[2][S1]["status"] == TO_REVIEW


def test_a_new_current_version_inherits_from_the_reviewed_one_before_it():
    raw = _raw(v1=_act(s1="a", s2="b"), v2=_act(s1="a", s2="c"))

    status = review_status([1, 2], raw, {1: {S1, S2}})

    assert status[2][S1] == {"status": INHERITS, "source": 1, "source_key": S1}
    assert status[2][S2]["status"] == TO_REVIEW


def test_inheritance_never_crosses_a_version_where_the_words_differed():
    raw = _raw(v1=_act(s1="a"), v2=_act(s1="b"), v3=_act(s1="a"))

    status = review_status([1, 2, 3], raw, {3: {S1}})

    assert status[1][S1]["status"] == TO_REVIEW


def test_a_restructured_unit_is_not_lent_to_another_version():
    raw = _raw(v1=_act(s1="a"), v2=_act(s1="a"))

    status = review_status([1, 2], raw, {2: {S1}}, blocked={2: {S1}})

    assert status[1][S1]["status"] == TO_REVIEW


def test_an_inserted_provision_is_to_review_in_the_version_that_gained_it():
    raw = _raw(v1=_act(s1="a"), v2=_act(s1="a", s2="b"))

    assert review_status([1, 2], raw, {2: {S1}})[2][S2]["status"] == TO_REVIEW


def test_a_carried_from_link_lets_a_moved_provision_inherit():
    raw = _raw(v1=_act(s1="a", s2="moved words"), v2=_act(s1="a", s3="moved words"))

    status = review_status([1, 2], raw, {2: {S1, S3}}, links={2: {S3: S2}})

    assert status[1][S2] == {"status": INHERITS, "source": 2, "source_key": S3}


# ---------------------------------------------------------------------
# A provision's wordings across the versions
# ---------------------------------------------------------------------

def _doc(version, nodes, checked=(), unattached=None, acts=None):
    doc = {
        "version": version, "as_at": f"2026-0{version}-01", "as_at_printed": f"1 Month {version}",
        "raw": signatures(nodes), "effective": diffing.provisions(nodes), "checked": set(checked),
        "loose_notes": loose_notes(unattached or []),
    }
    if acts is not None:
        doc["amending_acts"] = set(acts)
    return doc


def _chain(result, version, key):
    return result["chains"][result["by_key"][(version, key)]]


def test_a_provision_that_never_changed_has_one_wording():
    result = provision_chains([_doc(1, _act(s1="a")), _doc(2, _act(s1="a"))])

    assert len(_chain(result, 2, S1)["wordings"]) == 1


def test_an_amendment_starts_a_new_wording_named_by_its_margin_note():
    old = _act(s1="a person may appeal")
    new = _act(s1="a person may appeal within 28 days")
    new[-1]["history"] = [{"raw": "S. 1(1) amended by No. 5/2026 s. 3."}]

    wordings = _chain(provision_chains([_doc(1, old), _doc(2, new)]), 2, S1)["wordings"]

    assert [w["versions"] for w in wordings] == [[1], [2]]
    assert wordings[0]["ended_by"]["change"] == "changed"
    assert wordings[0]["ended_by"]["notes"] == ["S. 1(1) amended by No. 5/2026 s. 3."]
    assert wording_at({"wordings": wordings}, 2) == 1


# The Act says what changed, not the parser: a text difference is only an
# amendment where the Table of Amendments took in an Act and the
# provision's own notes cite one they didn't before.

def _noted(text, *notes):
    nodes = _act(s1=text)
    nodes[-1]["history"] = [{"raw": n} for n in notes]
    return nodes


def _versions(result):
    return [w["versions"] for w in _chain(result, 1, S1)["wordings"]]


def test_a_line_wrap_the_parser_read_differently_is_not_an_amendment():
    old = _noted("must file a charge- sheet.", "S. 1(1) amended by No. 5/2020 s. 3.")
    new = _noted("must file a charge-sheet.", "S. 1(1) amended by No. 5/2020 s. 3.")

    result = provision_chains([_doc(1, old, acts={"5/2020"}), _doc(2, new, acts={"5/2020", "7/2026"})])

    assert _versions(result) == [[1, 2]]


def test_a_note_citing_a_new_act_makes_the_difference_an_amendment():
    old = _noted("a person may appeal")
    new = _noted("a person may appeal within 28 days", "S. 1(1) amended by No. 7/2026 s. 3.")

    result = provision_chains([_doc(1, old, acts={"5/2020"}), _doc(2, new, acts={"5/2020", "7/2026"})])

    wordings = _chain(result, 1, S1)["wordings"]
    assert [w["versions"] for w in wordings] == [[1], [2]]
    assert wordings[0]["ended_by"]["notes"] == ["S. 1(1) amended by No. 7/2026 s. 3."]


def test_an_act_commencing_in_stages_still_amends():
    """Its citation is new to the provision though the Act was already in
    the older reprint's table -- only something else of it had commenced."""
    old = _noted("a person may appeal")
    new = _noted("a person may appeal within 28 days", "S. 1(1) amended by No. 5/2020 s. 9.")

    result = provision_chains([_doc(1, old, acts={"5/2020"}), _doc(2, new, acts={"5/2020", "7/2026"})])

    assert _versions(result) == [[1], [2]]


def test_a_reprint_for_a_commencement_alone_still_amends():
    """The CPA's 26 April 2026 reprint added no Act to its table; nine of
    its provisions were amended by one listed since the reprint before."""
    old = _noted("a person may appeal")
    new = _noted("a person may appeal within 28 days", "S. 1(1) amended by No. 7/2026 s. 3.")

    result = provision_chains([_doc(1, old, acts={"7/2026"}), _doc(2, new, acts={"7/2026"})])

    assert _versions(result) == [[1], [2]]


def test_a_note_citing_an_act_the_table_does_not_list_is_not_enough():
    old = _noted("a person may appeal")
    new = _noted("a person may appeal within 28 days", "S. 1(1) amended by No. 7/2026 s. 3.")

    result = provision_chains([_doc(1, old, acts={"5/2020"}), _doc(2, new, acts={"5/2020"})])

    assert _versions(result) == [[1, 2]]


def test_a_loose_note_counts_as_the_provisions_own():
    old = _noted("a person may appeal")
    new = _noted("a person may appeal within 28 days")
    unattached = [{"raw": "S. 1(1) substituted by No. 7/2026 s. 3.", "section": "1"}]

    result = provision_chains([_doc(1, old, acts={"5/2020"}),
                               _doc(2, new, acts={"5/2020", "7/2026"}, unattached=unattached)])

    assert _versions(result) == [[1], [2]]


def test_without_a_table_of_amendments_the_text_decides():
    """A table that failed to parse must not read as "nothing incorporated"
    and hide the amendments behind it."""
    old = _noted("must file a charge- sheet.")
    new = _noted("must file a charge-sheet.")

    result = provision_chains([_doc(1, old, acts={"5/2020"}), _doc(2, new, acts=set())])

    assert _versions(result) == [[1], [2]]


def test_an_insertion_and_a_repeal_are_absent_wordings():
    result = provision_chains([
        _doc(1, _act(s1="a")),
        _doc(2, _act(s1="a", s2="new")),
        _doc(3, _act(s2="new"), unattached=[{"raw": "S. 1 repealed by No. 9/2026 s. 4.", "section": "1"}]),
    ])

    inserted = _chain(result, 2, S2)["wordings"]
    assert [(w["absent"], w["versions"]) for w in inserted] == [(True, [1]), (False, [2, 3])]
    assert inserted[0]["ended_by"]["change"] == "inserted"

    repealed = _chain(result, 1, S1)["wordings"]
    assert [(w["absent"], w["versions"]) for w in repealed] == [(False, [1, 2]), (True, [3])]
    assert repealed[0]["ended_by"]["notes"] == ["S. 1 repealed by No. 9/2026 s. 4."]


def test_equal_raw_parses_are_one_wording_whatever_a_reviewer_changed():
    """A correction in one version is not an amendment by Parliament."""
    raw_nodes = _act(s1="the acused")
    reviewed = _act(s1="the accused")
    doc1 = _doc(1, raw_nodes)
    doc2 = {**_doc(2, reviewed, checked={S1}), "raw": signatures(raw_nodes)}

    wordings = _chain(provision_chains([doc1, doc2]), 2, S1)["wordings"]

    assert len(wordings) == 1
    assert wordings[0]["provision"]["text"].endswith("the accused")
    assert wordings[0]["checked"]


def test_a_carried_from_link_joins_two_numbers_into_one_chain():
    result = provision_chains([_doc(1, _act(s2="words")), _doc(2, _act(s3="words, amended"))],
                              links={2: {S3: S2}})

    assert result["by_key"][(1, S2)] == result["by_key"][(2, S3)]
    assert len(_chain(result, 2, S3)["wordings"]) == 2


# Where the amending Acts first in a reprint have been fetched, their
# instructions decide (dashboard._act_amended), not the margin notes.

def test_an_act_instruction_for_the_provision_makes_the_difference_real():
    old = _noted("a person may appeal")
    new = _noted("a person may appeal within 28 days")

    result = provision_chains([_doc(1, old, acts={"5/2020"}),
                               {**_doc(2, new, acts={"5/2020"}), "act_amended": {S1}}])

    assert _versions(result) == [[1], [2]]


def test_with_acts_fetched_a_note_alone_does_not_make_a_change():
    """A margin note is only as good as the parse that placed it; the Act
    names the provision itself, and it names nothing here."""
    old = _noted("must file a charge- sheet.")
    new = _noted("must file a charge-sheet.", "S. 1(1) amended by No. 7/2026 s. 3.")

    result = provision_chains([_doc(1, old, acts={"7/2026"}),
                               {**_doc(2, new, acts={"7/2026"}), "act_amended": set()}])

    assert _versions(result) == [[1, 2]]
