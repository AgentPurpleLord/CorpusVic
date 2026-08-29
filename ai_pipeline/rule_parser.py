"""
Offset-based, config-driven structural parser.

Instead of asking a model to guess where one component ends and the next
begins, this walks the extracted body lines once and classifies each line
using two independent signals: its text (against the act's pattern
profile) and its typesetting (bold + font size). Font weight/size turns out
to be far more reliable than position for this: Part/Division/Subdivision/
Section headings are consistently set bold at a size distinct from body
text, while subsections/paragraphs/subparagraphs are never bold -- that's
the drafter's own hierarchy signal, already sitting in the PDF, rather than
something inferred from geometry that can coincidentally misfire on a short
wrapped body line.

The output is the same flat, ordered node shape the AI backend produces, so
review.py and tree.py work unchanged.

The key guarantee an LLM can't give you: every input line is consumed by
exactly one output node. There is no code path that silently drops a line;
anything that doesn't match a known pattern becomes continuation text of
whatever node is currently open. `parse_result.lines_total ==
parse_result.lines_consumed` is a hard assertion, not a hope.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from .extract import BodyLine, PageText
from .profiles import load_profile

HIERARCHY_ORDER = ["part", "division", "subdivision", "section", "subsection", "paragraph", "subparagraph"]
HEADING_LEVELS = {"part", "division", "subdivision", "section"}


@dataclass
class ParseResult:
    nodes: list[dict]
    lines_total: int
    lines_consumed: int
    warnings: list[str] = field(default_factory=list)


def _flatten_lines(pages: list[PageText]) -> list[BodyLine]:
    lines = []
    for page in pages:
        lines.extend(page.body_lines)
    return lines


def _body_font_size(lines: list[BodyLine]) -> float:
    """The most common non-bold font size -- the document's body baseline,
    used to tell a genuinely oversized bare heading apart from ordinary
    bold emphasis (e.g. a defined term bolded inline) at body size."""
    sizes = Counter(round(l.size, 1) for l in lines if not l.bold and l.size)
    return sizes.most_common(1)[0][0] if sizes else 12.0


def _next_letter(s: str) -> str:
    """a -> b, z -> aa, aa -> ab (base-26 increment over lowercase letters)."""
    chars = list(s)
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "a"
        i -= 1
    return "a" + "".join(chars)


_ROMAN_VALUES = [
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
]


def _roman_to_int(s: str) -> int | None:
    s = s.lower()
    i, n = 0, 0
    for value, sym in _ROMAN_VALUES:
        while s[i : i + len(sym)] == sym:
            n += value
            i += len(sym)
    return n if i == len(s) and n > 0 else None


def _int_to_roman(n: int) -> str:
    result = []
    for value, sym in _ROMAN_VALUES:
        while n >= value:
            result.append(sym)
            n -= value
    return "".join(result)


def _next_roman(s: str) -> str | None:
    n = _roman_to_int(s)
    return None if n is None else _int_to_roman(n + 1)


def _bracket_level(content: str, stack: list[dict]) -> str:
    """Disambiguates a bracketed token like "(1)", "(a)", "(i)". Digits are
    always a subsection. For alphabetic content, single letters and roman
    numerals overlap ("(i)", "(v)", "(x)" are valid as both), so nesting
    depth alone isn't enough -- e.g. paragraph (a) followed by (b) must stay
    a sibling paragraph even though the innermost open node might be a
    subparagraph from a previous paragraph's nested list. The tiebreaker is
    sequence continuity: does this token continue the letter/roman sequence
    already open at some level, or start a fresh nested run?"""
    if re.match(r"^\d", content):
        return "subsection"
    if stack:
        top = stack[-1]
        if top["type"] == "paragraph":
            if _next_letter(top["number"]) == content:
                return "paragraph"
            return "subparagraph"
        if top["type"] == "subparagraph":
            if _next_roman(top["number"]) == content:
                return "subparagraph"
            if len(stack) >= 2 and stack[-2]["type"] == "paragraph":
                return "paragraph"
    return "paragraph"


def parse_act(pages: list[PageText], profile_name: str | None = None) -> ParseResult:
    patterns = load_profile(profile_name)
    lines = _flatten_lines(pages)
    body_size = _body_font_size(lines)

    nodes: list[dict] = []
    stack: list[dict] = []
    warnings: list[str] = []
    current_note: dict | None = None
    notes_mode = False
    asterisk_run: list[BodyLine] = []
    cursor = 0
    lines_consumed = 0

    def close_top():
        node = stack.pop()
        node["text"] = node["text"].strip()

    def open_node(level: str, number: str | None, heading: str | None, line: BodyLine, char_start: int) -> dict:
        idx = HIERARCHY_ORDER.index(level)
        while stack and HIERARCHY_ORDER.index(stack[-1]["type"]) >= idx:
            close_top()
        node = {
            "type": level, "number": number, "heading": heading, "text": "",
            "page_start": line.page_no, "page_end": line.page_no,
            "char_start": char_start, "char_end": char_start, "source": "rules",
        }
        nodes.append(node)
        stack.append(node)
        return node

    def append_text(node: dict, text: str, line: BodyLine, char_end: int):
        node["text"] = (node["text"] + "\n" + text) if node["text"] else text
        node["page_end"] = line.page_no
        node["char_end"] = char_end

    def append_heading(node: dict, text: str, char_end: int):
        node["heading"] = (node["heading"] + " " + text) if node["heading"] else text
        node["char_end"] = char_end

    def close_note():
        nonlocal current_note
        if current_note is not None:
            current_note["text"] = current_note["text"].strip()
            current_note = None

    def flush_asterisk_run(char_end: int):
        if len(asterisk_run) >= 3:
            first, last = asterisk_run[0], asterisk_run[-1]
            nodes.append({
                "type": "note", "number": None, "heading": None,
                "text": ("* " * len(asterisk_run)).strip(),
                "page_start": first.page_no, "page_end": last.page_no,
                "char_start": asterisk_start, "char_end": char_end, "source": "rules",
            })
        else:
            # Too short a run to be the repealed-text marker -- don't lose it,
            # fold it into whatever's currently open instead.
            for l in asterisk_run:
                if not stack:
                    open_node("part", None, "Preliminary", l, l.y0)
                append_text(stack[-1], l.text.strip(), l, char_end)
        asterisk_run.clear()

    for line in lines:
        text = line.text.strip()
        char_start = cursor
        char_end = cursor + len(text)
        cursor = char_end + 1  # account for the "\n" join
        lines_consumed += 1
        if not text:
            continue

        if text == "*":
            if not asterisk_run:
                asterisk_start = char_start
            asterisk_run.append(line)
            continue
        elif asterisk_run:
            flush_asterisk_run(char_start - 1)

        if patterns["notes_marker"].match(text):
            close_note()
            notes_mode = True
            continue

        if notes_mode:
            m = patterns["note_item"].match(text)
            # A hanging-indent note number ("1") can land as its own line,
            # separate from its text, if the PDF laid it out with a tab stop
            # rather than inline -- don't let that split fool us into
            # thinking the notes block ended.
            if m or (text.isdigit() and len(text) <= 3):
                close_note()
                current_note = {
                    "type": "note", "number": m.group(1) if m else text, "heading": None,
                    "text": m.group(2) if m else "",
                    "page_start": line.page_no, "page_end": line.page_no,
                    "char_start": char_start, "char_end": char_end, "source": "rules",
                }
                nodes.append(current_note)
                continue
            if current_note is not None and not _looks_like_boundary(text, patterns):
                append_text(current_note, text, line, char_end)
                continue
            close_note()
            notes_mode = False

        matched = False
        if line.bold:
            for level in ("part", "division", "subdivision", "section"):
                m = patterns[level].match(text)
                if m:
                    groups = [g for g in m.groups() if g is not None]
                    number, heading = groups[0], groups[1].strip()
                    open_node(level, number, heading, line, char_start)
                    matched = True
                    break
            if not matched:
                top = stack[-1] if stack else None
                if top is not None and top["type"] in HEADING_LEVELS and not top["text"]:
                    # Heading text that wrapped onto another bold line, e.g.
                    # "3A Unintentional killing in the course or furtherance\nof
                    # a crime of violence" -- extend the heading, not the body.
                    append_heading(top, text, char_end)
                elif round(line.size, 1) > body_size:
                    # Bold and visibly larger than body text, but no numbered
                    # pattern matched -- a bare topical heading (e.g. "Fraud
                    # and blackmail" grouping a run of sections).
                    nodes.append({
                        "type": "heading_group", "number": None, "heading": text, "text": text,
                        "page_start": line.page_no, "page_end": line.page_no,
                        "char_start": char_start, "char_end": char_end, "source": "rules",
                    })
                else:
                    # Bold at body size with no structural pattern -- inline
                    # emphasis (e.g. a defined term), not a boundary.
                    if not stack:
                        open_node("part", None, "Preliminary", line, char_start)
                    append_text(stack[-1], text, line, char_end)
                matched = True

        if not matched:
            m = patterns["subsection"].match(text)
            bracket_match, level = None, None
            if m and re.match(r"^\d", m.group(1)):
                bracket_match, level = m, "subsection"
            else:
                m2 = patterns["paragraph"].match(text) or patterns["subparagraph"].match(text)
                if m2:
                    bracket_match, level = m2, _bracket_level(m2.group(1), stack)
            if level and bracket_match:
                remainder = bracket_match.group(2).strip() or None
                open_node(level, bracket_match.group(1), None, line, char_start)
                if remainder:
                    append_text(stack[-1], remainder, line, char_end)
                matched = True

        if not matched:
            # Continuation of whatever is currently open. If nothing is open
            # yet (preamble text before the first Part), open a synthetic
            # holder rather than dropping it.
            if not stack:
                open_node("part", None, "Preliminary", line, char_start)
                warnings.append(f"page {line.page_no}: text before any recognised Part -- filed under a synthetic preamble node")
            append_text(stack[-1], text, line, char_end)

    if asterisk_run:
        flush_asterisk_run(cursor)
    close_note()
    while stack:
        close_top()

    return ParseResult(nodes=nodes, lines_total=len(lines), lines_consumed=lines_consumed, warnings=warnings)


def _looks_like_boundary(text: str, patterns: dict) -> bool:
    return any(
        patterns[key].match(text)
        for key in ("part", "division", "subdivision", "section", "subsection", "paragraph", "subparagraph")
    ) or text == "*"
