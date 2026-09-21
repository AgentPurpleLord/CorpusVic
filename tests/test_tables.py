"""Tests for corpus/tables.py -- recovering a printed table from
where its lines sit on the page.

The coordinates in these fixtures are the real ones, taken off the Acts
each test names: a table's geometry is the whole of its meaning here, so
inventing plausible-looking numbers would be testing nothing.
"""
from corpus.parsing import tables
from corpus.parsing.tables import find_table, format_rows, split_rows

from conftest import line


def _at(text, x0, y0, page=1, bold=False):
    return line(text, x0=x0, y0=y0, page_no=page, bold=bold)


def _cpa_s7a_lines():
    """Criminal Procedure Act s 7A, pages 46-47. Two columns, cells that
    wrap, a header row, and a second row that starts on the next page."""
    return [
        _at("Table", 309.36, 558.16, bold=True),
        _at("Column 1", 222.60, 579.49), _at("Column 2", 336.00, 579.49),
        _at("An offence against a", 222.60, 597.20), _at("A defence that would", 335.99, 597.20),
        _at("child under the age of 16", 222.60, 608.73), _at("be available under", 335.99, 608.73),
        _at("section 45(4) of the Crimes", 335.99, 620.09),
        _at("Act 1958 if the person were", 335.99, 631.61),
        _at("An offence against a", 222.60, 160.52, page=2), _at("A defence that would", 335.99, 160.52, page=2),
        _at("16 or 17 year old child", 222.60, 172.05, page=2), _at("be available under", 335.99, 171.93, page=2),
        _at("7B Uncertainty about time", 161.64, 250.01, page=2, bold=True),
    ]


def test_two_columns_come_out_as_two_columns():
    """Read as a stream of lines, this section came out as "An offence
    against a / A defence that would / child under the age of 16 / be
    available under / ..." -- two columns interleaved a line at a time
    into one unreadable paragraph. No pattern over that text could put it
    back; the information is in the x coordinate."""
    table = find_table(_cpa_s7a_lines(), 0)

    assert table is not None
    assert table.heading == "Table"
    assert table.rows[0] == ["Column 1", "Column 2"]
    assert table.rows[1][0] == "An offence against a child under the age of 16"
    assert table.rows[1][1].startswith("A defence that would be available under section 45(4)")


def test_a_row_that_starts_on_the_next_page_is_a_new_row():
    table = find_table(_cpa_s7a_lines(), 0)
    assert len(table.rows) == 3
    assert table.rows[2] == [
        "An offence against a 16 or 17 year old child",
        "A defence that would be available under",
    ]


def test_the_table_ends_at_the_next_heading():
    lines = _cpa_s7a_lines()
    table = find_table(lines, 0)
    assert table.end == len(lines) - 1  # the bold "7B ..." line is not in it


def test_a_table_of_one_line_cells_is_not_one_enormous_row():
    """The Interpretation Act's style-change table: ten rows, not one
    cell in it wrapped. Reading the smallest gap in a block as its line
    pitch works only where something *did* wrap -- here every gap is a
    row gap, and treating them as wraps collapsed the whole table."""
    rows = [
        ("column 1", "column 2", 267.20),
        ("old style", "new style", 284.00),
        ("Sub-division", "Subdivision", 304.32),
        ("sub-section", "subsection", 324.12),
        ("sub-paragraph", "subparagraph", 343.92),
        ("sub-clause", "subclause", 363.72),
    ]
    lines = [l for left, right, y in rows for l in (_at(left, 180.84, y), _at(right, 322.68, y))]

    table = find_table(lines, 0)

    assert [r[0] for r in table.rows] == [left for left, _r, _y in rows]
    assert [r[1] for r in table.rows] == [right for _l, right, _y in rows]


def test_a_row_gap_only_a_quarter_wider_than_a_wrap_still_splits():
    """The Crimes Act's s 320 table: 11.5pt wraps, 14.5pt rows. At a
    flatter threshold its rows ran together in pairs."""
    lines = [
        _at("Column 1", 223.46, 349.53), _at("Column 2", 340.34, 349.53),
        _at("Attempt to pervert the", 223.46, 416.81), _at("Level 2 imprisonment", 340.34, 416.81),
        _at("course of justice", 223.46, 428.33), _at("(25 years maximum)", 340.34, 428.33),
        _at("Breach of prison", 223.46, 442.85), _at("Level 6 imprisonment", 340.34, 442.85),
        _at("(5 years maximum)", 340.34, 454.31),
    ]

    table = find_table(lines, 0)

    assert table.rows[1] == ["Attempt to pervert the course of justice", "Level 2 imprisonment (25 years maximum)"]
    assert table.rows[2] == ["Breach of prison", "Level 6 imprisonment (5 years maximum)"]


