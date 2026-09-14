"""
Tables in the printed Act, recovered from where the lines sit on the page.

A table is the one thing in a legislative PDF whose meaning is entirely in
its geometry. Read as a stream of lines -- which is what everything else
here is -- the Criminal Procedure Act's s 7A table came out as "An offence
against a / A defence that would / child under the age of 16 / be
available under / ...", two columns interleaved a line at a time into one
unreadable paragraph inside subsection (3). No pattern over that text
could ever put it back, because the information that "A defence that
would" belongs beside the line above rather than after it was never in the
text at all. It is in the x coordinate.

So this works from the coordinates:

  * Two lines sharing a baseline, far apart horizontally, is a thing
    ordinary flowed prose cannot produce. That is what starts a table.
    Their x positions are its columns, and the block runs on for as long
    as every line keeps landing in one of them.
  * Rows come from the *gaps* between baselines. A cell that wraps puts
    its next line exactly one line-pitch down; a new row leaves more than
    that. Where every gap in the block is the same, nothing wrapped
    anywhere, and every baseline is its own row.

What comes out is stored as text, not as a structure bolted onto the
node: rows one per line, cells separated by a pipe. That keeps a table
exactly as reviewable and exactly as editable as every other kind of
provision -- a reviewer who sees a cell split in the wrong place fixes it
by typing, in the same box they fix everything else in, and the renderers
parse it back. A structure stored beside the text would have had to be
carried through the verified table, the correction log, the re-anchoring
and both exports, and would have been silently dropped by the first one
that did not know about it. The pipe is safe: there is not one in the
entire parsed corpus.
"""
from dataclasses import dataclass, field

from .extract import BodyLine, join_printed_line

# Two lines count as sharing a baseline within this many points. Real
# tables set their cells' first lines off the same baseline, but a
# renderer's own rounding drifts them a fraction apart.
BASELINE_TOLERANCE = 3.0
# How far apart two x positions must be before they are different columns
# rather than one column and an indent. A subparagraph's hanging indent is
# around 20pt in these Acts; a column break is over 100.
MIN_COLUMN_GAP = 40.0
# How close a line's x must be to a column's to belong to it.
COLUMN_TOLERANCE = 4.0
# How far apart the two kinds of baseline gap -- a cell wrapping, and a
# new row -- have to be before a block is read as having both. Below
# this, its gaps are all one kind and every baseline is a row of its own.
# Every table in this corpus sits well clear of it either way: the
# Criminal Procedure Act's s 7A table jumps 1.52x from its wraps to its
# rows and the Crimes Act's s 320 table 1.26x, while the Interpretation
# Act's style-change table, which has no wrapped cell anywhere, varies by
# only 1.18x across all of its rows.
WRAP_ROW_SEPARATION = 1.22
# A caption above a table ("Table", "Table 1") -- bold, short, and on its
# own line. Longer than this and it is a sentence, not a caption.
MAX_CAPTION_CHARS = 60

CELL_SEPARATOR = " | "


@dataclass
class DetectedTable:
    """Where a table is in the line list, and what it says."""
    start: int
    end: int          # exclusive
    rows: list[list[str]]
    heading: "str | None" = None
    columns: list[float] = field(default_factory=list)

    @property
    def text(self) -> str:
        return format_rows(self.rows)


def format_rows(rows: list[list[str]]) -> str:
    """Rows as a table node's stored text."""
    return "\n".join(CELL_SEPARATOR.join(cells) for cells in rows)


def split_rows(text: str) -> list[list[str]]:
    """A table node's stored text, back as rows of cells.

    Every renderer goes through this rather than reading the text
    directly, so a reviewer's own edit to the text is a real edit to the
    table."""
    return [
        [cell.strip() for cell in row.split("|")]
        for row in (text or "").split("\n")
        if row.strip()
    ]


def _columns_at(lines: list[BodyLine], index: int) -> "list[float] | None":
    """The column positions of the row starting at `index`, or None if the
    lines there aren't side by side at all.

    Bold disqualifies it. A table's cells are set in plain body text; a
    *bold* pair on one baseline is a section heading whose number was long
    enough to be its own run ("464ZGFB" and "Destruction of samples given
    by police and..." sit 64pt apart on the same line), which is the one
    thing in these Acts that otherwise looks exactly like a two-column
    row."""
    if lines[index].bold or lines[index].text.strip() == "*":
        # An asterisk row is the widest-spaced thing in these Acts --
        # four of them across the measure, every pair further apart than
        # any real column gap. It can sit *inside* a table (a repealed
        # row) but it can never be the evidence that one is starting.
        return None
    baseline = lines[index].y0
    xs = [lines[index].x0]
    j = index + 1
    while j < len(lines) and abs(lines[j].y0 - baseline) <= BASELINE_TOLERANCE:
        if lines[j].page_no != lines[index].page_no or lines[j].bold or lines[j].text.strip() == "*":
            break
        xs.append(lines[j].x0)
        j += 1
    if len(xs) < 2:
        return None
    xs.sort()
    if any(b - a < MIN_COLUMN_GAP for a, b in zip(xs, xs[1:])):
        return None
    return xs


def _column_of(x0: float, columns: list[float]) -> "int | None":
    for i, column in enumerate(columns):
        if abs(x0 - column) <= COLUMN_TOLERANCE:
            return i
    return None


