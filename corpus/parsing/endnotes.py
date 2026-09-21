"""
Parses the Endnotes that close every authorised Victorian Act -- the
General information block, the Table of Amendments, and the
Explanatory details -- into actual structure rather than a wall of
text.

Why this needs its own parser, rather than reusing the Act's own
(rule_parser.py):

  * The Table of Amendments is a *two-column table*, and PyMuPDF reads
    the two columns in the wrong order -- every value comes out before
    its own label ("10.3.09" then "Assent Date:"). Read as flowing
    text, it isn't just unstructured, it's actually backwards. The fix
    comes from geometry: labels sit in one column of x-positions,
    values in another, and a label shares a visual row with the first
    line of its value, so sorting each row left-to-right puts them back
    in the right order. Nothing else in this pipeline needs this kind
    of column reconstruction, which is why it lives here and not in
    extract.py.

  * The endnote section headings ("1 General information", "2 Table of
    Amendments") are shaped exactly like an Act's own section headings,
    so the body parser would read them as sections 1 and 2 -- clashing
    with the Act's real sections 1 and 2, and swallowing every
    remaining page into one node. run_pipeline.py now stops the body
    parse at detect_endnotes_start and hands these pages here instead.

What this is actually *for*: the Act's own margin notes name amending
Acts by number only ("amended by No. 68/2009 s. 51(b)(i)"). The Table
of Amendments is what turns those numbers into a title, an assent date
and a commencement date -- see corpus/amendments.py, which joins
the two together.

This works on a best-effort basis, same as everywhere else in this
pipeline: a line that can't be classified is kept as its section's own
text rather than dropped, and lines_consumed/lines_total lets the
caller check that nothing went missing.
"""
import re
from dataclasses import dataclass, field

from corpus.parsing.extract import BodyLine, PageText

# "Endnotes" is set as its own title, the same size as a Schedule's, on
# the first endnote page and nowhere else -- a far more reliable marker
# than the running header (which extract.py strips) or the section
# headings below it (which look exactly like the Act's own).
_ENDNOTES_TITLE_MIN_SIZE = 14.0

# "1 General information", "2 Table of Amendments", "3 Explanatory details".
_SECTION_HEADING_RE = re.compile(r"^(\d+)\s+(\S.*)$")

# "Assent Date:", "Commencement Date:", "Current State:", "Note:", and
# the "Date of Making:"/"Date of Commencement:" pair a subordinate
# instrument gets instead. Matched by shape rather than a fixed list of
# known labels, so an unfamiliar label is captured properly instead of
# being silently swallowed into the value above it.
_FIELD_LABEL_RE = re.compile(r"^([A-Z][A-Za-z'/() ]{1,40}):$")

# The record's own citation: "No. 7/2009", the five-digit "No.
# 10214/1985" older Acts use, or a statutory rule's "S.R. No.
# 144/1965".
_CITATION_RE = re.compile(r"\b(S\.R\.\s*)?No\.\s*(\d+)\s*/\s*(\d{4})\b")

# A trailing "(as amended by No. 68/2009)" qualifies the record, and its
# citation belongs to a *different* Act -- strip it before reading the
# record's own number. Only a trailing one: plenty of real short titles
# carry a parenthetical of their own ("Interpretation of Legislation
# (Further Amendment) Act 1985").
_TRAILING_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")

# The record's title line ends with the citation the line above already
# captured ("Bus Safety Act 2009, No. 13/2009") -- trimmed off `title`
# so it reads as the Act's own short title, which is how it's shown and
# how it matches corpus/act_registry.json's own keys.
_TRAILING_CITATION_RE = re.compile(r",?\s*(?:S\.R\.\s*)?No\.\s*\d+\s*/\s*\d{4}\s*$")

_DIVIDER_RE = re.compile(r"^[–—\-]{5,}$")

# Two lines belong to the same printed row if their tops are within
# this many points -- a label and the first line of its value sit on
# one baseline, but can differ by a fraction of a point.
_ROW_TOLERANCE = 3.0

_COLUMN_TOLERANCE = 6.0

# A bullet in the General information block is typeset as its own
# "line" holding nothing but the marker, with the item's text beside it
# at a deeper indent.
_BULLET_RE = re.compile(r"^[\u2022\u00b7\u2023\u25e6\-]$")

