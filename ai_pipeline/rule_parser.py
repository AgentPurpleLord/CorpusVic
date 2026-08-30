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


_GROUP_HEADING_MAX_WORDS = 8

# A defined term is conventionally bolded where it's introduced, even
# outside a section that reads as a "Definitions" section by heading (see
# definitions.py's own docstring on this same convention) -- "medical
# practitioner means—", "youth justice custodial worker has the same
# meaning...", "sexual penetration—see section 35A;". These are bold,
# body-size, and stand at the start of a fresh clause exactly like a
# genuine topic heading does, so the shape of a definition's own opening
# words is the only thing that tells the two apart.
_DEFLIKE_RE = re.compile(
    r"^[A-Za-z][\w \"',()/-]{0,80}?\s+(?:means?\b|has\b|have\b|includes?\b)|[—-]\s*see\s+section\b",
    re.IGNORECASE,
)
# A defined term can also wrap so that only the term itself sits on the bold
# line and the defining verb starts the very next (non-bold) line, e.g.
# "approved alternative publication Internet site" / "means an Internet
# site approved under ...". _DEFLIKE_RE alone can't see that -- the verb
# isn't on this line -- so a heading candidate is also rejected when the
# *next* line opens with one.
_DEF_CONTINUATION_RE = re.compile(r"^(?:\([^)]*\)\s*)?(?:means?\b|has\b|have\b|includes?\b)", re.IGNORECASE)
_TERMINAL_PUNCT_RE = re.compile(r"[.;:!?]\s*$")


def _is_fresh_start(prev_text: str, prev_bold: bool) -> bool:
    """Is the line that follows starting clean, rather than continuing a
    sentence in progress? True at the very start of the document, right
    after body text that reached a terminal full stop/semicolon/etc., or
    right after a bold heading line (Part/Division/Subdivision/Section
    titles are always bold and never themselves end in that punctuation --
    "Division 1—Offences against the person" has nothing to terminate).
    Used to gate several "is this really a heading, or just continuing
    whatever came before" decisions below: a numbered Subdivision, a bare
    Section-number wrap, and a bare topical heading_group all only ever
    legitimately start right after one of these two things."""
    return not prev_text or prev_bold or bool(_TERMINAL_PUNCT_RE.search(prev_text))


def _looks_like_group_heading(text: str, prev_text: str, prev_bold: bool, next_text: str) -> bool:
    """A bare topical heading grouping a run of sections ("Theft, robbery,
    burglary, &c.", "Fraud and blackmail", "Fingerprinting") can be set at
    ordinary body size, distinguished from an inline bold emphasis (a
    defined term, an Act-name citation tail wrapped from the previous
    line, a small-print endnotes/amendment-history table row) only by
    shape and position: short, not itself the start (or, wrapped, the lead-
    in) of a "term means ..." clause, and sitting right after the previous
    node reached a clean sentence break rather than continuing it."""
    if not text[:1].isupper():
        # A genuine heading always opens with a capitalised word; a lower-
        # case start means this bold line is actually the tail of a
        # multi-line bolded defined term wrapping across several lines
        # (only the first of which reads as a definition opener), not a
        # heading in its own right.
        return False
    if len(text.split()) > _GROUP_HEADING_MAX_WORDS:
        return False
    if text.rstrip().endswith(("—", "–")):
        # A term introducing a lettered list of alternative meanings
        # instead of "means"/"has"/"includes" ("ASIC Regulations—" then
        # "(a) when used in relation to ...") -- still a defined term, not
        # a heading; a genuine topical heading never trails off like this.
        return False
    if _DEFLIKE_RE.search(text) or _DEF_CONTINUATION_RE.match(next_text):
        return False
    return _is_fresh_start(prev_text, prev_bold)


# Legislative sentences that happen to open a subsection commonly start with
# one of these -- "In this section, X means ...", "If under the ... Act the
# Secretary ...", "Section 3 of the Crimes (...) Act ...". A genuine
# Subdivision title never does; it's a noun-phrase caption ("Homicide",
# "Child stealing", "Abrogation of obsolete rules of law"), never the start
# of a flowing sentence -- so this is the discriminator for the rare case
# where a numbered subsection's first line prints entirely bold (usually
# because a defined term sits early in it) and would otherwise be
# misidentified as a "(N) Title"-shaped Subdivision.
_SENTENCE_OPENER_WORDS = {
    "in", "if", "section", "sections", "subsection", "where", "for", "when",
    "unless", "notwithstanding", "despite", "subject", "this", "that", "a",
    "an", "the", "any", "every", "no", "nothing", "whosoever", "whoever",
}