def test_a_repeated_column_heading_is_dropped():
    """A table continuing onto the next page reprints its headings for a
    reader who has turned over. In one continuous table they are not a
    row of it -- the Crimes Act's s 321P table came out with "Column 1 |
    Column 2" four times down the middle of its own data."""
    lines = [
        _at("Column 1", 223.46, 349.53), _at("Column 2", 340.34, 349.53),
        _at("Level 1 imprisonment", 223.46, 380.00), _at("Level 2 imprisonment", 340.34, 380.00),
        _at("Column 1", 223.46, 160.00, page=2), _at("Column 2", 340.34, 160.00, page=2),
        _at("Level 3 imprisonment", 223.46, 190.00, page=2), _at("Level 4 imprisonment", 340.34, 190.00, page=2),
    ]

    table = find_table(lines, 0)

    assert table.rows == [
        ["Column 1", "Column 2"],
        ["Level 1 imprisonment", "Level 2 imprisonment"],
        ["Level 3 imprisonment", "Level 4 imprisonment"],
    ]


def test_a_repealed_row_is_kept_where_it_was():
    """"* * * *" across the table's width is Victoria's "a row used to be
    here" marker. Dropping it would leave no sign anything had been."""
    lines = [
        _at("Column 1", 223.46, 349.53), _at("Column 2", 340.34, 349.53),
        _at("*", 275.84, 400.17), _at("*", 332.54, 400.17), _at("*", 389.26, 400.17),
        _at("Breach of prison", 223.46, 442.85), _at("Level 6 imprisonment", 340.34, 442.85),
    ]

    table = find_table(lines, 0)

    assert table.rows[1] == ["* * *", ""]


def test_an_asterisk_row_cannot_start_a_table():
    """It is the widest-spaced thing in these Acts -- four asterisks
    across the measure, every pair further apart than any real column
    gap -- so on its own it looks more like a table than a table does."""
    lines = [
        _at("*", 275.84, 400.17), _at("*", 332.54, 400.17),
        _at("*", 275.84, 420.17), _at("*", 332.54, 420.17),
    ]
    assert find_table(lines, 0) is None


def test_a_long_section_number_beside_its_title_is_not_a_table():
    """The Crimes Act's s 464ZGFB: the number is long enough to be its
    own text run, 64pt from the heading beside it, on the same baseline.
    It is the one thing in these Acts that looks exactly like a
    two-column row -- and the one thing set in bold, which no table cell
    is."""
    lines = [
        _at("464ZGFB", 141.80, 565.83, bold=True),
        _at("Destruction of samples given by police and", 205.58, 565.83, bold=True),
        _at("VIFM personnel and storage of DNA", 205.58, 579.63, bold=True),
    ]
    assert find_table(lines, 0) is None


def test_an_indent_is_not_a_column():
    """A paragraph's hanging indent is about 20pt in these Acts; a column
    break is over 100."""
    lines = [
        _at("(a) something", 216.24, 157.01),
        _at("continues here", 235.32, 157.01),
    ]
    assert find_table(lines, 0) is None


def test_a_single_row_is_not_a_table():
    lines = [_at("left", 223.46, 349.53), _at("right", 340.34, 349.53)]
    assert find_table(lines, 0) is None


def test_rows_round_trip_through_the_stored_text():
    """Every renderer reads a table back out of its text, so a reviewer's
    own edit to that text is a real edit to the table."""
    rows = [["Column 1", "Column 2"], ["an offence", "a defence"]]
    assert split_rows(format_rows(rows)) == rows


def test_split_rows_tolerates_a_hand_edited_table():
    """Whitespace around a cell, and a blank line, are what a person
    typing into a text box produces."""
    assert split_rows("a | b\n\n  c  |d  \n") == [["a", "b"], ["c", "d"]]


def test_a_caption_has_to_be_short_and_bold():
    """"Table" is a caption. The sentence introducing it ("...specified
    opposite it in column 2 of the Table.") is not, and neither is a
    plain line that happens to sit above one."""
    rows = [
        _at("Column 1", 223.46, 349.53), _at("Column 2", 340.34, 349.53),
        _at("first", 223.46, 380.00), _at("second", 340.34, 380.00),
    ]
    assert find_table([_at("TABLE", 314.78, 331.53, bold=True), *rows], 0).heading == "TABLE"
    assert find_table([_at("Table", 314.78, 331.53), *rows], 0) is None
    long_caption = "of imprisonment specified opposite it in column 2 of the Table and more"
    assert find_table([_at(long_caption, 209.84, 331.53, bold=True), *rows], 0) is None


def test_the_separator_appears_nowhere_in_the_corpus():
    """Why a pipe is safe to store rows with: there is not one in any
    parsed Act, Bill or Explanatory Memorandum in this repository."""
    assert tables.CELL_SEPARATOR.strip() == "|"