# Where one block of prose ends and the next begins, expressed as a
# multiple of the line's own type size. A wrapped line sits about 1.15
# sizes below the one above it; a new paragraph, heading or bullet gets
# extra space, about 1.65 and up. This is relative to the size rather
# than one fixed number for the whole document, because these pages mix
# three different sizes -- 9pt Table of Amendments rows, 10pt endnote
# prose, and 12pt provisions quoted under Explanatory details -- and
# each wraps with its own spacing.
_BLOCK_GAP_FACTOR = 1.4

# A heading is a short standalone line. "Legislative Assembly: 4 December
# 2008" is not one (a colon with a value after it is a labelled fact),
# but "Constitution Act 1975:" and "Style changes" are.
_HEADING_MAX_CHARS = 70


@dataclass
class EndnotesParseResult:
    sections: list[dict] = field(default_factory=list)
    amending_acts: list[dict] = field(default_factory=list)
    lines_total: int = 0
    lines_consumed: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"sections": self.sections, "amending_acts": self.amending_acts}


def detect_endnotes_start(pages: list[PageText]) -> int | None:
    """The page number (counting from 1) where the Endnotes begin, or
    None if this document has none (a Bill, an Explanatory Memorandum,
    or an extract of an Act that stops before them)."""
    for page in pages:
        for line in page.body_lines:
            if line.text.strip().lower() == "endnotes" and line.bold and line.size >= _ENDNOTES_TITLE_MIN_SIZE:
                return page.page_no
    return None


def _visual_rows(lines: list[BodyLine]) -> list[BodyLine]:
    """The page's lines in true reading order: grouped into printed rows
    by their vertical position, then left-to-right within each row. This
    is the whole fix for the Table of Amendments -- PyMuPDF hands back
    the value column before the label column, so read as-is, every
    field's value comes before the label it belongs to."""
    ordered = sorted(lines, key=lambda l: (l.y0, l.x0))
    out: list[BodyLine] = []
    row: list[BodyLine] = []
    row_top = None
    for line in ordered:
        if row_top is None or line.y0 - row_top <= _ROW_TOLERANCE:
            row_top = line.y0 if row_top is None else row_top
            row.append(line)
        else:
            out.extend(sorted(row, key=lambda l: l.x0))
            row = [line]
            row_top = line.y0
    out.extend(sorted(row, key=lambda l: l.x0))
    return out


def _mode(values: list[float]) -> float | None:
    if not values:
        return None
    counts: dict[float, int] = {}
    for v in values:
        counts[round(v)] = counts.get(round(v), 0) + 1
    return float(max(counts, key=lambda k: (counts[k], -k)))


def _columns(lines: list[BodyLine]) -> dict | None:
    """Where the table's columns actually sit in this document, measured
    from the label lines rather than hard-coded. Returns None when no
    labels are found at all -- an Act with no Table of Amendments, or a
    layout this doesn't recognise -- in which case the caller keeps
    everything as section text instead of inventing records."""
    label_lines = [l for l in lines if _FIELD_LABEL_RE.match(l.text.strip())]
    label_x = _mode([l.x0 for l in label_lines])
    if label_x is None:
        return None
    record_size = _mode([l.size for l in label_lines]) or 9.0
    # The record-title column: whatever sits left of the labels, at the
    # same type size the records themselves are set in.
    title_x = _mode([l.x0 for l in lines if l.x0 < label_x - _COLUMN_TOLERANCE and abs(l.size - record_size) < 0.6])
    return {
        "label_x": label_x,
        "title_x": title_x if title_x is not None else label_x - 25,
        "record_size": record_size,
    }


def _starts_new_block(line: BodyLine, previous: "BodyLine | None") -> bool:
    """Whether this line begins a new block rather than continuing the
    paragraph above it -- judged from the space between the two, scaled
    to the type size they're set in (see _BLOCK_GAP_FACTOR)."""
    if previous is None:
        return True
    if abs(line.size - previous.size) > 0.6:
        return True
    if line.page_no != previous.page_no:
        # A paragraph that runs over a page break is still one
        # paragraph. There's no space to measure across the break, so
        # read the sentence instead: an unfinished line continued by a
        # lower-case one is a wrap, anything else starts a new block.
        return not (
            not previous.text.rstrip().endswith((".", ":", ";", "\u2014", '."'))
            and line.text.lstrip()[:1].islower()
        )
    return line.y0 - previous.y0 > max(line.size, previous.size) * _BLOCK_GAP_FACTOR


