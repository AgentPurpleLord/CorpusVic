"""Tests for ai_pipeline/endnotes.py -- reading an Act's closing Endnotes,
chiefly the Table of Amendments, which is a two-column table PyMuPDF hands
back with every value ahead of its own label.

The fixtures below are built from real measured coordinates off the
Criminal Procedure Act's own endnote pages (see conftest's own note on the
same convention for rule_parser's tests): the record title column at
x0=141.7, labels at 166.8, values at 265.9, and a label typeset on the same
baseline as the first line of its value but emitted *after* it."""
from ai_pipeline.endnotes import detect_endnotes_start, parse_endnotes, parse_citation

from conftest import line, page

TITLE_X = 141.7
LABEL_X = 166.8
VALUE_X = 265.9
SECTION_X = 127.6


def at(text, x0, y0, *, bold=False, size=9.0, page_no=1):
    l = line(text, x0=x0, bold=bold, size=size, page_no=page_no)
    l.y0 = y0
    l.y1 = y0 + 9
    return l


def _table_page(page_no=1, start_y=100.0):
    """One Table-of-Amendments page holding two records. Value lines are
    emitted *before* the label they belong to, exactly as extraction hands
    them over."""
    y = start_y
    lines = [
        at("2 Table of Amendments", SECTION_X, y, bold=True, size=11.0, page_no=page_no),
        at("This publication incorporates amendments made to the Test Act 2009", TITLE_X, y + 20, size=10.0, page_no=page_no),
        at("Test Act 2009, No. 7/2009", TITLE_X, y + 40, bold=True, page_no=page_no),
        at("10.3.09", VALUE_X, y + 50, page_no=page_no),
        at("Assent Date:", LABEL_X, y + 50.2, page_no=page_no),
        at("S. 438 on 1.1.12: s. 438;", VALUE_X, y + 60, page_no=page_no),
        at("Commencement Date:", LABEL_X, y + 60.2, page_no=page_no),
        at("s. 387P inserted on 3.10.18", VALUE_X, y + 70, page_no=page_no),
        at("Bus Safety Act 2009, No. 13/2009 (as amended by No. 68/2009)", TITLE_X, y + 90, bold=True, page_no=page_no),
        at("7.4.09", VALUE_X, y + 100, page_no=page_no),
        at("Assent Date:", LABEL_X, y + 100.2, page_no=page_no),
    ]
    return page(lines, page_no=page_no)


def test_detect_endnotes_start_finds_the_endnotes_title():
    pages = [
        page([at("Schedule 5—Transitional provisions", 145.0, 100, bold=True, size=16.0, page_no=1)], page_no=1),
        page([at("Endnotes", 265.6, 100, bold=True, size=16.0, page_no=2)], page_no=2),
    ]
    assert detect_endnotes_start(pages) == 2


def test_detect_endnotes_start_is_none_for_a_document_with_none():
    # A Bill and an Explanatory Memorandum have no endnotes at all.
    pages = [page([at("Clause 1 sets out the purposes.", 141.7, 100, page_no=1)], page_no=1)]
    assert detect_endnotes_start(pages) is None


def test_a_labels_value_is_not_read_before_its_label():
    # The bug this parser exists for: read in extraction order, every value
    # precedes the label it belongs to ("10.3.09" then "Assent Date:").
    result = parse_endnotes([_table_page()])

    first = result.amending_acts[0]
    assert first["fields"]["assent_date"] == "10.3.09"
    assert first["fields"]["commencement_date"] == "S. 438 on 1.1.12: s. 438; s. 387P inserted on 3.10.18"


def test_each_record_is_its_own_entry():
    result = parse_endnotes([_table_page()])

    assert [a["citation"] for a in result.amending_acts] == ["7/2009", "13/2009"]
    assert result.amending_acts[1]["fields"]["assent_date"] == "7.4.09"


def test_the_section_headings_are_read_as_sections():
    result = parse_endnotes([_table_page()])

    assert [(s["number"], s["heading"]) for s in result.sections] == [("2", "Table of Amendments")]
    assert "This publication incorporates" in result.sections[0]["text"]


def test_every_endnote_line_is_accounted_for():
    # Same invariant the rules engine holds itself to: nothing is silently
    # dropped, and the caller can check.
    result = parse_endnotes([_table_page()])

    assert result.lines_consumed == result.lines_total
    assert result.warnings == []


def test_a_document_with_no_table_of_amendments_keeps_its_text():
    prose = page([
        at("1 General information", SECTION_X, 100, bold=True, size=11.0),
        at("See www.legislation.vic.gov.au for Victorian Bills.", TITLE_X, 120, size=10.0),
    ])
    result = parse_endnotes([prose])

    assert result.amending_acts == []
    assert "legislation.vic.gov.au" in result.sections[0]["text"]
    assert any("no Table of Amendments columns" in w for w in result.warnings)


def test_parse_citation_reads_the_records_own_act_number():
    assert parse_citation("Test Act 2009, No. 7/2009")["citation"] == "7/2009"


def test_parse_citation_ignores_a_trailing_as_amended_by_qualifier():
    # "(as amended by No. 68/2009)" names a *different* Act.
    found = parse_citation("Bus Safety Act 2009, No. 13/2009 (as amended by No. 68/2009)")
    assert found["citation"] == "13/2009"


def test_parse_citation_reads_a_pre_1970s_five_digit_number():
    found = parse_citation("Interpretation of Legislation (Further Amendment) Act 1985, No. 10214/1985")
    assert found["citation"] == "10214/1985"
    assert found["is_statutory_rule"] is False


def test_parse_citation_marks_a_statutory_rule():
    found = parse_citation("Criminal Appeal Rules 1965, S.R. No. 144/1965")
    assert found["citation"] == "144/1965"
    assert found["is_statutory_rule"] is True


def test_a_record_title_that_wraps_onto_a_second_line_is_joined():
    y = 100.0
    lines = [
        at("2 Table of Amendments", SECTION_X, y, bold=True, size=11.0),
        at("Criminal Procedure Amendment (Consequential and Transitional Provisions)", TITLE_X, y + 20, bold=True),
        at("Act 2009, No. 68/2009", TITLE_X, y + 30, bold=True),
        at("24.11.09", VALUE_X, y + 40),
        at("Assent Date:", LABEL_X, y + 40.2),
    ]
    result = parse_endnotes([page(lines)])

    assert len(result.amending_acts) == 1
    assert result.amending_acts[0]["citation"] == "68/2009"
    assert result.amending_acts[0]["title"] == (
        "Criminal Procedure Amendment (Consequential and Transitional Provisions) Act 2009"
    )
