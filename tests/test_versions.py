"""Tests for corpus/versions.py -- reading which expression of an Act
a PDF is off its own front matter.

The fixtures below are the real first-page text of the Acts in acts/,
reduced to the block that matters. Every Authorised Version prints it the
same way; a Bill and an Explanatory Memorandum print none of it."""
from corpus.versions import (
    current_version,
    describe,
    discover_versions,
    document_slug,
    group_versions,
    parse_front_matter,
    read_front_matter,
    split_document_slug,
    work_directory,
)

AUTHORISED_VERSION = """Authorised by the Chief Parliamentary Counsel
i
Authorised Version No. 114
Criminal Procedure Act 2009
No. 7 of 2009
Authorised Version incorporating amendments as at
1 July 2026
TABLE OF PROVISIONS
Section
Page
"""

A_BILL = """PARLIAMENT OF VICTORIA
561018B.I-25/6/2008
BILL LA INTRODUCTION 25/6/2008
i
Evidence Bill 2008
TABLE OF PROVISIONS
Clause
Page
"""


def test_an_authorised_version_states_its_number_and_date():
    meta = parse_front_matter(AUTHORISED_VERSION)

    assert meta["version"] == 114
    assert meta["as_at"] == "2026-07-01"
    assert meta["as_at_printed"] == "1 July 2026"


def test_the_version_number_is_an_int_so_it_sorts():
    # 110 before 111 before 112 -- as strings, "110" sorts after "11".
    assert isinstance(parse_front_matter(AUTHORISED_VERSION)["version"], int)


def test_the_as_at_date_is_iso_so_it_sorts():
    # "1 July 2026" and "4 March 2026" sort alphabetically into nonsense.
    march = parse_front_matter(AUTHORISED_VERSION.replace("1 July 2026", "4 March 2026"))
    july = parse_front_matter(AUTHORISED_VERSION)

    assert march["as_at"] < july["as_at"]


def test_the_work_is_identified_separately_from_the_version():
    # The title and "No. 7 of 2009" are fixed for the Act's whole life;
    # the version number and date change with every reprint.
    meta = parse_front_matter(AUTHORISED_VERSION)

    assert meta["title"] == "Criminal Procedure Act 2009"
    assert meta["act_no"] == "7"
    assert meta["year"] == 2009


def test_the_version_number_is_not_mistaken_for_the_act_number():
    # "Authorised Version No. 114" sits directly above "No. 7 of 2009".
    assert parse_front_matter(AUTHORISED_VERSION)["act_no"] == "7"


def test_a_pre_1970s_five_digit_act_number_is_read():
    text = AUTHORISED_VERSION.replace("No. 7 of 2009", "No. 10096 of 1984")
    assert parse_front_matter(text)["act_no"] == "10096"


def test_a_bill_has_no_version_and_that_is_not_a_failure():
    meta = parse_front_matter(A_BILL)

    assert meta["version"] is None
    assert meta["as_at"] is None
    assert meta["title"] == "Evidence Bill 2008"  # still identifies itself


def test_an_unreadable_date_leaves_the_date_out_rather_than_guessing():
    text = AUTHORISED_VERSION.replace("1 July 2026", "1 Quintilis 2026")
    meta = parse_front_matter(text)

    assert meta["as_at"] is None
    assert meta["as_at_printed"] == "1 Quintilis 2026"  # kept as printed
    assert meta["version"] == 114


def test_a_missing_file_reads_as_an_unversioned_document(tmp_path):
    # Metadata about a document; not having it never fails a parse that
    # otherwise succeeded.
    assert read_front_matter(tmp_path / "nope.pdf")["version"] is None


def test_describe_says_only_what_the_document_states():
    assert describe(parse_front_matter(AUTHORISED_VERSION)) == (
        "Criminal Procedure Act 2009 — Version 114, as at 1 July 2026"
    )
    assert describe(parse_front_matter(A_BILL)) == "Evidence Bill 2008"


def test_discover_versions_of_a_directory_that_does_not_exist(tmp_path):
    assert discover_versions(tmp_path / "nope") == []