def _looks_like_subdivision_title(heading: str) -> bool:
    first_word = re.match(r"[A-Za-z]+", heading)
    if first_word and first_word.group(0).lower() in _SENTENCE_OPENER_WORDS:
        return False
    return not _DEFLIKE_RE.search(heading)


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


_INDENT_TOLERANCE = 3.0
_HANGING_LIST_FLOOR = HIERARCHY_ORDER.index("subsection")


def _resolve_hanging_list(stack: list[dict], stack_x0: list[float], x0: float, close_top) -> None:
    """A common legislative construct opens a subsection (or section) with
    lead-in text, breaks into a lettered/roman list, and then closes the
    list with independent text that grammatically resumes the *lead-in's*
    sentence, not the list item's -- "(a) does X; or (b) does Y -- is
    guilty of an offence." A purely textual parse has no way to see that;
    but the PDF's own hanging indent does: each level's own wrapped
    continuation lines print at a fixed indent past that level's opening
    marker, so a plain continuation line that outdents back past the
    innermost list item's own indent is resuming whatever shallower level
    actually sits at that indent, not continuing the list item. Only ever
    pops subsection/paragraph/subparagraph -- Part/Division/Subdivision/
    Section only ever close via an explicit pattern match."""
    while (
        len(stack) > 1
        and HIERARCHY_ORDER.index(stack[-1]["type"]) >= _HANGING_LIST_FLOOR
        and x0 < stack_x0[-1] - _INDENT_TOLERANCE
    ):
        close_top()


