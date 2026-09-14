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
`_consume_as_continuation`. Each classifier returns True once it's
handled the line. The bookkeeping for the open-node stack
(`_open_node`/`_close_top`/...) is shared state on the instance.
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from .definitions import looks_like_definitions_section
from .extract import BodyLine, PageText
from .hierarchy import HIERARCHY_ORDER, heading_levels, make_ranks
from .profiles import load_hierarchy, load_profile


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
_ENACTING_WORDS_RE = re.compile(r"^The Parliament of Victoria enacts:?\s*$")
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
    recognise."""
    for i, line in enumerate(lines):
        text = line.text.strip()
        if _ENACTING_WORDS_RE.match(text) or _OLD_ENACTING_WORDS_RE.search(text):
            return lines[i + 1 :]
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


def _bracket_level(content: str, stack: list[dict]) -> str:
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
            if _continues_letters(top["number"], content):
                return "paragraph"
            return "subparagraph"
        if top["type"] == "subparagraph":
            if _continues_romans(top["number"], content):
                return "subparagraph"
            if len(stack) >= 2 and stack[-2]["type"] == "paragraph":
                return "paragraph"
    return "paragraph"


_INDENT_TOLERANCE = 3.0


# A line that ends in a hyphen or a dash was broken at a character the
# words already contained -- "charge-sheet", "cross-examine",
# "Broad-based", "of—" -- so the next line joins straight onto it. Every
# one of the hyphen-ending lines across this project's parsed corpus is
# such a compound; none is a word a typesetter split for fit, which is
# why undoing the break would be wrong here.
_JOINS_TIGHT = ("-", "\u2014", "\u2013")


def _append_text(node: dict, text: str, line: BodyLine, char_end: int) -> None:
    """Adds one more printed line to a node's text as running prose.

    The line break itself is not part of the legislation -- it is where
    the PDF's column happened to run out -- so it is not kept. Keeping it
    meant every consumer had to undo it (and several did, differently, or
    forgot to), and it made the stored text disagree with the same words
    quoted anywhere else."""
    if node["text"]:
        joiner = "" if node["text"].endswith(_JOINS_TIGHT) else " "
        node["text"] = node["text"] + joiner + text
    else:
        node["text"] = text
    node["page_end"] = line.page_no
    node["char_end"] = char_end


def _append_heading(node: dict, text: str, char_end: int) -> None:
    node["heading"] = (node["heading"] + " " + text) if node["heading"] else text
    node["char_end"] = char_end


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
        # ai_pipeline.definitions.looks_like_definitions_section) --
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
        node, text, char_end = self._pending_hangs_off
        self._pending_hangs_off = None
        _append_heading(node, text, char_end)

    def _open_node(self, level: str, number: str | None, heading: str | None, line: BodyLine,
                   char_start: int, rank: "int | None" = None) -> dict:
        self._flush_hangs_off()
        if rank is None:
            rank = self.rank[level]
        # Compared against the depth each open node was *opened* at, not
        # its type's own: a definition's depth depends on what introduced
        # it (see _definition_rank), so two definitions opened inside the
        # same subsection still close each other, while neither closes
        # that subsection.
        while self.stack and self.stack_rank[-1] >= rank:
            self._close_top()
        if level == self.top_level_type:
            # A new Section starts a clean slate: whether it defines
            # terms is its own business, and any lead-in depth from the
            # previous one goes with it.
            self._in_definitions_section = looks_like_definitions_section(heading)
            self._definition_rank = None
        node = {
            "type": level, "number": number, "heading": heading, "text": "",
            "page_start": line.page_no, "page_end": line.page_no,
            "char_start": char_start, "char_end": char_start, "source": "rules",
        }
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
        if len(run) >= 3:
            first, last = run[0], run[-1]
            self.nodes.append({
                "type": "repealed", "number": None, "heading": None,
                "text": ("* " * len(run)).strip(),
                "page_start": first.page_no, "page_end": last.page_no,
                "char_start": self.asterisk_start, "char_end": char_end, "source": "rules",
            })
        else:
            # Too short a run to be the repealed-text marker -- don't
            # lose it, fold it into whatever's currently open instead.
            for l in run:
                if not self.stack:
                    self._open_node(self._preamble_level, None, "Preliminary", l, l.y0)
                _append_text(self.stack[-1], l.text.strip(), l, char_end)
        run.clear()

    def _resolve_hanging_list(self, x0: float) -> None:
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
        only ever close through an explicit pattern match."""
        while (
            len(self.stack) > 1
            and self.rank[self.stack[-1]["type"]] >= self._hanging_list_floor
            and x0 < self.stack_x0[-1] - _INDENT_TOLERANCE
        ):
            self._close_top()

    # -- main pass --------------------------------------------------------

    def feed(self, lines: list[BodyLine]) -> None:
        self.lines_total = len(lines)
        for idx, line in enumerate(lines):
            text = line.text.strip()
            char_start = self.cursor
            char_end = self.cursor + len(text)
            self.cursor = char_end + 1  # account for the "\n" join
            self.lines_consumed += 1
            if not text:
                continue
            next_text = lines[idx + 1].text.strip() if idx + 1 < len(lines) else ""

            if text == "*":
                if not self.asterisk_run:
                    self.asterisk_start = char_start
                self.asterisk_run.append(line)
                continue
            elif self.asterisk_run:
                self._flush_asterisk_run(char_start - 1)

            if self.patterns["notes_marker"].match(text):
                self._close_marked_block()
                self.marked_block_type = "note"
                continue

            if self.patterns["example_marker"].match(text):
                self._close_marked_block()
                self.marked_block_type = "example"
                continue

            if self.marked_block_type and self._handle_marked_block(line, text, char_start, char_end):
                continue

            was_heading_group = self.prev_was_heading_group
            self.prev_was_heading_group = False
            if not (
                self._try_bold_heading(line, text, char_start, was_heading_group)
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
        """True if this line can't possibly be more of a Notes/Example
        block's own text -- either it matches one of the ordinary
        structural patterns (_looks_like_boundary: a new Part, Division,
        etc.), or it's a fresh defined term opening inside a Definitions
        section (see _try_definition_start), which _looks_like_boundary
        has no way to see on its own, since it works off text patterns
        alone with no font information -- nothing about a definition's
        shape is something a text pattern alone can catch; only its
        typesetting gives it away."""
        return _looks_like_boundary(text, self.patterns) or bool(self._in_definitions_section and line.leading_bold_italic)

    def _handle_marked_block(self, line: BodyLine, text: str, char_start: int, char_end: int) -> bool:
        """Inside a "Notes" (or singular "Note") or "Example" block --
        self.marked_block_type says which; both share this same state
        machine, since an Example is set up exactly like a singular
        Note, just under a different marker word (see basic-
        structure.yaml). Returns True if the line belongs to the open
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
        if (m or (kind == "note" and text.isdigit() and len(text) <= 3)) and not line.bold:
            self._close_marked_block()
            self.current_marked_block = {
                "type": kind, "number": m.group(1) if m else text, "heading": None,
                "text": m.group(2) if m else "",
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
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
            node_type = self.top_level_type if level == "section" else level
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
            self._open_node(self.top_level_type, text, None, line, char_start)
            return True

        return False

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
        if top is None or top["type"] != "schedule" or top["text"] or line.bold:
            return False
        if not _SCHEDULE_HANGS_OFF_RE.match(text):
            return False
        if top["heading"]:
            _append_heading(top, f"({text})", char_end)
        else:
            # The bare "SCHEDULE 1" form, whose title is still to come
            # on the next bold line -- hold this so it lands after the
            # title rather than becoming the start of the heading.
            self._pending_hangs_off = (top, f"({text})", char_end)
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
        if round(line.size, 1) > self.body_size or _looks_like_group_heading(
            text, self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels), next_text
        ):
            # A topical heading between two sections ("Theft, robbery,
            # burglary, &c.", "Fingerprinting"), not a defined term. Now
            # that a lead-in can turn definitions on part-way through a
            # section, the run stays on until the next section opens --
            # and one of these headings can arrive first, where it would
            # otherwise be swallowed as a term and take the rest of the
            # Act's structure with it. A defined term is never set larger
            # than body text, and never has a heading's shape.
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
        if top is not None and top["type"] == "definition" and not top["text"]:
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
            _append_heading(top, term, char_end)
            if remainder:
                _append_text(top, remainder, line, char_end)
            return True

        self._open_node("definition", None, term, line, char_start, rank=self._definition_rank)
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
                bracket_match, level = m2, _bracket_level(m2.group(1), self.stack)
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
            _append_heading(top, text, char_end)
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
            self.nodes.append({
                "type": "heading_group", "number": None, "heading": text, "text": text,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            })
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
        self._definition_rank = None if top["type"] == self.top_level_type else self.stack_rank[-1] + 1

    def _consume_as_continuation(self, line: BodyLine, text: str, char_start: int, char_end: int) -> None:
        """Continuation of whatever is currently open. If nothing is
        open yet (preamble text before the first Part), open a
        synthetic holder rather than dropping it."""
        if not self.stack:
            self._open_node(self._preamble_level, None, "Preliminary", line, char_start)
            self.warnings.append(
                f"page {line.page_no}: text before any recognised Part -- filed under a synthetic preamble node"
            )
        else:
            self._resolve_hanging_list(line.x0)
        _append_text(self.stack[-1], text, line, char_end)


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