def test_discover_versions_reads_the_real_criminal_procedure_act():
    """The five Authorised Versions in acts/criminal-procedure-act/,
    ordered by the Act's own version number rather than by filename or
    date -- it is the only one of the three the Act guarantees to be
    sequential."""
    found = discover_versions("acts/criminal-procedure-act")

    assert [v["version"] for v in found] == [110, 111, 112, 113, 114]
    assert [v["as_at"] for v in found] == [
        "2026-03-04", "2026-04-01", "2026-04-26", "2026-05-01", "2026-07-01",
    ]
    # All five are expressions of the same work.
    assert {v["act_no"] for v in found} == {"7"}


# ---------------------------------------------------------------------
# Works and their versions
#
# Everything in this pipeline addresses a document by a single slug --
# the parse's filename, the review database's key, the browse URL -- so a
# version gets a slug of its own rather than a second identifier threaded
# alongside the first.
# ---------------------------------------------------------------------

def test_a_version_is_addressed_by_a_slug_of_its_own():
    assert document_slug("criminal-procedure-act", 114) == "criminal-procedure-act-v114"


def test_an_unversioned_document_is_just_its_own_slug():
    # A Bill is not version 1 of anything.
    assert document_slug("evidence-bill", None) == "evidence-bill"


def test_a_document_slug_decomposes_back_to_its_work_and_version():
    assert split_document_slug("criminal-procedure-act-v114") == ("criminal-procedure-act", 114)
    assert split_document_slug("evidence-bill") == ("evidence-bill", None)


def test_a_slug_that_merely_ends_in_a_number_is_not_a_version():
    # "criminal-procedure-bill-2008" must not read as version 2008 of
    # "criminal-procedure-bill" -- the marker is "-v" then digits.
    assert split_document_slug("criminal-procedure-bill-2008") == ("criminal-procedure-bill-2008", None)
    assert split_document_slug("crimes-act-50") == ("crimes-act-50", None)


def test_composing_and_decomposing_round_trips():
    for work, version in (("criminal-procedure-act", 114), ("evidence-act", 27), ("a-bill", None)):
        assert split_document_slug(document_slug(work, version)) == (work, version)


def test_a_pdf_in_a_work_directory_belongs_to_that_work(tmp_path):
    acts = tmp_path / "acts"
    (acts / "criminal-procedure-act").mkdir(parents=True)
    versioned = acts / "criminal-procedure-act" / "cpa-114.pdf"
    versioned.write_bytes(b"")

    assert work_directory(versioned, acts) == "criminal-procedure-act"


def test_a_pdf_directly_in_acts_belongs_to_no_work(tmp_path):
    """The opt-in is positional on purpose. An Act states its version
    whether or not anyone wants it tracked, so reading that alone would
    have renamed every document already here the moment this landed, and
    taken each one's review work with it."""
    acts = tmp_path / "acts"
    acts.mkdir()
    loose = acts / "sentencing-act.pdf"
    loose.write_bytes(b"")

    assert work_directory(loose, acts) is None


def test_group_versions_orders_a_works_versions_oldest_first():
    documents = [
        {"slug": "criminal-procedure-act-v113"},
        {"slug": "criminal-procedure-act-v110"},
        {"slug": "criminal-procedure-act-v114"},
        {"slug": "evidence-act-v27"},
    ]

    works = group_versions(documents)

    assert [d["version"] for d in works["criminal-procedure-act"]] == [110, 113, 114]
    assert works["evidence-act"][0]["work"] == "evidence-act"


def test_group_versions_leaves_out_what_is_not_a_version_of_anything():
    # A Bill and an EM have no timeline to be points on.
    works = group_versions([{"slug": "criminal-procedure-bill-2008"}, {"slug": "criminal-procedure-act-v114"}])

    assert list(works) == ["criminal-procedure-act"]


def test_the_current_version_is_the_highest_numbered():
    versions = [{"version": 110}, {"version": 114}, {"version": 112}]

    assert current_version(versions)["version"] == 114
    assert current_version([]) is None
