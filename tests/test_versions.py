"""Tests for ai_pipeline/versions.py -- reading which expression of an Act
a PDF is off its own front matter.

The fixtures below are the real first-page text of the Acts in acts/,
reduced to the block that matters. Every Authorised Version prints it the
same way; a Bill and an Explanatory Memorandum print none of it."""
from ai_pipeline.versions import describe, discover_versions, parse_front_matter, read_front_matter

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