def _looks_like_heading(text: str) -> bool:
    if len(text) > _HEADING_MAX_CHARS:
        return False
    if text.endswith((":", "\u2014", "\u2013")):
        return True  # introduces what follows: "Constitution Act 1975:"
    if ":" in text:
        return False  # a labelled fact: "Legislative Assembly: 4 December 2008"
    return not text.endswith((".", ";", '."', '.\u201d'))


def _finish_block(block: dict | None, out: list[dict]) -> None:
    """Joins a block's wrapped lines back into one string. This is where
    the source PDF's own line-wrap points stop being part of the text --
    they're just a layout artefact, and keeping them made the endnotes
    render as a ragged column of half-sentences."""
    if block is None:
        return
    text = " ".join(block.pop("parts")).strip()
    text = re.sub(r"\s{2,}", " ", text)
    if not text:
        return
    if block["kind"] == "paragraph" and _looks_like_heading(text):
        block["kind"] = "heading"
    out.append({**block, "text": text})


def _normalise_field(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def parse_citation(title: str) -> dict:
    """The record's own citation, read off its title line. "Bus Safety
    Act 2009, No. 13/2009 (as amended by No. 68/2009)" is Act 13 of
    2009 -- the number in the trailing qualifier belongs to a different
    Act."""
    stripped = _TRAILING_QUALIFIER_RE.sub("", title)
    matches = list(_CITATION_RE.finditer(stripped))
    if not matches:
        return {"citation": None, "act_no": None, "year": None, "is_statutory_rule": False}
    m = matches[-1]
    return {
        "citation": f"{m.group(2)}/{m.group(3)}",
        "act_no": m.group(2),
        "year": m.group(3),
        "is_statutory_rule": bool(m.group(1)),
    }


def _finish_record(record: dict | None, out: list[dict], warnings: list[str]) -> None:
    if record is None:
        return
    title = " ".join(record["title_parts"]).strip()
    if not title:
        return
    fields = {k: " ".join(v).strip() for k, v in record["fields"].items()}
    entry = {
        "title": _TRAILING_CITATION_RE.sub("", _TRAILING_QUALIFIER_RE.sub("", title)).rstrip(", ").strip(),
        "raw_title": title,
        **parse_citation(title),
        "fields": fields,
        "page_start": record["page_start"],
        "page_end": record["page_end"],
    }
    if entry["citation"] is None:
        warnings.append(f"page {entry['page_start']}: no Act number found in Table of Amendments entry {title!r}")
    out.append(entry)


def parse_endnotes(pages: list[PageText]) -> EndnotesParseResult:
    """`pages` are the endnote pages only -- everything from
    detect_endnotes_start onwards (run_pipeline.py does the split)."""
    result = EndnotesParseResult()
    lines: list[BodyLine] = []
    for page in pages:
        lines.extend(_visual_rows(page.body_lines))
    result.lines_total = len(lines)
    columns = _columns(lines)
    # The endnote section headings are the leftmost thing on any of
    # these pages -- further left than even the Table of Amendments'
    # own record titles. Measured rather than taken from the column
    # geometry, so the sections are still found in a document with no
    # Table of Amendments for _columns to measure.
    section_x = min((l.x0 for l in lines), default=0.0)
    # The leftmost body column -- where the endnotes' own prose starts.
    # Anything further right is indented for a reason (a bullet's text,
    # a provision quoted inside Explanatory details, the Table of
    # Amendments' own label/value columns).
    body_x = min((l.x0 for l in lines if l.x0 > section_x + _COLUMN_TOLERANCE), default=section_x)
    # The size the endnotes' own prose is set in. A provision quoted
    # under Explanatory details is reproduced at the Act's own larger
    # size, which is what tells it apart from a centred banner that
    # merely indents. Table-of-Amendments rows share the body column
    # but are set even smaller, and are handled by the record machinery
    # -- excluded here, or they'd outnumber the prose and get mistaken
    # for "the prose size" themselves.
    prose_size = _mode([
        l.size for l in lines
        if abs(l.x0 - body_x) <= _COLUMN_TOLERANCE
        and (columns is None or abs(l.size - columns["record_size"]) > 0.6)
    ]) or 10.0
    if columns is None:
        result.warnings.append("no Table of Amendments columns found -- endnote text kept unstructured")

    section: dict | None = None
    record: dict | None = None
    current_field: str | None = None
    block: dict | None = None
    previous: BodyLine | None = None

    def close_block() -> None:
        nonlocal block
        if section is not None:
            _finish_block(block, section["blocks"])
        block = None

    def close_section() -> None:
        nonlocal section, record, current_field
        _finish_record(record, result.amending_acts, result.warnings)
        record = None
        current_field = None
        close_block()
        if section is not None:
            # "text" is kept alongside the blocks: it's what every
            # reader of this data expected before blocks existed, and
            # it's now reflowed rather than carrying the PDF's own wrap
            # points.
            section["text"] = "\n".join(b["text"] for b in section["blocks"]).strip()
            result.sections.append(section)
        section = None

    for line in lines:
        text = line.text.strip()
        if not text:
            result.lines_consumed += 1
            continue
        starts_block = _starts_new_block(line, previous)
        previous = line

        heading = _SECTION_HEADING_RE.match(text)
        is_section_heading = (
            heading is not None and line.bold and line.x0 <= section_x + _COLUMN_TOLERANCE
        )
        if is_section_heading:
            close_section()
            section = {
                "number": heading.group(1),
                "heading": heading.group(2),
                "blocks": [],
                "page_start": line.page_no,
                "page_end": line.page_no,
            }
            block = None
            result.lines_consumed += 1
            continue

        if section is None:
            # The "Endnotes" title itself, and anything before the
            # first numbered section.
            result.lines_consumed += 1
            continue
        section["page_end"] = line.page_no

        if columns is not None and not _DIVIDER_RE.match(text):
            label = _FIELD_LABEL_RE.match(text)
            if label is not None and abs(line.x0 - columns["label_x"]) <= _COLUMN_TOLERANCE:
                if record is not None:
                    current_field = _normalise_field(label.group(1))
                    record["fields"].setdefault(current_field, [])
                    record["page_end"] = line.page_no
                    result.lines_consumed += 1
                    continue
            elif line.x0 > columns["label_x"] + _COLUMN_TOLERANCE:
                if record is not None and current_field is not None:
                    record["fields"][current_field].append(text)
                    record["page_end"] = line.page_no
                    result.lines_consumed += 1
                    continue
            elif (
                line.x0 <= columns["label_x"] - _COLUMN_TOLERANCE
                and abs(line.size - columns["record_size"]) < 0.6
            ):
                # A record title -- and the first one after any field
                # has been read closes off the record before it, which
                # is what separates one entry from the next (a title
                # can itself wrap onto a second line).
                if record is None or current_field is not None:
                    _finish_record(record, result.amending_acts, result.warnings)
                    record = {"title_parts": [], "fields": {}, "page_start": line.page_no, "page_end": line.page_no}
                    current_field = None
                record["title_parts"].append(text)
                record["page_end"] = line.page_no
                result.lines_consumed += 1
                continue

        # Anything else is this section's own prose -- the General
        # information block, the Table of Amendments' own introduction,
        # the provisions quoted under Explanatory details. Grouped into
        # blocks so it can be rendered the way the printed page reads,
        # rather than as a column of hard-wrapped lines: a run of lines
        # one line-height apart is one paragraph, a wider gap starts the
        # next block.
        _finish_record(record, result.amending_acts, result.warnings)
        record = None
        current_field = None

        if _DIVIDER_RE.match(text):
            close_block()
            result.lines_consumed += 1
            continue

        if _BULLET_RE.match(text):
            close_block()
            block = {"kind": "bullet", "parts": []}
            result.lines_consumed += 1
            continue

        # Deeper indent or a larger type size than the endnote body: a
        # provision quoted inside Explanatory details, which is the
        # Act's own text rather than the endnote's commentary on it. A
        # quotation is indented past the body column *and* set larger.
        # A centred banner ("INTERPRETATION OF LEGISLATION ACT 1984
        # (ILA)") and a bullet's own text also indent, but stay in the
        # prose size.
        quoted = line.x0 > body_x + _COLUMN_TOLERANCE and line.size > prose_size + 0.6
        kind = "quote" if quoted else "paragraph"
        if block is not None and block["kind"] == "bullet" and not block["parts"]:
            # The marker and the item's text are typeset on one row, so
            # whatever follows the marker belongs to it no matter how
            # it indents.
            pass
        elif block is None or starts_block or block["kind"] != kind:
            close_block()
            block = {"kind": kind, "parts": []}
        block["parts"].append(text)
        result.lines_consumed += 1

    close_section()

    if result.lines_consumed != result.lines_total:
        result.warnings.append(
            f"{result.lines_total - result.lines_consumed} endnote line(s) unaccounted for"
        )
    return result