def parse_act(pages: list[PageText], profile_name: str | None = None) -> ParseResult:
    patterns = load_profile(profile_name)
    lines = _flatten_lines(pages)
    body_size = _body_font_size(lines)

    nodes: list[dict] = []
    stack: list[dict] = []
    stack_x0: list[float] = []
    warnings: list[str] = []
    current_note: dict | None = None
    notes_mode = False
    asterisk_run: list[BodyLine] = []
    cursor = 0
    lines_consumed = 0
    prev_line_text = ""
    prev_line_bold = False

    def close_top():
        node = stack.pop()
        stack_x0.pop()
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
        stack_x0.append(line.x0)
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

    for idx, line in enumerate(lines):
        text = line.text.strip()
        char_start = cursor
        char_end = cursor + len(text)
        cursor = char_end + 1  # account for the "\n" join
        lines_consumed += 1
        if not text:
            continue
        next_text = lines[idx + 1].text.strip() if idx + 1 < len(lines) else ""

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
            for level in ("part", "division", "section"):
                m = patterns[level].match(text)
                if m:
                    number, heading = m.group(1), m.group(2).strip()
                    open_node(level, number, heading, line, char_start)
                    matched = True
                    break

            # A Subdivision heading is a bracketed number plus a short bold
            # title ("(1) Homicide", "(8G) Abrogation of obsolete rules of
            # law") -- or the number is spelled out ("Subdivision 2—Title").
            # The bracket form overlaps in shape with a bracketed
            # paragraph/subparagraph marker ("(a)", "(i)"), which is also
            # commonly bolded when it's an Act-name citation inside an
            # enumerated list ("(i) the Conservation, Forests and Lands
            # Act 1987; or") -- but a Subdivision's own number is always
            # digit-led in this drafting convention (paragraphs are letters,
            # subparagraphs are lowercase roman numerals, never digits), so
            # that's the one discriminator size/position doesn't need to
            # gate on: a lettered/roman bracket here isn't a Subdivision
            # candidate at all, and falls through to bracket-item
            # classification below same as it always did.
            #
            # A digit-led bracket can still be a false positive of a
            # different kind, though: a bold mid-sentence pinpoint citation
            # that itself starts a new physical line, e.g. "under section
            # 9A(1A) or\n(1B) of the Corrections Act 1986 to exercise ..."
            # -- "(1B)" here is a continuation of "or", not a fresh heading.
            # A genuine Subdivision only ever opens right after whatever
            # came before it reached a clean sentence break, same signal
            # used for the unnumbered heading_group case below.
            if not matched:
                m = patterns["subdivision"].match(text)
                if m:
                    if m.group(1) is not None:
                        heading = m.group(2).strip()
                        if (
                            re.match(r"^\d", m.group(1))
                            and _is_fresh_start(prev_line_text, prev_line_bold)
                            and _looks_like_subdivision_title(heading)
                        ):
                            open_node("subdivision", m.group(1), heading, line, char_start)
                            matched = True
                    else:
                        open_node("subdivision", m.group(3), m.group(4).strip(), line, char_start)
                        matched = True

            # A bare section number with nothing else on the line -- the
            # heading wraps onto the next bold line instead (same wrap
            # pattern as the bare Subdivision case above), e.g. "465AAAA"
            # alone followed by "Police may use assistants and equipment"
            # as a separate bold line.
            if not matched and re.match(r"^\d+[A-Za-z]*$", text) and _is_fresh_start(prev_line_text, prev_line_bold):
                open_node("section", text, None, line, char_start)
                matched = True

        # Bracket items (subsection/paragraph/subparagraph) are classified by
        # content shape + sequence context, not boldness -- Act-name
        # citations are commonly bolded throughout these Acts.
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
                if (
                    level == "subsection"
                    and line.bold
                    and remainder is None
                    and _is_fresh_start(prev_line_text, prev_line_bold)
                ):
                    # A bare bold "(N)" with nothing else on the line -- the
                    # Subdivision's title wraps onto the next bold line
                    # instead (mirrors the heading-wrap handling below), e.g.
                    # "(4A)" / "Non-fatal strangulation" as two lines. A
                    # genuine subsection number is never bold on its own,
                    # and a genuine Subdivision only opens right after a
                    # clean sentence break -- guards against a bold bracket
                    # that's actually a pinpoint citation ("section 9A(1A)
                    # or\n(1B) of ...") wrapping mid-sentence instead.
                    level = "subdivision"
                open_node(level, bracket_match.group(1), None, line, char_start)
                if remainder:
                    append_text(stack[-1], remainder, line, char_end)
                matched = True

        if not matched and line.bold:
            top = stack[-1] if stack else None
            if top is not None and top["type"] in HEADING_LEVELS and not top["text"]:
                # Heading text that wrapped onto another bold line, e.g.
                # "3A Unintentional killing in the course or furtherance\nof
                # a crime of violence" -- extend the heading, not the body.
                append_heading(top, text, char_end)
            elif round(line.size, 1) > body_size or (
                round(line.size, 1) == body_size and _looks_like_group_heading(text, prev_line_text, prev_line_bold, next_text)
            ):
                # A bare topical heading grouping a run of sections. Usually
                # bold and visibly larger than body text (e.g. "Fraud and
                # blackmail"), but some Acts set these at plain body size
                # ("Theft, robbery, burglary, &c."), distinguishable from
                # inline bold emphasis only by shape -- see
                # _looks_like_group_heading. It always sits between two
                # sections, never inside one, but tree reconstruction
                # (build_hierarchy_tree in akn_export.py, shared by both
                # exporters) replays node *types* from this flat list
                # independently of this function's own stack -- so that's
                # where attachment gets corrected, not here.
                nodes.append({
                    "type": "heading_group", "number": None, "heading": text, "text": text,
                    "page_start": line.page_no, "page_end": line.page_no,
                    "char_start": char_start, "char_end": char_end, "source": "rules",
                })
            else:
                # Bold at body size with no structural pattern -- inline
                # emphasis (e.g. a defined term or, as above, an Act-name
                # citation), not a boundary.
                if not stack:
                    open_node("part", None, "Preliminary", line, char_start)
                append_text(stack[-1], text, line, char_end)
            matched = True

        if not matched:
            # Continuation of whatever is currently open. If nothing is open
            # yet (preamble text before the first Part), open a synthetic
            # holder rather than dropping it.
            if not stack:
                open_node("part", None, "Preliminary", line, char_start)
                warnings.append(f"page {line.page_no}: text before any recognised Part -- filed under a synthetic preamble node")
            else:
                _resolve_hanging_list(stack, stack_x0, line.x0, close_top)
            append_text(stack[-1], text, line, char_end)

        prev_line_text = text
        prev_line_bold = line.bold

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