def _row_breaks(baselines: list[tuple[int, float]]) -> set[int]:
    """Which of these (page, y) baselines start a new row.

    A block's baseline gaps come in at most two sizes: the leading inside
    a cell that wraps, and the wider step to the next row. So the gaps are
    sorted and cut at the *first* proportional jump big enough to be the
    boundary between them -- everything up to it is a wrap, everything
    past it a row. The first rather than the widest, because a table
    whose rows are separated by more than one size of gap (one blank line
    here, a repealed row there) would otherwise have its cut placed at
    the largest of those and read the smaller ones as wraps.

    Where there is no such jump, the gaps are all one kind, which means
    nothing wrapped anywhere and every baseline is its own row. That case
    has to be recognised rather than assumed away: a table of one-line
    cells (the Interpretation Act's old-style/new-style table is ten of
    them) has nothing but row gaps, and reading the smallest of those as a
    line pitch collapses the whole table into a single row."""
    gaps = [
        b_y - a_y
        for (a_page, a_y), (b_page, b_y) in zip(baselines, baselines[1:])
        if a_page == b_page
    ]
    if not gaps:
        return set(range(len(baselines)))
    ordered = sorted(gaps)
    widest_wrap = None
    for smaller, larger in zip(ordered, ordered[1:]):
        if smaller <= 0 or larger / smaller >= WRAP_ROW_SEPARATION:
            widest_wrap = smaller
            break
    if widest_wrap is None:
        return set(range(len(baselines)))

    breaks = {0}
    for i, ((a_page, a_y), (b_page, b_y)) in enumerate(zip(baselines, baselines[1:]), start=1):
        # A page break ends the row: a cell wrapping across one is far
        # rarer than a row simply being the first thing on the next page.
        if a_page != b_page or (b_y - a_y) > widest_wrap:
            breaks.add(i)
    return breaks


def find_table(lines: list[BodyLine], start: int) -> "DetectedTable | None":
    """The table beginning at `lines[start]`, if one does.

    `start` may be the table's own first row, or a short bold caption
    directly above it, which becomes the table's heading."""
    heading = None
    first = start
    columns = _columns_at(lines, first)
    if columns is None:
        caption = lines[start]
        if (
            caption.bold
            and len(caption.text.strip()) <= MAX_CAPTION_CHARS
            and start + 1 < len(lines)
        ):
            columns = _columns_at(lines, start + 1)
            if columns is not None:
                heading = caption.text.strip()
                first = start + 1
    if columns is None:
        return None

    block: list[tuple[int, BodyLine]] = []
    index = first
    while index < len(lines):
        if lines[index].bold:
            # A bold line ends the table for the same reason one can't
            # start it: it is the next heading, not another cell.
            break
        text = lines[index].text.strip()
        if text == "*":
            # Victoria's "a row used to be here" marker, printed across
            # the table's width as a row of its own rather than in any
            # column. Kept, in the first column, because a repealed row
            # is part of what the table says; dropping it would leave no
            # sign that anything had been there.
            column = 0
        else:
            column = _column_of(lines[index].x0, columns)
            if column is None:
                break
        block.append((column, lines[index]))
        index += 1

    baselines: list[tuple[int, float]] = []
    for _column, line in block:
        key = (line.page_no, line.y0)
        if not baselines or line.page_no != baselines[-1][0] or abs(line.y0 - baselines[-1][1]) > BASELINE_TOLERANCE:
            baselines.append(key)
    breaks = _row_breaks(baselines)

    rows: list[list[str]] = []
    baseline_index = -1
    for column, line in block:
        key = (line.page_no, line.y0)
        if baseline_index < 0 or key[0] != baselines[baseline_index][0] or abs(key[1] - baselines[baseline_index][1]) > BASELINE_TOLERANCE:
            baseline_index += 1
            if baseline_index in breaks:
                rows.append(["" for _ in columns])
        rows[-1][column] = join_printed_line(rows[-1][column], line.text.strip())

    rows = _drop_repeated_headings(rows)
    if len(rows) < 2:
        # One row is a coincidence of layout, not a table.
        return None
    return DetectedTable(start=start, end=index, rows=rows, heading=heading, columns=columns)


def _drop_repeated_headings(rows: list[list[str]]) -> list[list[str]]:
    """Removes the column headings a table reprints at the top of each
    page it continues onto.

    On the page they are a courtesy to a reader who has turned over. In
    one continuous table they are neither a row of the table nor
    information -- the Crimes Act's s 321P table came out with "Column 1 |
    Column 2" appearing four times down the middle of its own data.

    A repeat is recognised by being an exact copy of the table's own
    first row, and takes with it however many following rows also copy
    what followed the first -- a table headed both "Column 1 / Column 2"
    and "Item No. / Offence charged" reprints both."""
    if not rows:
        return rows
    kept = [rows[0]]
    index = 1
    while index < len(rows):
        if rows[index] != rows[0]:
            kept.append(rows[index])
            index += 1
            continue
        repeat = 1
        while (
            repeat < len(kept)
            and index + repeat < len(rows)
            and rows[index + repeat] == rows[repeat]
        ):
            repeat += 1
        index += repeat
    return kept


