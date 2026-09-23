"""
A structural parser driven by position and configuration, not by asking
an AI model to guess.

Instead of asking a model to guess where one part ends and the next
begins, this walks through the extracted body lines once and classifies
each one using two independent signals: its text (checked against the
Act's own pattern profile) and how it's typeset (bold and font size).
Font weight and size turn out to be far more reliable than position for
this: Part, Division, Subdivision and Section headings are consistently
set bold at a size that's distinct from body text, while subsections,
paragraphs and subparagraphs are never bold. That's the drafter's own
signal for the document's structure, already sitting right there in the
PDF, rather than something guessed from page geometry that can
misfire on a short line that just happens to wrap.

The output is a flat, ordered list of nodes (tree.py rebuilds the
hierarchy from it), so review.py and tree.py don't need to change.

The key guarantee an AI model can't give you: every line of input ends
up in exactly one output node. There's no code path that silently drops
a line -- anything that doesn't match a known pattern becomes more text
on whatever node is currently open. `parse_result.lines_total ==
parse_result.lines_consumed` is a hard check, not just a hope.

Structure: `parse_act` builds a `_LineParser` and feeds it the flat list
of lines. `_LineParser.feed` makes one pass; for each line it tries the
classifiers below in order (`_try_bold_heading` -> `_try_schedule_hangs_off`
-> `_try_definition_start` -> `_try_bracket_item` -> `_try_bold_emphasis`),
and any line none of them claims falls through to
`_consume_as_continuation`. Ahead of all of them sit the marker blocks --
Notes, Examples and Penalties -- which are recognised by their own
opening words and then run as a small state machine
(`_handle_marked_block`) until something structural ends them. Each classifier returns True once it's
handled the line. The bookkeeping for the open-node stack
(`_open_node`/`_close_top`/...) is shared state on the instance.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from corpus.domain.definitions import looks_like_definitions_section
from corpus.parsing.extract import BodyLine, PageText, join_printed_line
from corpus.domain.hierarchy import HIERARCHY_ORDER, heading_levels, make_ranks
from corpus.domain.profiles import load_hierarchy, load_profile
from corpus.parsing.tables import find_table


@dataclass
class ParseResult:
    nodes: list[dict]
    lines_total: int
    lines_consumed: int
    warnings: list[str] = field(default_factory=list)
    # The container ordering this parse actually used (the default, or
    # the profile's `hierarchy:` override) -- run_pipeline.py saves this
    # so the exporters can rebuild the tree with the same level order.
    hierarchy: list[str] = field(default_factory=lambda: list(HIERARCHY_ORDER))


def _flatten_lines(pages: list[PageText]) -> list[BodyLine]:
    lines = []
    for page in pages:
        lines.extend(page.body_lines)
    return lines


def _body_font_size(lines: list[BodyLine]) -> float:
    """The most common non-bold font size -- the document's normal body
    size, used to tell a genuinely larger heading apart from ordinary
    bold text at body size (e.g. a defined term bolded inline)."""
    sizes = Counter(round(l.size, 1) for l in lines if not l.bold and l.size)
    return sizes.most_common(1)[0][0] if sizes else 12.0


# Every Victorian Bill, and every modern Act, opens its actual operative
# text with this exact, fixed phrase -- a reliable marker for skipping
# past whatever front matter comes before it (see _skip_front_matter).
# An older Act (drafted before this phrase came into use) closes its
# own, longer-form enacting words with a different phrase instead --
# "BE IT ENACTED by the Queen's Most Excellent Majesty ... follows (that
# is to say):" -- wrapped across several lines, so this only matches its
# fixed ending.
# "therefore": an Act with a Preamble enacts on the strength of it (the
# Family Violence Protection Act 2008).
_ENACTING_WORDS_RE = re.compile(r"^The Parliament of Victoria (?:therefore )?enacts:?\s*$")
_PREAMBLE_RE = re.compile(r"^Preamble$")
_OLD_ENACTING_WORDS_RE = re.compile(r"\(that is to say\):?\s*$", re.IGNORECASE)


def _skip_front_matter(lines: list[BodyLine]) -> list[BodyLine]:
    """Both a Bill's own introduction print and an enacted Act's own
    reprint carry something ahead of the real operative text that isn't
    structure itself, but would otherwise get parsed as some:

    A Bill's introduction print opens with a title page and a multi-page
    Table of Provisions -- a table of contents whose rows repeat every
    real Part or clause heading closely enough (same numbering, similar
    bold and size choices) to fool the heading classifiers below into
    treating the table of contents itself as real structure.

    An Act's own reprint carries a short identity block right at the
    start of its operative text -- "Authorised Version No. 114 /
    Criminal Procedure Act 2009 / Authorised Version incorporating
    amendments as at / 1 July 2026" -- which toc.py's detect_body_start
    doesn't catch, since it isn't a Table-of-Provisions page and doesn't
    look like the front matter that function skips whole pages of. Left
    in, this became two spurious heading_group nodes at the very top of
    every parsed Act's body, making it read as though this pipeline's
    own output *were* an Authorised Version, rather than this pipeline's
    reading of one.

    Both are fixed printing conventions that end at a fixed phrase, so
    both are handled the same way: skip everything up to and including
    whichever enacting phrase is found first, modern or old-style.
    Returns the lines unchanged if neither is found, rather than
    silently discarding the whole document on a layout this doesn't
    recognise.

    A Preamble is the exception: it is printed before the enacting words,
    and is part of the Act. It is kept, from its own heading, with only
    the enacting line itself dropped."""
    for i, line in enumerate(lines):
        text = line.text.strip()
        if _ENACTING_WORDS_RE.match(text) or _OLD_ENACTING_WORDS_RE.search(text):
            # The last "Preamble" before them: a Table of Provisions may
            # list one too.
            start = next((j for j in range(i - 1, -1, -1) if _PREAMBLE_RE.match(lines[j].text.strip())), None)
            return (lines[start:i] if start is not None else []) + lines[i + 1 :]
    return lines


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
# outside a section whose heading reads as "Definitions" (see
# definitions.py's own docstring on this same convention) -- "medical
# practitioner means—", "youth justice custodial worker has the same
# meaning...", "sexual penetration—see section 35A;". These are bold,
# body-size, and sit at the start of a fresh clause exactly like a real
# topic heading does, so the only thing that tells the two apart is the
# shape of the opening words.
# "In this section—", "In this Act—", "In this Division—": the standard
# drafting lead-in that opens a run of defined terms. A Definitions
# section announces itself in its heading (see
# definitions.looks_like_definitions_section), but plenty of sections
# define terms without being called that -- the Criminal Procedure Act's
# section 4 ("Meaning of sexual offence") puts four of them in its
# subsection (6) -- and there the lead-in is the only announcement there
# is.
# Anchored at the start of the line (after any provision number), because
# "In this section" only announces definitions when it opens the sentence.
# Mid-sentence it is doing something else entirely -- "it does not matter
# that the offence is described in this section—" and "Nothing in this
# section—" both end exactly the same way and define nothing.
_DEFINITIONS_LEAD_IN_RE = re.compile(
    r"^(?:\(\S{1,6}\)\s*)?In th(?:is|e)\s+"
    r"(?:Act|section|Division|Part|Subdivision|Chapter|Schedule)\b[^.;]{0,60}[\u2014:]\s*$",
    re.IGNORECASE,
)

_DEFLIKE_RE = re.compile(
    r"^[A-Za-z][\w \"',()/-]{0,80}?\s+(?:means?\b|has\b|have\b|includes?\b)|[—-]\s*see\s+section\b",
    re.IGNORECASE,
)
# A defined term can also wrap so only the term itself sits on the bold
# line, with the defining verb starting the very next (non-bold) line,
# e.g. "approved alternative publication Internet site" / "means an
# Internet site approved under ...". _DEFLIKE_RE alone can't catch that,
# since the verb isn't on this line -- so a heading candidate is also
# rejected when the *next* line opens with one.
_DEF_CONTINUATION_RE = re.compile(r"^(?:\([^)]*\)\s*)?(?:means?\b|has\b|have\b|includes?\b)", re.IGNORECASE)
_TERMINAL_PUNCT_RE = re.compile(r"[.;:!?]\s*$")


def _prev_line_was_heading(stack: list[dict], heading_levels: set[str]) -> bool:
    """True if the line just processed left us still inside an open
    heading's own title -- a Chapter, Part, Division, Subdivision or
    Section node with no body text yet, either because it just opened
    or its title wrapped across more than one bold line -- as opposed
    to inside an ordinary node's body content, even body content that
    happens to be bold throughout. An Act-name citation commonly spans
    several *consecutive* bold lines ("... the Crimes\n(Mental
    Impairment and Unfitness to be\nTried) Act 1997, the Magistrates'
    Court\nAct 1989, ..."); only the first genuine heading line should
    count as a fresh start, so this checks what the previous line
    actually *did* (extend an open heading with no body content yet)
    rather than simply whether it was bold, since every line in a
    wrapped citation is.

    `heading_levels` is this Act's own resolved set (self.heading_levels
    -- see hierarchy.py's heading_levels(), which changes per profile
    once a Chapter level is involved), not the module-level default.
    This is a plain function rather than a method purely so it can be
    called directly from _looks_like_group_heading below without
    needing a _LineParser instance."""
    return bool(stack) and stack[-1]["type"] in heading_levels and not stack[-1]["text"]


# What the freshness check below actually guards against: a bold line
# that reads like a section heading but is really the tail of a
# citation that wrapped onto its own line -- an Act name's year on its
# own line ("... Act\n1997 insert--"), or the rest of a list of section
# numbers ("sections 84F\nand 84G of the Domestic Animals Act 1994").
# Both continue a sentence, and both give themselves away: a year is
# four digits and nothing else, and a continuation runs on in lower
# case where a real heading's title is capitalised ("320A Maximum term
# of imprisonment", "464Y Caution before forensic procedure").
#
# Requiring freshness everywhere else cost real provisions, because the
# check only fires on the line right after body text that didn't reach
# a full stop -- exactly where a section following a wrapped provision
# sits. That lost the Crimes Act's ss 320A, 464Y and 464ZGFC, the
# Criminal Procedure Act's s 7B, and about 20 clauses of the Bill.
def _could_be_a_wrapped_citation(number: str, heading: str) -> bool:
    if re.fullmatch(r"(?:1[6-9]|20)\d{2}", number):
        return True  # a wrapped Act-name year
    return not heading[:1].isupper()


def _is_fresh_start(prev_text: str, prev_line_was_heading: bool) -> bool:
    """Is the line that follows starting clean, rather than continuing a
    sentence already in progress? True at the very start of the
    document, right after body text that reached a full stop, semicolon
    or similar, or right after a Part, Division, Subdivision or Section
    heading line (those never end in that kind of punctuation --
    "Division 1—Offences against the person" has nothing to end).
    Used to gate several "is this really a heading, or just a
    continuation of what came before" decisions below: a numbered
    Subdivision, a bare Section-number wrap, and a bare topical
    heading_group can only ever legitimately start right after one of
    these two things."""
    return not prev_text or prev_line_was_heading or bool(_TERMINAL_PUNCT_RE.search(prev_text))


def _looks_like_group_heading(text: str, prev_text: str, prev_line_was_heading: bool, next_text: str) -> bool:
    """A bare topical heading grouping a run of sections ("Theft, robbery,
    burglary, &c.", "Fraud and blackmail", "Fingerprinting") can be set
    at ordinary body size, told apart from inline bold emphasis (a
    defined term, the tail of an Act-name citation wrapped from the
    previous line, a small-print endnotes or amendment-history table
    row) only by shape and position: short, not itself the start of a
    "term means ..." clause (or the lead-in to one wrapping), and
    sitting right after the previous node reached a clean sentence
    break rather than continuing it."""
    if not text[:1].isupper():
        # A genuine heading always opens with a capitalised word; a
        # lower-case start means this bold line is actually the tail of
        # a multi-line bolded defined term wrapping across several
        # lines (only the first of which reads as a definition opener),
        # not a heading in its own right.
        return False
    if len(text.split()) > _GROUP_HEADING_MAX_WORDS:
        return False
    if text.rstrip().endswith(("—", "–")):
        # A term introducing a lettered list of alternative meanings
        # instead of "means"/"has"/"includes" ("ASIC Regulations—" then
        # "(a) when used in relation to ...") -- this is still a defined
        # term, not a heading; a genuine topical heading never trails
        # off like this.
        return False
    if _DEFLIKE_RE.search(text) or _DEF_CONTINUATION_RE.match(next_text):
        return False
    return _is_fresh_start(prev_text, prev_line_was_heading)


# Legislative sentences that happen to open a subsection commonly start
# with one of these -- "In this section, X means ...", "If under the
# ... Act the Secretary ...", "Section 3 of the Crimes (...) Act ...".
# A genuine Subdivision title never does; it's a short caption
# ("Homicide", "Child stealing", "Abrogation of obsolete rules of
# law"), never the start of a flowing sentence -- so this tells apart
# the rare case where a numbered subsection's first line prints
# entirely bold (usually because a defined term sits early in it),
# which would otherwise be misread as a "(N) Title"-shaped Subdivision.
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


def _split_insertion(token: str, base_re: str) -> "tuple[str, str]":
    """A provision number as (what it was inserted after, the suffix that
    inserted it): "ab" -> ("a", "b"), "ia" -> ("i", "a"), "b" -> ("b", "")."""
    m = re.match(base_re, token)
    return (m.group(0), token[m.end():]) if m else (token, "")


def _continues_run(prev: str, content: str, base_re: str, next_in_base) -> bool:
    """Is `content` a sibling of `prev` in the same numbered run?

    Straightforwardly, (a) is followed by (b) and (i) by (ii). But an
    amending Act inserts a provision between two existing ones by
    suffixing the one before it -- (a), (ab), (b), or (i), (ia), (ii) --
    and every step of that is still the same run:

      (a)  -> (ab)   a first insertion after (a)
      (ab) -> (ac)   a second insertion after (a)
      (ab) -> (b)    back to the original sequence

    Reading only the plain next token as a sibling is what made the
    Criminal Procedure Act's section 4(1)(b) come out as a subparagraph
    of (ab) -- which then made its own (i), (ii) and (iii) come out as
    paragraphs of it."""
    if content == next_in_base(prev):
        return True
    base, suffix = _split_insertion(prev, base_re)
    if not suffix:
        # An insertion opening after this one: "ab" after "a".
        return content[:-1] == prev and len(content) == len(prev) + 1
    # A further insertion after the same base, or the base's own next.
    return content == base + _next_letter(suffix) or content == next_in_base(base)


def _continues_letters(prev: str, content: str) -> bool:
    return _continues_run(prev, content, r"^[a-z]", _next_letter)


def _continues_romans(prev: str, content: str) -> bool:
    return _continues_run(prev, content, r"^[ivxlcdm]+", lambda t: _next_roman(t) or "")


def _later_in_run(prev: str, content: str, base_re: str, next_in_base, reach: int = 8) -> bool:
    """Is `content` a later sibling of `prev`, some steps on? Only asked
    across a repealed row: (b), "* * * * *", (d) -- the (c) between them
    is the row -- where "d", a roman numeral too, otherwise read as the
    start of a subparagraph list."""
    token = _split_insertion(prev, base_re)[0]
    for _ in range(reach):
        token = next_in_base(token)
        if not token:
            return False
        if _split_insertion(content, base_re)[0] == token:
            return True
    return False


def _bracket_level(content: str, stack: list[dict], after_repeal: bool = False) -> str:
    """Works out what a bracketed token like "(1)", "(a)", "(i)" actually
    is. Digits are always a subsection. For letters, single letters and
    roman numerals overlap ("(i)", "(v)", "(x)" are valid as either), so
    nesting depth alone isn't enough -- e.g. paragraph (a) followed by
    (b) must stay a sibling paragraph even though the innermost open
    node might be a subparagraph from a previous paragraph's own nested
    list. The tiebreaker is whether the sequence continues: does this
    token continue the letter or roman-numeral sequence already open at
    some level, or does it start a fresh nested run?"""
    if re.match(r"^\d", content):
        return "subsection"
    if stack:
        top = stack[-1]
        if top["type"] == "paragraph":
            if _continues_letters(top["number"], content) or (
                    after_repeal and _later_in_run(top["number"], content, r"^[a-z]", _next_letter)):
                return "paragraph"
            return "subparagraph"
        if top["type"] == "subparagraph":
            if _continues_romans(top["number"], content) or (
                    after_repeal and _later_in_run(top["number"], content, r"^[ivxlcdm]+", lambda t: _next_roman(t) or "")):
                return "subparagraph"
            if len(stack) >= 2 and stack[-2]["type"] == "paragraph":
                return "paragraph"
    return "paragraph"


_INDENT_TOLERANCE = 3.0

_SCHEDULE_ITEM_RE = re.compile(r"^(\d+[A-Z]*)\s+(\S.*)$")
_RULE_RE = re.compile(r"^[\u2550\u2500]{3,}$")
# "Consequential amendments", "Amendment of the Bail Act 1977" -- not the
# title of an Amendment Act a transitional Schedule names.
_AMENDING_SCHEDULE_RE = re.compile(r"\bamendments\b|(?:^|—|–|-)\s*(?:consequential\s+)?amendment\s+of\b", re.IGNORECASE)


def _next_item_number(prev: "str | None", number: str) -> bool:
    """Does `number` follow `prev` in a Schedule's list? 1 opens one; 4
    is followed by 5, or by 4A for an item inserted after it; 4A by 4B."""
    if prev is None:
        return number == "1"
    m = re.match(r"(\d+)([A-Z]*)$", prev)
    if not m:
        return False
    base, suffix = int(m.group(1)), m.group(2)
    following = {str(base + 1), f"{base}{_next_letter(suffix.lower()).upper() if suffix else 'A'}"}
    return number in following


# Where a sentence or a list item has finished. The line above a new
# provision ends one of these ways, or is a heading; "*" marks omitted
# text.
_CLEAN_END_RE = re.compile(r"(?:[.;:\u2014\u2013*]|;\s*(?:or|and|and/or))\s*$")


def _right_margins(lines: list[BodyLine]) -> dict[int, float]:
    """The right edge of the text block, by page parity -- odd and even
    pages are printed with their margins mirrored. Read off the lines
    that reach it, so no measure is assumed."""
    out = {}
    for parity in (0, 1):
        edges = sorted(l.x1 for l in lines if l.page_no % 2 == parity)
        if edges:
            out[parity] = edges[int(len(edges) * 0.95)]
    return out


def _wraps(above: "BodyLine | None", line: BodyLine, text: str, margins: dict[int, float], body_size: float) -> bool:
    """Is this line the sentence above carrying on, though it opens like a
    provision ("(3), admissible as if...", "1958 provides for...")?

    Acts are set ragged-right, not justified: a line breaks early only
    where the next word would not fit. So after a line that stopped
    mid-sentence, a bracket that would have fitted on it starts something
    new -- "(b)" after an "or" set on its own line -- and one that would
    not is the sentence wrapping. Measured on two CPA reprints, those two
    cases were every one of the 154 bracketed lines that followed an
    unfinished line.

    A heading above finishes nothing, but bold alone is not a heading: a
    Note, set smaller than the body, prints the Acts it cites in bold, and
    a line that is mostly citation reads as bold."""
    if above is None or _CLEAN_END_RE.search(above.text.strip()):
        return False
    if above.bold and round(above.size, 1) >= body_size:
        return False
    margin = margins.get(above.page_no % 2)
    if margin is None:
        return False
    per_char = (line.x1 - line.x0) / max(len(text), 1)
    return above.x1 + (len(text.split()[0]) + 1) * per_char > margin


# Two printed lines belong in the same box when they follow each other
# down the page. More clear space than this between them and they are two
# runs of the same provision rather than one -- which is what a provision
# whose own text resumes below something that interrupted it looks like.
_RECT_LINE_GAP = 1.8


def add_rect(node: dict, line: BodyLine) -> None:
    """Records where on the page this line printed, as part of the box
    around the node it belongs to.

    A node is built from printed lines that each know exactly where they
    are, and until now all of that was thrown away the moment their text
    was joined -- a node remembered which *pages* it spanned and nothing
    more. There was nothing to draw, so the parse could only ever be
    reviewed as text beside a picture of the page, never on it.

    One rect per contiguous run per page, rather than one per line: what
    a reader wants to see is a box around the provision, and a provision
    is almost always one run. Where it is not -- its lines separated by
    something that interrupted it -- it gets a box for each run, which is
    also the shape a reviewer needs to mark up a continuation that
    resumes in more than one place."""
    rects = node.setdefault("rects", [])
    if rects:
        last = rects[-1]
        if last["page"] == line.page_no and line.y0 - last["y1"] <= (line.y1 - line.y0) * _RECT_LINE_GAP:
            last["x0"] = round(min(last["x0"], line.x0), 1)
            last["x1"] = round(max(last["x1"], line.x1), 1)
            last["y1"] = round(max(last["y1"], line.y1), 1)
            return
    rects.append({
        "page": line.page_no,
        "x0": round(line.x0, 1), "y0": round(line.y0, 1),
        "x1": round(line.x1, 1), "y1": round(line.y1, 1),
    })


def _append_text(node: dict, text: str, line: BodyLine, char_end: int) -> None:
    """Adds one more printed line to a node's text -- see
    extract.join_printed_line for what happens to the break itself."""
    node["text"] = join_printed_line(node["text"], text)
    node["page_end"] = line.page_no
    node["char_end"] = char_end
    add_rect(node, line)


def _append_heading(node: dict, text: str, char_end: int, line: "BodyLine | None" = None) -> None:
    node["heading"] = (node["heading"] + " " + text) if node["heading"] else text
    node["char_end"] = char_end
    if line is not None:
        add_rect(node, line)


# A Schedule's heading is sometimes immediately followed by a standalone
# line naming which section(s) it "hangs off" -- "Sections 6(3), 159(3)"
# or "Section 5" -- see basic-structure.yaml's own note on this and
# _try_schedule_hangs_off below.
_SCHEDULE_HANGS_OFF_RE = re.compile(r"^Sections?\s+[\d()\s,]+\.?$")

# A Schedule heading with nothing but its number on the line, its title
# set below it -- how a Bill's introduction print sets them ("SCHEDULE
# 1", then the sections it hangs off, then "CHARGES ON A CHARGE-SHEET OR
# INDICTMENT"), as opposed to an Act, which writes "Schedule 1--Charges
# on a charge-sheet" on one line and matches the profile's own
# `schedule` pattern instead. Left undetected, a Bill would have no
# Schedules at all: its Schedule clauses restart at 1 with nothing to
# mark the boundary, so they'd read as a second clause 1, 2, 3 in the
# body, matching the Act's own sections 1, 2, 3. Same "title wraps onto
# the next bold line" shape as the bare Section number case in
# _try_bold_heading, and handled the same way -- but without that
# case's _is_fresh_start check, which a real Schedule heading fails: the
# divider rule and the "SCHEDULES" banner a Bill prints above it leave
# no sentence-ending punctuation behind. That check isn't needed here
# anyway. A bare Section number is just a bare number, which body text
# produces all the time (a citation's year wrapping onto its own line);
# this needs the literal word "Schedule", bold, alone on the line, which
# across every Act, Bill and EM in this repo happens exactly three times
# -- the three real Schedule headings of the one Bill that sets them
# this way, and nothing else.
_BARE_SCHEDULE_RE = re.compile(r"^Schedule\s+(\d+[A-Za-z]*)$", re.IGNORECASE)


# The levels _try_bracket_item recognises by shape alone, with no font
# information -- the boundaries that open without needing to be bold.
_BRACKETED_LEVELS = ("subsection", "paragraph", "subparagraph", "sub_subparagraph")


def _looks_like_boundary(text: str, patterns: dict) -> bool:
    return any(
        compiled.match(text)
        for key, compiled in patterns.items()
        if key not in ("notes_marker", "note_item", "example_marker")
    ) or text == "*" or bool(_BARE_SCHEDULE_RE.match(text))


class _LineParser:
    """One pass over the flat list of body lines. See the module
    docstring for the order the classifiers run in; each `_try_*` or
    `_handle_*` method returns True once it's consumed the current
    line."""

    def __init__(self, patterns: dict, body_size: float, hierarchy_order: list[str], top_level_type: str = "section"):
        self.patterns = patterns
        self.body_size = body_size
        # The node type given to the top-level numbered provision this
        # drafting convention matches with a "section" pattern -- plain
        # "section" for an Act, "clause" for a Bill (see hierarchy.py's
        # HIERARCHY_RANK entry for "clause" and schema.py's NODE_TYPES).
        # The pattern lookup key is always "section" either way -- a
        # profile's regex for this level doesn't change between the
        # two, only what the resulting node ends up called.
        self.top_level_type = top_level_type

        self.order = list(hierarchy_order)
        self.rank = make_ranks(self.order)
        self.heading_levels = heading_levels(self.order)
        self._hanging_list_floor = self.rank["subsection"]
        # The heading levels that use the "Word N—Title" shape (Chapter,
        # Part, Division, ...) plus "section" itself, tried in hierarchy
        # order in _try_bold_heading. "subdivision" is excluded -- it
        # has its own two-form handling right after.
        self._prefix_heading_levels = [
            lvl for lvl in self.order
            if self.rank[lvl] <= self.rank["section"] and lvl != "subdivision" and lvl in patterns
        ]
        # Synthetic bucket for text before the first real container.
        # "part" for every real Victorian Act; the top level otherwise
        # (a profile could conceivably drop "part").
        self._preamble_level = "part" if "part" in self.rank else self.order[0]

        self.nodes: list[dict] = []
        self.stack: list[dict] = []
        # (schedule node, text, char_end) held by _try_schedule_hangs_off
        # until its heading is complete -- see _flush_hangs_off.
        self._pending_hangs_off: "tuple[dict, str, int] | None" = None
        self.stack_x0: list[float] = []
        # The depth each open node was opened at, parallel to `stack`.
        self.stack_rank: list[float] = []
        self.warnings: list[str] = []

        # Which kind of bold-marker block ("Notes"/singular "Note", or
        # "Example") is currently open, if any -- see
        # _handle_marked_block. Both share the exact same state machine,
        # just under a different marker word and resulting node type.
        self.current_marked_block: dict | None = None
        self.marked_block_type: str | None = None
        self.asterisk_run: list[BodyLine] = []
        self.asterisk_start = 0

        self.cursor = 0
        self.lines_total = 0
        self.lines_consumed = 0
        self.prev_text = ""
        # The printed line before the one being read, whatever handled it
        # -- prev_text is left stale by the marker and table paths -- and
        # the right margin it is measured against (see _wraps).
        self.line_above: "BodyLine | None" = None
        # Where clause and section headings start, and the last number a
        # Schedule's list reached (see _try_schedule_item).
        self.heading_x0: "float | None" = None
        self._term_line: "BodyLine | None" = None   # the line a defined term was last read from
        self.schedule_last_number: "str | None" = None
        # A repealed row since the last list item opened, so the next item
        # may skip the ones repealed (see _later_in_run).
        self.repealed_since_item = False
        self.margins: dict[int, float] = {}
        # A bare topical heading_group (e.g. a Bill's "CHAPTER 7--..."
        # caption) isn't pushed onto self.stack the way a Part, Division
        # or Section is -- it's appended straight to self.nodes instead
        # (see _try_bold_emphasis) -- so _prev_line_was_heading's own
        # stack check can't see it. Tracked separately here so a fresh
        # section or clause heading right after one of these still
        # counts as a clean structural boundary for _is_fresh_start, the
        # same as right after a Part or Division heading.
        self.prev_was_heading_group = False
        # Whether the currently-open Section/Clause looks like a
        # Definitions/Interpretation section (see
        # corpus.definitions.looks_like_definitions_section) --
        # reset every time one opens (_open_node, below), gating
        # _try_definition_start so a bold+italic leading run only ever
        # becomes its own "definition" node inside a section that's
        # actually introducing defined terms, never on some unrelated
        # bold+italic text elsewhere.
        self._in_definitions_section = False
        # Where a definition opened by a lead-in should sit. None means
        # the default: a Definitions section's own terms sit at
        # subsection depth (see hierarchy.make_ranks). A lead-in found
        # *inside* a provision sets this one deeper than that provision,
        # so the terms nest under the subsection that introduces them
        # instead of closing it and becoming its siblings.
        self._definition_rank = None

    # -- stack bookkeeping ---------------------------------------------------

    def _close_top(self) -> None:
        self._flush_hangs_off()
        node = self.stack.pop()
        self.stack_x0.pop()
        self.stack_rank.pop()
        node["text"] = node["text"].strip()

    def _flush_hangs_off(self) -> None:
        """Puts a Schedule's held-back "hangs off" note at the end of its
        heading, once that heading is fully in. Only the bare "SCHEDULE
        1" form needs holding back: there the note is printed between
        the number and the title, so appending it as it arrives would
        put it in front of the title it should follow -- and the title
        itself can wrap over more than one bold line, so it isn't
        finished until something else opens or the Schedule closes."""
        if self._pending_hangs_off is None:
            return
        node, text, char_end, line = self._pending_hangs_off
        self._pending_hangs_off = None
        _append_heading(node, text, char_end, line)

    def _open_node(self, level: str, number: str | None, heading: str | None, line: BodyLine,
                   char_start: int, rank: "int | None" = None) -> dict:
        self._flush_hangs_off()
        self.repealed_since_item = False
        if rank is None:
            rank = self.rank[level]
        # Compared against the depth each open node was *opened* at, not
        # its type's own: a definition's depth depends on what introduced
        # it (see _definition_rank), so two definitions opened inside the
        # same subsection still close each other, while neither closes
        # that subsection.
        while self.stack and self.stack_rank[-1] >= rank:
            self._close_top()
        if level in (self.top_level_type, "clause", "item"):
            # A new Section starts a clean slate: whether it defines
            # terms is its own business, and any lead-in depth from the
            # previous one goes with it.
            self._in_definitions_section = looks_like_definitions_section(heading)
            self._definition_rank = None
        if level in ("schedule", "dictionary"):
            self.schedule_last_number = None
        if level == "part" and any(n["type"] == "dictionary" for n in self.stack):
            # A Dictionary's "Part 1—Definitions" is a list of defined
            # terms with no section to announce it; its other Parts are
            # clauses.
            self._in_definitions_section = looks_like_definitions_section(heading)
            self._definition_rank = None
        elif level in ("clause", "item") and self._in_schedule():
            self.schedule_last_number = number
        if level in (self.top_level_type, "clause", "item") and line.bold and heading:
            self.heading_x0 = line.x0   # a heading's, not a list item's
        node = {
            "type": level, "number": number, "heading": heading, "text": "",
            "page_start": line.page_no, "page_end": line.page_no,
            "char_start": char_start, "char_end": char_start, "source": "rules",
        }
        add_rect(node, line)
        if rank != self.rank[level]:
            # This node sits somewhere its type alone doesn't say -- a
            # definition introduced inside a subsection rather than
            # directly under a Definitions section. Recorded on the node
            # so that everything downstream nests it where the parser
            # decided, not where make_ranks would put it by type (see
            # akn_export.build_hierarchy_tree).
            node["depth_rank"] = rank
        self.nodes.append(node)
        self.stack.append(node)
        self.stack_x0.append(line.x0)
        self.stack_rank.append(rank)
        return node

    def _close_marked_block(self) -> None:
        if self.current_marked_block is not None:
            self.current_marked_block["text"] = self.current_marked_block["text"].strip()
            self.current_marked_block = None

    def _flush_asterisk_run(self, char_end: int) -> None:
        """Three or more asterisks on their own is Victoria's standard way
        of marking "a provision used to be here and was repealed" --
        different from an actual footnote or margin note (type "note"),
        so it gets its own type rather than being lumped in with one: a
        reviewer (or an export) needs to treat "the parser wasn't sure
        what this text was" and "this was deliberately, formally
        repealed" very differently, and grouping them both under "note"
        made that impossible to tell apart later on. Never carries a
        number or heading of its own -- like a genuine note, it isn't
        itself a numbered provision (see _try_bracket_item's own
        subsection/paragraph handling for that); unlike a note, its text
        is always exactly this asterisk marker, never free text a human
        or a margin annotation wrote."""
        run = self.asterisk_run
        # Each printed row of stars is one provision repealed: two rows
        # one under the other are two provisions, not one repeal.
        rows: list[list[BodyLine]] = []
        for l in run:
            if rows and l.page_no == rows[-1][0].page_no and abs(l.y0 - rows[-1][0].y0) < 2.0:
                rows[-1].append(l)
            else:
                rows.append([l])
        if len(run) >= 3 and not any(len(row) >= 3 for row in rows):
            rows = [run]   # stars set one to a row: one marker, as before
        for row in rows:
            if len(row) < 3:
                # Too short a run to be the repealed-text marker -- don't
                # lose it, fold it into whatever's currently open instead.
                for l in row:
                    if not self.stack:
                        self._open_node(self._preamble_level, None, "Preliminary", l, l.y0)
                    _append_text(self.stack[-1], l.text.strip(), l, char_end)
                continue
            marker = {
                "type": "repealed", "number": None, "heading": None,
                "text": ("* " * len(row)).strip(),
                "page_start": row[0].page_no, "page_end": row[-1].page_no,
                "char_start": self.asterisk_start, "char_end": char_end, "source": "rules",
            }
            for l in row:
                add_rect(marker, l)
            self.nodes.append(marker)
            if self.stack and self.stack[-1]["type"] in ("paragraph", "subparagraph"):
                self.repealed_since_item = True
        run.clear()

    def _wrapped(self, line: BodyLine, text: str) -> bool:
        above = self.line_above
        if above is not None and any(self.patterns[k].match(above.text.strip()) for k in ("notes_marker", "example_marker")):
            return False   # "Note" heads what follows it, at whatever size a Note is set
        return _wraps(above, line, text, self.margins, self.body_size)

    def _resolve_hanging_list(self, x0: float) -> bool:
        """A common legislative construct opens a subsection (or section)
        with lead-in text, breaks into a lettered or roman-numeral
        list, and then closes the list with text that grammatically
        resumes the *lead-in's* sentence, not the list item's -- "(a)
        does X; or (b) does Y -- is guilty of an offence." Looking at
        the text alone gives no way to see that, but the PDF's own
        hanging indent does: each level's own wrapped continuation
        lines print at a fixed indent past that level's opening marker,
        so a plain continuation line that outdents back past the
        innermost list item's own indent is resuming whatever
        shallower level actually sits at that indent, not continuing
        the list item. This only ever closes subsection, paragraph or
        subparagraph levels -- Part, Division, Subdivision and Section
        only ever close through an explicit pattern match.

        Returns whether it closed anything, because that is exactly the
        signal that the line about to be consumed is a wrap-up rather
        than an ordinary continuation -- see _consume_as_continuation."""
        closed = False
        while (
            len(self.stack) > 1
            and self.rank[self.stack[-1]["type"]] >= self._hanging_list_floor
            and x0 < self.stack_x0[-1] - _INDENT_TOLERANCE
        ):
            self._close_top()
            closed = True
        return closed

    # -- main pass --------------------------------------------------------

    def feed(self, lines: list[BodyLine]) -> None:
        self.lines_total = len(lines)
        self.margins = _right_margins(lines)
        # A table is claimed whole, by the run of lines it occupies, so
        # everything after its first line is already spoken for. The
        # per-line bookkeeping above still runs for each of them -- they
        # were consumed, just not one at a time.
        consumed_through = -1
        for idx, line in enumerate(lines):
            text = line.text.strip()
            char_start = self.cursor
            char_end = self.cursor + len(text)
            self.cursor = char_end + 1  # account for the "\n" join
            self.lines_consumed += 1
            if idx <= consumed_through:
                continue
            if not text:
                continue
            self.line_above = next((lines[j] for j in range(idx - 1, -1, -1) if lines[j].text.strip()), None)

            table = find_table(lines, idx)
            if table is not None:
                self._open_table(table, lines, char_start)
                consumed_through = table.end - 1
                continue
            next_text = lines[idx + 1].text.strip() if idx + 1 < len(lines) else ""

            if _RULE_RE.match(text):
                continue   # the rule printed under an Act's last provision

            if text == "*":
                if not self.asterisk_run:
                    self.asterisk_start = char_start
                self.asterisk_run.append(line)
                continue
            elif self.asterisk_run:
                self._flush_asterisk_run(char_start - 1)

            if _PREAMBLE_RE.match(text) and not self.nodes:
                # Its paragraphs nest under it as a section's would.
                self._open_node("preamble", None, "Preamble", line, char_start, rank=self.rank[self.top_level_type])
                self.prev_text = text
                continue

            if self.patterns["notes_marker"].match(text):
                self._close_marked_block()
                self.marked_block_type = "note"
                continue

            if self.patterns["example_marker"].match(text):
                self._close_marked_block()
                self.marked_block_type = "example"
                continue

            if self.patterns["penalty_marker"].match(text):
                self._open_penalty(line, text, char_start, char_end)
                continue

            if self.marked_block_type and self._handle_marked_block(line, text, char_start, char_end):
                continue

            was_heading_group = self.prev_was_heading_group
            self.prev_was_heading_group = False
            if not (
                self._try_schedule_item(line, text, char_start, char_end)
                or self._try_bold_heading(line, text, char_start, was_heading_group)
                or self._try_schedule_hangs_off(line, text, char_end)
                or self._try_definition_start(line, text, char_start, char_end, next_text)
                or self._try_bracket_item(line, text, char_start, char_end, was_heading_group)
                or self._try_bold_emphasis(line, text, char_start, char_end, next_text)
            ):
                self._consume_as_continuation(line, text, char_start, char_end)

            self._note_definitions_lead_in(text)
            self.prev_text = text

        if self.asterisk_run:
            self._flush_asterisk_run(self.cursor)
        self._close_marked_block()
        while self.stack:
            self._close_top()

    def result(self) -> ParseResult:
        return ParseResult(
            nodes=self.nodes,
            lines_total=self.lines_total,
            lines_consumed=self.lines_consumed,
            warnings=self.warnings,
            hierarchy=list(self.order),
        )

    # -- classifiers -----------------------------------------------------

    def _ends_marked_block(self, line: BodyLine, text: str) -> bool:
        """True if this line can't possibly be more of a Notes/Example/
        Penalty block's own text -- either it matches one of the ordinary
        structural patterns (_looks_like_boundary: a new Part, Division,
        etc.), or it's a fresh defined term opening inside a Definitions
        section (see _try_definition_start), which _looks_like_boundary
        has no way to see on its own, since it works off text patterns
        alone with no font information -- nothing about a definition's
        shape is something a text pattern alone can catch; only its
        typesetting gives it away."""
        if self._wrapped(line, text):
            # The block's own sentence carrying a reference onto the next
            # line ("...under subsection" / "(2) of that Act"): shaped like
            # a provision, and not one.
            return False
        if self.marked_block_type == "penalty" and not line.bold:
            # A penalty's own wording starts lines with numbers all the
            # time -- "1200 penalty units maximum) or both;", "600
            # penalty units." -- and the section pattern is "a number,
            # then some words", so _looks_like_boundary read half of them
            # as a new section and cut the penalty off mid-sentence. In
            # the ordinary flow that pattern only ever opens a section on
            # a *bold* line (see _try_bold_heading); this holds a penalty
            # to the same rule, leaving only the boundaries that
            # genuinely need no bold to be recognised.
            return (
                text == "*"
                or any(self.patterns[key].match(text) for key in _BRACKETED_LEVELS if key in self.patterns)
                or bool(self._in_definitions_section and line.leading_bold_italic)
            )
        return (
            _looks_like_boundary(text, self.patterns)
            or bool(self._in_definitions_section and line.leading_bold_italic)
            # A bold line set at body size or larger, which in these Acts
            # is what a heading looks like -- and a bare topical caption
            # ("Offences relating to Horse-drawn Vehicles, Public
            # Vehicles, Animals, &c.") is invisible to
            # _looks_like_boundary, which reads text alone. Without this,
            # a Penalty running to the foot of a group's last provision
            # swallowed the caption introducing the next one.
            #
            # The size is what makes it safe. Bold alone is not a
            # heading: a Note is set two points smaller than body text
            # and cites Acts by name, which this drafting sets bold --
            # and where the citation is most of the line, the line's
            # dominant weight *is* bold. Criminal Procedure Act s 6 note
            # 1 ends "...section 528 of / the Children, Youth and
            # Families Act 2005.", whose last line is bold at 10pt
            # against a 12pt body. Read as a heading it ended the Notes
            # block, so that line fell through to paragraph (c), note 2
            # was never recognised as a note at all, and its own "2"
            # leaked into the text of what was left.
            or (line.bold and round(line.size, 1) >= self.body_size)
        )

    def _open_table(self, table, lines: list[BodyLine], char_start: int) -> None:
        """Emits a table (see corpus/tables.py) as one node.

        Appended straight to self.nodes rather than pushed on the stack,
        like a note: it is part of what the provision above it says, and
        nothing nests inside one. Its rows live in its text, which is
        what makes it as editable in review as any other piece."""
        self._close_marked_block()
        self.marked_block_type = None
        block = lines[table.start : table.end]
        end = char_start
        for line in block:
            end += len(line.text.strip()) + 1
        node = {
            "type": "table", "number": None, "heading": table.heading,
            "text": table.text,
            "page_start": block[0].page_no, "page_end": block[-1].page_no,
            "char_start": char_start, "char_end": end - 1, "source": "rules",
        }
        for line in block:
            add_rect(node, line)
        self.nodes.append(node)

    def _open_penalty(self, line: BodyLine, text: str, char_start: int, char_end: int) -> None:
        """Starts a penalty node at a "Penalty: ..." line.

        Unlike Notes and Examples, whose marker word sits alone on its
        own line above the block, a penalty's marker and its content are
        the same line -- so this opens the node rather than merely
        arming a state machine. Everything after it rides the same
        machinery, because a penalty wraps and ends exactly the way
        those do: further plain lines belong to it ("Penalty: Level 3
        imprisonment (20 years / maximum).", and the multi-limb
        "Penalty: If the injury was caused intentionally-- / level 5
        imprisonment..."), and the next structural line ends it.

        Appended straight to self.nodes rather than pushed on the stack,
        like a note: it is a fact about the provision above it, not a
        container, and nothing ever nests inside one.
        """
        self._close_marked_block()
        self.marked_block_type = "penalty"
        self.current_marked_block = {
            "type": "penalty", "number": None, "heading": None, "text": text,
            "page_start": line.page_no, "page_end": line.page_no,
            "char_start": char_start, "char_end": char_end, "source": "rules",
        }
        add_rect(self.current_marked_block, line)
        self.nodes.append(self.current_marked_block)

    def _handle_marked_block(self, line: BodyLine, text: str, char_start: int, char_end: int) -> bool:
        """Inside a "Notes" (or singular "Note"), "Example" or "Penalty"
        block -- self.marked_block_type says which; all three share this
        same state machine, since an Example is set up exactly like a
        singular Note under a different marker word (see basic-
        structure.yaml), and a Penalty differs only in being opened by
        its own first line rather than by a marker above it (see
        _open_penalty). Returns True if the line belongs to the open
        block (the caller skips to the next line); returns False --
        having also closed the block -- when it's ended and the line
        needs normal classification instead."""
        kind = self.marked_block_type
        # Only a "Notes" block's own numbered items ("1 ...", "2 ...")
        # need the note_item pattern -- an Example is never numbered
        # this way (no evidence of "Example 1"/"Example 2" in this
        # drafting convention, only ever a single unnumbered block per
        # callout).
        m = self.patterns["note_item"].match(text) if kind == "note" else None
        if m and self._wrapped(line, text):
            # An Act's year carried onto the next line ("...Provisions) Act"
            # / "1958 provides for..."), not note 1958.
            m = None
        # A hanging-indent note number ("1") can land as its own line,
        # separate from its text, if the PDF laid it out with a tab
        # stop rather than inline -- don't let that split fool us into
        # thinking the notes block ended. Gated on the line not being
        # bold: a note item is always set in plain body text, so a
        # *bold* line with this same "digit(s) then text" shape is
        # actually the next section or clause heading immediately after
        # the Notes block (e.g. "38 Requirements for informant's
        # statement in..."), not a new note -- it falls through to the
        # boundary check below, which ends the block and lets it be
        # reclassified normally.
        if (m or (kind == "note" and text.isdigit() and len(text) <= 3 and not self._wrapped(line, text))) and not line.bold:
            self._close_marked_block()
            self.current_marked_block = {
                "type": kind, "number": m.group(1) if m else text, "heading": None,
                "text": m.group(2) if m else "",
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            add_rect(self.current_marked_block, line)
            self.nodes.append(self.current_marked_block)
            return True
        if self.current_marked_block is not None and not self._ends_marked_block(line, text):
            _append_text(self.current_marked_block, text, line, char_end)
            return True
        if self.current_marked_block is None and not self._ends_marked_block(line, text):
            # A singular, unnumbered block (as opposed to "Notes" with
            # its own numbered "1 ...", "2 ..." items) -- the drafting
            # convention for one explanatory remark or worked example
            # under a single provision. Without this, its first line
            # matched neither branch above (no leading number to open a
            # numbered item with) and immediately fell through to
            # ending the block, silently gluing it onto whatever text
            # was already open instead of ever becoming its own node.
            self.current_marked_block = {
                "type": kind, "number": None, "heading": None, "text": text,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            add_rect(self.current_marked_block, line)
            self.nodes.append(self.current_marked_block)
            return True
        self._close_marked_block()
        self.marked_block_type = None
        return False

    def _try_bold_heading(self, line: BodyLine, text: str, char_start: int, was_heading_group: bool) -> bool:
        """Chapter, Part, Division and Section headings, then the two
        bare-number wrap cases (Subdivision, Section) -- all bold-only."""
        if not line.bold:
            return False

        if text == "Dictionary" and round(line.size, 1) > self.body_size and "schedule" in self.rank:
            # An Act's Dictionary, closing it as a Schedule would.
            self._open_node("dictionary", None, "Dictionary", line, char_start)
            return True

        for level in self._prefix_heading_levels:
            m = self.patterns[level].match(text)
            if not m:
                continue
            if (
                level == "section"
                and _could_be_a_wrapped_citation(m.group(1), m.group(2).strip())
                and not _is_fresh_start(self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels) or was_heading_group)
            ):
                # A bold line that happens to start with a bare number
                # mid-paragraph, not a genuine new section or clause --
                # e.g. an Act-name citation that wraps its year onto its
                # own line ("... Act\n1997 insert—", the "1997" being
                # the citation's year, not a section number). A real
                # section or clause always opens right after the
                # previous one's body reached a clean sentence break
                # (or right after a Part/Division heading line), the
                # same freshness test already used for a numbered
                # Subdivision below. Only "section" needs this: the
                # other prefix levels (Chapter, Part, Division, ...)
                # require a literal keyword ("Chapter "/"Part "/...),
                # which body text is never going to spell out
                # mid-paragraph the way a bare citation year can.
                continue
            node_type = self._provision_type() if level == "section" else level
            self._open_node(node_type, m.group(1), m.group(2).strip(), line, char_start)
            return True

        # A Subdivision heading is a bracketed number plus a short bold
        # title ("(1) Homicide", "(8G) Abrogation of obsolete rules of
        # law") -- or the number is spelled out ("Subdivision 2—Title").
        # The bracket form overlaps in shape with a bracketed
        # paragraph or subparagraph marker ("(a)", "(i)"), which is
        # also commonly bolded when it's an Act-name citation inside an
        # enumerated list ("(i) the Conservation, Forests and Lands
        # Act 1987; or") -- but a Subdivision's number is always
        # digit-led in this drafting convention (paragraphs are
        # letters, subparagraphs are lowercase roman numerals, never
        # digits), so that's the one thing that reliably tells them
        # apart without needing size or position: a lettered or roman
        # bracket here isn't a Subdivision candidate at all, and falls
        # through to bracket-item classification below the same as it
        # always did.
        #
        # A digit-led bracket can still be a false positive of a
        # different kind, though: a bold mid-sentence pinpoint citation
        # that itself starts a new physical line, e.g. "under section
        # 9A(1A) or\n(1B) of the Corrections Act 1986 to exercise ..."
        # -- "(1B)" here continues "or", it's not a fresh heading. A
        # genuine Subdivision only ever opens right after whatever came
        # before it reached a clean sentence break, the same signal
        # used for the unnumbered heading_group case below.
        m = self.patterns["subdivision"].match(text)
        if m:
            if m.group(1) is not None:
                heading = m.group(2).strip()
                if (
                    re.match(r"^\d", m.group(1))
                    and _is_fresh_start(self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels) or was_heading_group)
                    and _looks_like_subdivision_title(heading)
                ):
                    self._open_node("subdivision", m.group(1), heading, line, char_start)
                    return True
            else:
                self._open_node("subdivision", m.group(3), m.group(4).strip(), line, char_start)
                return True

        # A Schedule whose title is set below its number rather than
        # after a dash on the same line (see _BARE_SCHEDULE_RE). Opened
        # with no heading; the next bold line becomes it, through the
        # same empty-heading extension _try_bold_emphasis already does.
        m = _BARE_SCHEDULE_RE.match(text)
        if m and "schedule" in self.rank:
            self._open_node("schedule", m.group(1), None, line, char_start)
            return True

        # A bare section number with nothing else on the line -- the
        # heading wraps onto the next bold line instead (the same wrap
        # pattern as the bare Subdivision case above), e.g. "465AAAA"
        # alone, followed by "Police may use assistants and equipment"
        # as a separate bold line.
        if re.match(r"^\d+[A-Za-z]*$", text) and _is_fresh_start(self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels) or was_heading_group):
            self._open_node(self._provision_type(), text, None, line, char_start)
            return True

        return False

    def _in_schedule(self) -> bool:
        return any(n["type"] in ("schedule", "dictionary") for n in self.stack)

    def _provision_type(self) -> str:
        """What a numbered provision is called here: the Act's own word
        (section, or a Bill's clause), and in a Schedule a clause -- an
        item where the Schedule is one of amendments (issue #72)."""
        schedule = next((n for n in reversed(self.stack) if n["type"] in ("schedule", "dictionary")), None)
        if schedule is None:
            return self.top_level_type
        if schedule["type"] == "dictionary":
            return "clause"
        return "item" if _AMENDING_SCHEDULE_RE.search(schedule.get("heading") or "") else "clause"

    def _try_schedule_item(self, line: BodyLine, text: str, char_start: int, char_end: int) -> bool:
        """A Schedule's numbered list -- "1 Sections 36(5)... of the
        Children, Youth and Families Act 2005" -- set as a hanging list,
        not as clause headings: plain type, in from the column headings
        start at, numbered one after another. Read as headings, only the
        items whose Act name made them bold became provisions, and the
        rest ran into the Schedule's text (Family Violence Protection Act
        Sch 1).

        The numbering is what makes it safe: a wrapped line that happens
        to open with a number ("2005 and ...") is not the next item."""
        m = _SCHEDULE_ITEM_RE.match(text)
        if not m or not self._in_schedule() or self.heading_x0 is None:
            return False
        if line.x0 <= self.heading_x0 + _INDENT_TOLERANCE * 3:
            return False   # the heading column: a clause heading, handled as one
        if not _next_item_number(self.schedule_last_number, m.group(1)):
            return False
        self._open_node(self._provision_type(), m.group(1), None, line, char_start)
        _append_text(self.stack[-1], m.group(2).strip(), line, char_end)
        return True

    def _try_schedule_hangs_off(self, line: BodyLine, text: str, char_end: int) -> bool:
        """Immediately under a fresh Schedule heading, a standalone plain
        (non-bold) line like "Sections 6(3), 159(3)" or "Section 5"
        names which section(s) the Schedule "hangs off" (see basic-
        structure.yaml's own note on this) -- not really part of the
        heading, but not part of the Schedule's real content either, so
        it's folded into the heading (in parentheses) rather than
        either. Kept narrow on purpose (only right after a Schedule
        heading with no body text yet), so an ordinary cross-reference
        sentence elsewhere that happens to start the same way is never
        mistaken for one.

        Folding it into the heading rather than appending it as body
        text also matters for a reason beyond just "where does this
        belong": appending it to the Schedule's own `text` would make
        that text non-empty, and _is_fresh_start's own check for
        whether the previous line left us inside an empty heading
        (_prev_line_was_heading) relies on exactly that emptiness to
        decide whether the Schedule's first numbered item right after
        this line is a genuine fresh section start. Getting that wrong
        would silently glue the Schedule's first real clause onto this
        line as plain continuation text, instead of giving it its own
        node."""
        top = self.stack[-1] if self.stack else None
        if top is None or top["type"] not in ("schedule", "dictionary") or top["text"] or line.bold:
            return False
        if not _SCHEDULE_HANGS_OFF_RE.match(text):
            return False
        if top["heading"]:
            _append_heading(top, f"({text})", char_end, line)
        else:
            # The bare "SCHEDULE 1" form, whose title is still to come
            # on the next bold line -- hold this so it lands after the
            # title rather than becoming the start of the heading.
            self._pending_hangs_off = (top, f"({text})", char_end, line)
            top["char_end"] = char_end
        return True

    def _try_definition_start(self, line: BodyLine, text: str, char_start: int, char_end: int,
                              next_text: str = "") -> bool:
        """Inside a Definitions/Interpretation section, a defined term is
        reliably set bold and italic where it's introduced ("accused
        means a person who—") -- clearly different typesetting from the
        ordinary bold-only emphasis used elsewhere (Act-name citations)
        and the italic-only case citations that also appear in body
        text, so line.leading_bold_italic (see extract.py) is a much
        stronger signal here than the text-pattern guesses
        definitions.py falls back to elsewhere for cross-linking.
        Splitting each one into its own "definition" node -- nested at
        the same depth a numbered subsection would be (see
        hierarchy.py's make_ranks), so a definition's own (a)/(b) list
        still nests correctly under it -- rather than leaving a whole
        run of definitions joined into one Section's single, unbroken
        block of text, lets a human reviewer actually see and work
        through each term one at a time, the same way review.py already
        lets them work through a Section's own numbered pieces one at a
        time.

        Gated on self._in_definitions_section so an unrelated bold and
        italic run elsewhere (there's no known case of one, but nothing
        rules one out) can never be mistaken for a defined term outside
        a section that's actually introducing them."""
        if not (self._in_definitions_section and line.leading_bold_italic):
            return False
        in_dictionary = any(n["type"] == "dictionary" for n in self.stack)
        if round(line.size, 1) > self.body_size or (not in_dictionary and _looks_like_group_heading(
            text, self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels), next_text
        )):
            # A topical heading between two sections ("Theft, robbery,
            # burglary, &c.", "Fingerprinting"), not a defined term. Now
            # that a lead-in can turn definitions on part-way through a
            # section, the run stays on until the next section opens --
            # and one of these headings can arrive first, where it would
            # otherwise be swallowed as a term and take the rest of the
            # Act's structure with it. A defined term is never set larger
            # than body text, and never has a heading's shape. A
            # Dictionary has no topical headings among its terms, and its
            # first term can stand alone on its line under the Part
            # heading ("ACT court", defined only by a Note).
            return False
        term = line.leading_bold_italic
        if not text.startswith(term):
            # Shouldn't happen (term is built from this same line's own
            # leading spans -- see extract.py's _leading_bold_italic),
            # but if some edge case ever misaligns them, falling
            # through to ordinary continuation handling is safe;
            # slicing on a mismatched prefix here would not be.
            return False
        remainder = text[len(term) :].strip()

        top = self.stack[-1] if self.stack else None
        if top is not None and top["type"] == "definition" and not top["text"] and self.line_above is self._term_line:
            # Only straight after the term's own line: a term defined by a
            # Note alone ("ACT court" in the Evidence Act's Dictionary) has
            # the Note between it and the next term, which is new.
            #
            # The currently-open definition's own term wrapped onto this
            # second physical line (a long one, e.g. "indictable
            # offence that may be heard and" / "determined summarily
            # means an offence to..."), rather than this line starting
            # a genuinely new definition -- its text is still empty,
            # meaning the previous line was consumed entirely as
            # heading with nothing left over. Extend the heading
            # instead, the same way a wrapped Part/Division/Section
            # heading already does (see _try_bold_emphasis's own
            # heading-wrap branch) -- a definition just has no
            # stack-level "no body text yet" flag of its own the way
            # _prev_line_was_heading checks for those, so this checks
            # it directly.
            _append_heading(top, term, char_end, line)
            if remainder:
                _append_text(top, remainder, line, char_end)
            self._term_line = line
            return True

        self._open_node("definition", None, term, line, char_start, rank=self._definition_rank)
        self._term_line = line
        if remainder:
            _append_text(self.stack[-1], remainder, line, char_end)
        return True

    def _try_bracket_item(self, line: BodyLine, text: str, char_start: int, char_end: int, was_heading_group: bool) -> bool:
        """Bracket items (subsection, paragraph, subparagraph,
        sub_subparagraph) are classified by their content's shape and
        sequence, not by boldness -- Act-name citations are commonly
        bolded throughout these Acts, so boldness alone would give a
        false signal."""
        m = self.patterns["subsection"].match(text)
        bracket_match, level = None, None
        if m and re.match(r"^\d", m.group(1)):
            bracket_match, level = m, "subsection"
        else:
            m2 = self.patterns["paragraph"].match(text) or self.patterns["subparagraph"].match(text)
            if m2:
                bracket_match, level = m2, _bracket_level(m2.group(1), self.stack, self.repealed_since_item)
            else:
                m3 = self.patterns["sub_subparagraph"].match(text)
                if m3:
                    # Bracketed capital letters -- "(A)", "(B)" -- never
                    # overlap with paragraph's lowercase letters or
                    # subparagraph's lowercase roman numerals, so unlike
                    # those two (see _bracket_level), there's no
                    # sequence ambiguity to resolve here.
                    bracket_match, level = m3, "sub_subparagraph"
        if not (level and bracket_match):
            return False
        if self._wrapped(line, text):
            # A reference the sentence above carried onto this line --
            # "subject to subsections (2) and" / "(3), admissible as if..."
            # -- which would otherwise open a second (3) mid-sentence.
            return False
        if self.prev_text.rstrip().endswith(","):
            # A wrapped list of cross-references, not a new provision:
            # "a provision of Subdivision (8A), (8B)," / "(8C), (8D), ..."
            # would otherwise open a subsection numbered 8C in the middle
            # of a sentence, taking the rest of that sentence with it and
            # throwing the numbering of everything after it.
            #
            # A comma is the signal because legislative drafting never
            # puts one before a new numbered provision: an item ends with
            # a full stop, a semicolon, "; or", "; and" or an em dash.
            # Every one of the 35 places in this project's parsed corpus
            # where a bracketed provision follows a comma-ended line is
            # this same mistake.
            return False

        remainder = bracket_match.group(2).strip() or None
        if (
            level == "subsection"
            and line.bold
            and remainder is None
            and _is_fresh_start(self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels) or was_heading_group)
        ):
            # A bare bold "(N)" with nothing else on the line -- the
            # Subdivision's title wraps onto the next bold line instead
            # (mirrors the heading-wrap handling below), e.g. "(4A)" /
            # "Non-fatal strangulation" as two lines. A genuine
            # subsection number is never bold on its own, and a genuine
            # Subdivision only opens right after a clean sentence break
            # -- this guards against a bold bracket that's actually a
            # pinpoint citation ("section 9A(1A) or\n(1B) of ...")
            # wrapping mid-sentence instead.
            level = "subdivision"
        self._open_node(level, bracket_match.group(1), None, line, char_start)
        if remainder:
            _append_text(self.stack[-1], remainder, line, char_end)
        return True

    def _try_bold_emphasis(self, line: BodyLine, text: str, char_start: int, char_end: int, next_text: str) -> bool:
        """Any remaining bold line: a wrapped heading tail, a bare
        topical heading_group, or plain inline emphasis folded into the
        open node. A bold line always ends up handled here -- this
        never returns False for one."""
        if not line.bold:
            return False

        top = self.stack[-1] if self.stack else None
        if top is not None and top["type"] in self.heading_levels and not top["text"]:
            # Heading text that wrapped onto another bold line, e.g.
            # "3A Unintentional killing in the course or furtherance\nof
            # a crime of violence" -- extend the heading, not the body.
            _append_heading(top, text, char_end, line)
        elif round(line.size, 1) > self.body_size or (
            round(line.size, 1) == self.body_size
            and _looks_like_group_heading(text, self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels), next_text)
        ):
            # A bare topical heading grouping a run of sections. Usually
            # bold and visibly larger than body text (e.g. "Fraud and
            # blackmail"), but some Acts set these at plain body size
            # ("Theft, robbery, burglary, &c."), which can only be told
            # apart from inline bold emphasis by shape -- see
            # _looks_like_group_heading. It always sits between two
            # sections, never inside one, but rebuilding the tree
            # (build_hierarchy_tree in akn_export.py, shared by both
            # exporters) works out node *types* from this flat list on
            # its own, separately from this function's stack -- so
            # that's where attachment gets corrected, not here.
            group = {
                "type": "heading_group", "number": None, "heading": text, "text": text,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            add_rect(group, line)
            self.nodes.append(group)
            self.prev_was_heading_group = True
        else:
            # Bold at body size with no structural pattern matching --
            # inline emphasis (e.g. a defined term or, as above, an
            # Act-name citation), not a boundary.
            if not self.stack:
                self._open_node(self._preamble_level, None, "Preliminary", line, char_start)
            _append_text(self.stack[-1], text, line, char_end)
        return True

    def _note_definitions_lead_in(self, text: str) -> None:
        """"In this section—" and its relatives open a run of defined
        terms from wherever they appear, not only from a section headed
        Definitions. The Criminal Procedure Act's section 4 is the case
        that matters: it is headed "Meaning of sexual offence" and puts
        four defined terms in its subsection (6), where nothing in the
        heading announces them and they would otherwise arrive as one
        unbroken block of text inside that subsection.

        The terms are opened one level below whatever provision carried
        the lead-in, so they nest under it. In a section that *is* headed
        Definitions the lead-in is usually the section's own first line,
        which leaves the depth where it already was."""
        if not _DEFINITIONS_LEAD_IN_RE.search(text) or not self.stack:
            return
        self._in_definitions_section = True
        top = self.stack[-1]
        # A lead-in in the Section's own text leaves definitions where
        # make_ranks puts them; one inside a subsection nests them there.
        self._definition_rank = None if top["type"] in (self.top_level_type, "clause", "item") else self.stack_rank[-1] + 1

    def _consume_as_continuation(self, line: BodyLine, text: str, char_start: int, char_end: int) -> None:
        """Continuation of whatever is currently open. If nothing is
        open yet (preamble text before the first Part), open a
        synthetic holder rather than dropping it.

        Where the line outdented past a list to resume the sentence the
        provision opened with, it becomes a node of its own rather than
        being added to that provision's text. It has to: the list items
        are already in the node list, so appending here would print the
        wrap-up *before* the list it comes after. The Summary Offences
        Act's section 5 read "Where in a prosecution for obstructing a
        footpath street or road under—the obstruction alleged is by
        assemblage of persons ..." with its (a) and (b) stranded
        afterwards, which is not what the section says."""
        if not self.stack:
            self._open_node(self._preamble_level, None, "Preliminary", line, char_start)
            self.warnings.append(
                f"page {line.page_no}: text before any recognised Part -- filed under a synthetic preamble node"
            )
        elif self._resolve_hanging_list(line.x0) and self.stack[-1]["text"]:
            top = self.stack[-1]
            if top["type"] != "continuation":
                # One level inside the provision being resumed, so it sits
                # with that provision's list items and after them.
                self._open_node("continuation", None, None, line, char_start,
                                rank=self.stack_rank[-1] + 1)
        _append_text(self.stack[-1], text, line, char_end)


def read_box(node: dict, lines: list[BodyLine], patterns: dict) -> dict:
    """What a reviewer's box says this provision is, given what it
    already is. Returns the fields to change -- {"text": ...}, or
    {"number": ..., "heading": ...} for a provision that is only a
    heading.

    The words in a box are the page's own, and a provision does not store
    them that way. "(c) if a summons is issued..." is stored with the
    "(c)" as the node's number and only what follows as its text, so the
    marker has to come off or a provision corrected twice would end up
    printing it twice.

    What this deliberately does *not* do is re-decide what the provision
    is. Running the lines back through the classifier was tried and is
    the wrong tool: a subparagraph in isolation is indistinguishable from
    a paragraph, a note without the "Notes" heading above it is just
    text, and a continuation only exists as the resolution of a list that
    a box round it does not contain. Those types are identified from
    their surroundings, which a box excludes by design. The reviewer has
    already said what the provision is -- they are correcting what it
    says.

    Three kinds of provision, because the words in a box mean three
    different things:

      - a table's meaning is in its columns, so its box is read the way
        the page was read in the first place (see tables.find_table);
      - a provision that is only a heading -- a Chapter, a Part -- keeps
        its words in `heading`, split from its number by the same profile
        pattern that split them at parse time;
      - everything else keeps them in `text`, with its own marker off the
        front.
    """
    from .tables import find_table   # circular at module scope: tables imports extract, which this does too

    boxed = list(lines)
    if node.get("type") == "table":
        table = find_table(boxed, 0)
        if table is None:
            raise ValueError(
                "No table in that box -- a table is recognised from cells sitting side by side, "
                "so the box has to cover the columns, not one of them."
            )
        return {"text": table.text, "heading": table.heading or node.get("heading")}

    printed = ""
    for line in boxed:
        stripped = line.text.strip()
        if stripped:
            printed = join_printed_line(printed, stripped)

    if not (node.get("text") or "").strip() and node.get("heading"):
        pattern = patterns.get(node.get("type"))
        m = pattern.match(printed) if pattern else None
        if m and m.lastindex and m.lastindex >= 2:
            return {"number": m.group(1), "heading": m.group(2).strip()}
        return {"heading": printed}

    return {"text": _without_own_marker(printed, node)}


def _without_own_marker(printed: str, node: dict) -> str:
    """The printed words with this node's own marker taken off the front,
    whichever way the page prints it: "(c) ", "3 Short title ", or -- for
    a defined term, whose marker is the term itself -- "accused "."""
    number, heading = node.get("number"), node.get("heading")
    candidates = []
    if number and heading:
        candidates.append(f"{number} {heading}")
    if number:
        candidates += [f"({number})", str(number)]
    if heading:
        candidates.append(heading)
    for marker in candidates:
        if printed.startswith(marker):
            return printed[len(marker):].lstrip(" ")
    return printed


def parse_act(
    pages: list[PageText],
    profile_name: str | None = None,
    top_level_type: str = "section",
) -> ParseResult:
    """top_level_type: "section" for an enacted Act (the default),
    "clause" to parse a Bill instead -- same drafting shape and the
    same profile patterns apply either way (see _LineParser's own
    docstring on this parameter), just the resulting node type differs.

    Always looks for the fixed enacting phrase (see _skip_front_matter)
    and discards everything up to and including it -- a Bill's title
    page and Table of Provisions, or an Act's own reprinted identity
    block, whichever this document turns out to have. If neither is
    present (an unusual layout, or a test fixture with no front matter
    at all), that's a safe no-op: the lines are used unchanged. The
    skipped lines are recorded as a warning rather than silently
    dropped, so the completeness count in the result stays an honest,
    checkable reflection of what was deliberately excluded and why --
    see the module docstring's own completeness guarantee."""
    patterns = load_profile(profile_name)
    hierarchy_order = load_hierarchy(profile_name)
    lines = _flatten_lines(pages)
    warnings: list[str] = []
    remaining = _skip_front_matter(lines)
    skipped = len(lines) - len(remaining)
    if skipped:
        warnings.append(f"skipped {skipped} front-matter line(s) before the enacting words")
    lines = remaining
    parser = _LineParser(patterns, _body_font_size(lines), hierarchy_order, top_level_type=top_level_type)
    parser.feed(lines)
    result = parser.result()
    result.warnings = warnings + result.warnings
    return result
