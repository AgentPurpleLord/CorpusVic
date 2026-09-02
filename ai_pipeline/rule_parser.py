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

The output is a flat, ordered node list (tree.py reconstructs the
hierarchy from it), so
review.py and tree.py work unchanged.

The key guarantee an LLM can't give you: every input line is consumed by
exactly one output node. There is no code path that silently drops a line;
anything that doesn't match a known pattern becomes continuation text of
whatever node is currently open. `parse_result.lines_total ==
parse_result.lines_consumed` is a hard assertion, not a hope.

Structure: `parse_act` builds a `_LineParser` and feeds it the flat line
list. `_LineParser.feed` is the single pass; for each line it runs the
classifiers below in priority order (`_try_bold_heading` ->
`_try_schedule_hangs_off` -> `_try_definition_start` ->
`_try_bracket_item` -> `_try_bold_emphasis`), and any line none of them
claims falls through to `_consume_as_continuation`. Each classifier
returns True once it has consumed the line. The stack bookkeeping
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
    # The resolved container ordering this parse used (default, or the
    # profile's `hierarchy:` override) -- run_pipeline.py persists it so the
    # exporters can rebuild the tree with the same level order.
    hierarchy: list[str] = field(default_factory=lambda: list(HIERARCHY_ORDER))


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


# Every Victorian Bill's actual operative text opens with this exact,
# fixed formula -- a reliable anchor for skipping a Bill's own front
# matter (see _skip_bill_front_matter).
_ENACTING_WORDS_RE = re.compile(r"^The Parliament of Victoria enacts:?\s*$")


def _skip_bill_front_matter(lines: list[BodyLine]) -> list[BodyLine]:
    """A Bill's introduction print opens with a title page and a multi-
    page Table of Provisions -- a table of contents whose rows repeat
    every real Part/clause heading's own text closely enough (same
    numbering, similar bold/size choices) to fool the heading classifiers
    below into treating the TOC itself as structure. An enacted Act's own
    PDF never carries this front matter at all (a Bill-only artifact of
    the introduction print, gone by the time it's reprinted as an
    Authorised Version), so there's nothing equivalent to guard against
    when parsing an Act -- this is only ever called for a Bill (see
    parse_act's skip_front_matter parameter). Skips everything up to and
    including the fixed enacting formula every Bill's real text opens
    with; returns the lines unchanged if that formula isn't found, rather
    than silently discarding the whole document on a layout it doesn't
    recognise."""
    for i, line in enumerate(lines):
        if _ENACTING_WORDS_RE.match(line.text.strip()):
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


def _prev_line_was_heading(stack: list[dict], heading_levels: set[str]) -> bool:
    """True if the line just processed left us still inside an open
    heading's own title -- a Chapter/Part/Division/Subdivision/Section
    node with no body text yet, either because it just opened or its
    title wrapped across more than one bold line -- as opposed to inside
    an ordinary node's body content, even body content that happens to be
    bold throughout. An Act-name citation commonly spans several
    *consecutive* bold lines ("... the Crimes\n(Mental Impairment and
    Unfitness to be\nTried) Act 1997, the Magistrates' Court\nAct 1989,
    ..."); only the first genuine heading line should count as a fresh
    start, so this checks what the previous line actually *did* (extend
    an open heading with no body content yet) rather than merely whether
    it was bold, which every line in a wrapped citation is.

    `heading_levels` is this Act's own resolved set (self.heading_levels
    -- see hierarchy.py's heading_levels(), which varies per-profile once
    a Chapter level is in play), not the module-level default -- a free
    function rather than a method purely so it stays trivially callable
    from _looks_like_group_heading below without needing a _LineParser
    instance."""
    return bool(stack) and stack[-1]["type"] in heading_levels and not stack[-1]["text"]


def _is_fresh_start(prev_text: str, prev_line_was_heading: bool) -> bool:
    """Is the line that follows starting clean, rather than continuing a
    sentence in progress? True at the very start of the document, right
    after body text that reached a terminal full stop/semicolon/etc., or
    right after a Part/Division/Subdivision/Section heading line (those
    never themselves end in that punctuation -- "Division 1—Offences
    against the person" has nothing to terminate). Used to gate several
    "is this really a heading, or just continuing whatever came before"
    decisions below: a numbered Subdivision, a bare Section-number wrap,
    and a bare topical heading_group all only ever legitimately start
    right after one of these two things."""
    return not prev_text or prev_line_was_heading or bool(_TERMINAL_PUNCT_RE.search(prev_text))


def _looks_like_group_heading(text: str, prev_text: str, prev_line_was_heading: bool, next_text: str) -> bool:
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
    return _is_fresh_start(prev_text, prev_line_was_heading)


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


def _append_text(node: dict, text: str, line: BodyLine, char_end: int) -> None:
    node["text"] = (node["text"] + "\n" + text) if node["text"] else text
    node["page_end"] = line.page_no
    node["char_end"] = char_end


def _append_heading(node: dict, text: str, char_end: int) -> None:
    node["heading"] = (node["heading"] + " " + text) if node["heading"] else text
    node["char_end"] = char_end


def _looks_like_boundary(text: str, patterns: dict) -> bool:
    return any(
        compiled.match(text)
        for key, compiled in patterns.items()
        if key not in ("notes_marker", "note_item", "example_marker")
    ) or text == "*"


# A Schedule's own heading is sometimes immediately followed by a
# standalone line naming which section(s) it "hangs off" -- "Sections
# 6(3), 159(3)" or "Section 5" -- see basic-structure.yaml's own note on
# this and _try_schedule_hangs_off below.
_SCHEDULE_HANGS_OFF_RE = re.compile(r"^Sections?\s+[\d()\s,]+\.?$")


class _LineParser:
    """One pass over the flat body-line list. See the module docstring for
    the classifier priority order; each `_try_*`/`_handle_*` method returns
    True once it has consumed the current line."""

    def __init__(self, patterns: dict, body_size: float, hierarchy_order: list[str], top_level_type: str = "section"):
        self.patterns = patterns
        self.body_size = body_size
        # The node type emitted for the top-level numbered provision this
        # drafting convention calls a "section" pattern-wise -- "section"
        # itself for an Act, "clause" for a Bill (see hierarchy.py's
        # HIERARCHY_RANK entry for "clause" and schema.py's NODE_TYPES).
        # The *pattern* lookup key is always "section" regardless -- a
        # profile's regex for this level doesn't change between the two,
        # only what the resulting node gets called.
        self.top_level_type = top_level_type

        self.order = list(hierarchy_order)
        self.rank = make_ranks(self.order)
        self.heading_levels = heading_levels(self.order)
        self._hanging_list_floor = self.rank["subsection"]
        # The heading levels that use the "Word N—Title" shape (Chapter/
        # Part/Division/...) plus "section" itself, tried in hierarchy
        # order in _try_bold_heading. "subdivision" is excluded -- it has
        # its own two-form handling right after.
        self._prefix_heading_levels = [
            lvl for lvl in self.order
            if self.rank[lvl] <= self.rank["section"] and lvl != "subdivision" and lvl in patterns
        ]
        # Synthetic bucket for text before the first real container. "part"
        # for every real Victorian Act; the top level otherwise (a profile
        # could conceivably drop "part").
        self._preamble_level = "part" if "part" in self.rank else self.order[0]

        self.nodes: list[dict] = []
        self.stack: list[dict] = []
        self.stack_x0: list[float] = []
        self.warnings: list[str] = []

        # Which kind of bold-marker block ("Notes"/singular "Note", or
        # "Example") is currently open, if any -- see _handle_marked_block.
        # Both share the exact same state machine, just under a different
        # marker word and resulting node type.
        self.current_marked_block: dict | None = None
        self.marked_block_type: str | None = None
        self.asterisk_run: list[BodyLine] = []
        self.asterisk_start = 0

        self.cursor = 0
        self.lines_total = 0
        self.lines_consumed = 0
        self.prev_text = ""
        # A bare topical heading_group (e.g. a Bill's "CHAPTER 7--..."
        # caption) isn't pushed onto self.stack the way a Part/Division/
        # Section is -- it's appended straight to self.nodes instead (see
        # _try_bold_emphasis) -- so _prev_line_was_heading's own stack
        # inspection can't see it. Tracked separately here so a fresh
        # section/clause heading right after one of these still counts as
        # a clean structural boundary for _is_fresh_start, the same as
        # right after a Part/Division heading.
        self.prev_was_heading_group = False
        # Whether the currently-open Section/Clause looks like a
        # Definitions/Interpretation section (see
        # ai_pipeline.definitions.looks_like_definitions_section) -- set
        # fresh every time one opens (_open_node, below), gating
        # _try_definition_start so a bold+italic leading run is only ever
        # promoted to its own "definition" node inside a section that's
        # actually introducing defined terms, never on an incidental
        # bold+italic run elsewhere.
        self._in_definitions_section = False

    # -- stack bookkeeping ---------------------------------------------------

    def _close_top(self) -> None:
        node = self.stack.pop()
        self.stack_x0.pop()
        node["text"] = node["text"].strip()

    def _open_node(self, level: str, number: str | None, heading: str | None, line: BodyLine, char_start: int) -> dict:
        rank = self.rank[level]
        while self.stack and self.rank[self.stack[-1]["type"]] >= rank:
            self._close_top()
        if level == self.top_level_type:
            self._in_definitions_section = looks_like_definitions_section(heading)
        node = {
            "type": level, "number": number, "heading": heading, "text": "",
            "page_start": line.page_no, "page_end": line.page_no,
            "char_start": char_start, "char_end": char_start, "source": "rules",
        }
        self.nodes.append(node)
        self.stack.append(node)
        self.stack_x0.append(line.x0)
        return node

    def _close_marked_block(self) -> None:
        if self.current_marked_block is not None:
            self.current_marked_block["text"] = self.current_marked_block["text"].strip()
            self.current_marked_block = None

    def _flush_asterisk_run(self, char_end: int) -> None:
        """3+ asterisks on their own is Victoria's standard convention for
        "a provision used to be here and was repealed" -- distinct from an
        actual footnote/margin note (type "note"), so it gets its own type
        rather than being lumped in as one: a reviewer (or an export)
        wants to treat "the parser wasn't sure what this text was" and
        "this was deliberately, formally repealed" very differently, and
        conflating them under "note" made that impossible to tell apart
        downstream. Never carries a number or heading of its own -- like a
        genuine note, it isn't itself a numbered provision (see
        _try_bracket_item's own subsection/paragraph handling for that);
        unlike a note, its text is always exactly this asterisk marker,
        never free text a human or a margin annotation wrote."""
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
            # Too short a run to be the repealed-text marker -- don't lose it,
            # fold it into whatever's currently open instead.
            for l in run:
                if not self.stack:
                    self._open_node(self._preamble_level, None, "Preliminary", l, l.y0)
                _append_text(self.stack[-1], l.text.strip(), l, char_end)
        run.clear()

    def _resolve_hanging_list(self, x0: float) -> None:
        """A common legislative construct opens a subsection (or section)
        with lead-in text, breaks into a lettered/roman list, and then
        closes the list with independent text that grammatically resumes
        the *lead-in's* sentence, not the list item's -- "(a) does X; or
        (b) does Y -- is guilty of an offence." A purely textual parse has
        no way to see that; but the PDF's own hanging indent does: each
        level's own wrapped continuation lines print at a fixed indent past
        that level's opening marker, so a plain continuation line that
        outdents back past the innermost list item's own indent is
        resuming whatever shallower level actually sits at that indent, not
        continuing the list item. Only ever pops subsection/paragraph/
        subparagraph -- Part/Division/Subdivision/Section only ever close
        via an explicit pattern match."""
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
                or self._try_definition_start(line, text, char_start, char_end)
                or self._try_bracket_item(line, text, char_start, char_end, was_heading_group)
                or self._try_bold_emphasis(line, text, char_start, char_end, next_text)
            ):
                self._consume_as_continuation(line, text, char_start, char_end)

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
        block's own prose -- either it matches one of the ordinary
        structural patterns (_looks_like_boundary: a new Part/Division/
        .../paragraph), or -- a case _looks_like_boundary has no way to
        see, since it works off text patterns alone with no font
        information -- it's a fresh defined term opening inside a
        Definitions section (see _try_definition_start): nothing about a
        definition's own shape is pattern-matchable, only its
        typesetting."""
        return _looks_like_boundary(text, self.patterns) or bool(self._in_definitions_section and line.leading_bold_italic)

    def _handle_marked_block(self, line: BodyLine, text: str, char_start: int, char_end: int) -> bool:
        """Inside a "Notes" (or singular "Note") or "Example" block --
        self.marked_block_type says which; both share this same state
        machine, since an Example is set exactly like a singular Note,
        just under a different marker word (see basic-structure.yaml).
        Returns True if the line belongs to the open block (caller skips
        to the next line); returns False -- having also closed the block
        -- when it's ended and the line needs normal classification
        instead."""
        kind = self.marked_block_type
        # Only a "Notes" block's own numbered items ("1 ...", "2 ...")
        # need the note_item pattern -- an Example is never numbered this
        # way (no evidence of "Example 1"/"Example 2" in this drafting
        # convention, only ever a single unnumbered block per callout).
        m = self.patterns["note_item"].match(text) if kind == "note" else None
        # A hanging-indent note number ("1") can land as its own line,
        # separate from its text, if the PDF laid it out with a tab stop
        # rather than inline -- don't let that split fool us into thinking
        # the notes block ended. Gated on the line not being bold: a note
        # item is always set in plain body text, so a *bold* line with
        # this same "digit(s) then text" shape is actually the next
        # section/clause heading immediately following the Notes block
        # (e.g. "38 Requirements for informant's statement in..."), not
        # a new note -- falls through to the boundary check below, which
        # ends the block and lets it be reclassified normally.
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
            # A singular, unnumbered block (as opposed to "Notes" with its
            # own numbered "1 ...", "2 ..." items) -- the drafting
            # convention for one explanatory remark or worked example
            # under a single provision. Without this, its own first line
            # matches neither branch above (no leading number to open a
            # numbered item with) and immediately fell through to ending
            # the block, silently gluing it onto whatever text was
            # already open instead of ever becoming its own node.
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
        """Chapter/Part/Division/Section headings, then the two bare-number
        wrap cases (Subdivision, Section) -- all bold-only."""
        if not line.bold:
            return False

        for level in self._prefix_heading_levels:
            m = self.patterns[level].match(text)
            if not m:
                continue
            if level == "section" and not _is_fresh_start(self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels) or was_heading_group):
                # A bold line that happens to start with a bare number
                # mid-paragraph, not a genuine new section/clause -- e.g.
                # an Act-name citation that wraps its year onto its own
                # line ("... Act\n1997 insert—", the "1997" being the
                # citation's year, not a section number). A real
                # section/clause always opens right after the previous
                # one's body reached a clean sentence break (or right
                # after a Part/Division/heading line), same freshness
                # test already used for a numbered Subdivision below. Only
                # "section" needs this: the other prefix levels (Chapter/
                # Part/Division/...) require a literal keyword ("Chapter
                # "/"Part "/...), which body text is never going to spell
                # out mid-paragraph the way a bare citation year can.
                continue
            node_type = self.top_level_type if level == "section" else level
            self._open_node(node_type, m.group(1), m.group(2).strip(), line, char_start)
            return True

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

        # A bare section number with nothing else on the line -- the
        # heading wraps onto the next bold line instead (same wrap
        # pattern as the bare Subdivision case above), e.g. "465AAAA"
        # alone followed by "Police may use assistants and equipment"
        # as a separate bold line.
        if re.match(r"^\d+[A-Za-z]*$", text) and _is_fresh_start(self.prev_text, _prev_line_was_heading(self.stack, self.heading_levels) or was_heading_group):
            self._open_node(self.top_level_type, text, None, line, char_start)
            return True

        return False

    def _try_schedule_hangs_off(self, line: BodyLine, text: str, char_end: int) -> bool:
        """Immediately under a fresh Schedule heading, a standalone plain
        (non-bold) line like "Sections 6(3), 159(3)" or "Section 5" names
        which section(s) the Schedule "hangs off" (see basic-structure.
        yaml's own note on this) -- not itself part of the heading, but
        not the Schedule's own substantive content either, so it's folded
        into the heading (in parens) rather than either. Scoped tightly
        (only right after a Schedule heading with no body text yet) so an
        ordinary cross-reference sentence elsewhere that happens to start
        the same way is never mistaken for one.

        Folding it into the heading rather than appending it as body text
        also matters for a reason beyond just "where does this belong":
        appending it to the Schedule's own `text` would make that text
        non-empty, and _is_fresh_start's own "did the previous line leave
        us inside an empty heading" check (_prev_line_was_heading) reads
        exactly that emptiness to decide whether the Schedule's own first
        numbered item right after this line is a genuine fresh section
        start -- getting that wrong would silently glue the Schedule's
        first real clause onto this line as plain continuation text
        instead of giving it its own node."""
        top = self.stack[-1] if self.stack else None
        if top is None or top["type"] != "schedule" or top["text"] or line.bold:
            return False
        if not _SCHEDULE_HANGS_OFF_RE.match(text):
            return False
        _append_heading(top, f"({text})", char_end)
        return True

    def _try_definition_start(self, line: BodyLine, text: str, char_start: int, char_end: int) -> bool:
        """Inside a Definitions/Interpretation section, a defined term is
        reliably set bold+italic where it's introduced ("accused means a
        person who—") -- distinctly different typesetting from the
        ordinary bold-only emphasis used elsewhere (Act-name citations)
        and the italic-only case citations that also appear in body text,
        so line.leading_bold_italic (see extract.py) is a much stronger
        signal here than the text-pattern heuristics definitions.py falls
        back to for cross-linking. Splitting each one into its own
        "definition" node -- nested at the same rank a numbered
        subsection would be (see hierarchy.py's make_ranks), so a
        definition's own (a)/(b) list still nests correctly under it --
        rather than leaving a whole run of definitions concatenated into
        one Section's single, unbroken block of text lets a human
        reviewer actually see and work through each term individually,
        the same way review.py already lets them work through a
        Section's own numbered pieces one at a time.

        Gated on self._in_definitions_section so an incidental bold+
        italic run elsewhere (there isn't a known case of one, but
        nothing rules one out) can never be mistaken for a defined term
        outside a section that's actually introducing them."""
        if not (self._in_definitions_section and line.leading_bold_italic):
            return False
        term = line.leading_bold_italic
        if not text.startswith(term):
            # Shouldn't happen (term is built from this same line's own
            # leading spans -- see extract.py's _leading_bold_italic), but
            # if some edge case ever misaligns them, falling through to
            # ordinary continuation handling is safe; slicing on a
            # mismatched prefix here would not be.
            return False
        remainder = text[len(term) :].strip()

        top = self.stack[-1] if self.stack else None
        if top is not None and top["type"] == "definition" and not top["text"]:
            # The currently-open definition's own term wrapped onto this
            # second physical line (a long one, e.g. "indictable offence
            # that may be heard and" / "determined summarily means an
            # offence to..."), rather than this line starting a genuinely
            # new definition -- its own text is still empty, meaning the
            # previous line was consumed entirely as heading with nothing
            # left over. Extend the heading instead, the same way a
            # wrapped Part/Division/Section heading already does (see
            # _try_bold_emphasis's own heading-wrap branch) -- a
            # definition just has no stack-level "no body text yet" flag
            # of its own the way _prev_line_was_heading checks for those,
            # so this checks it directly.
            _append_heading(top, term, char_end)
            if remainder:
                _append_text(top, remainder, line, char_end)
            return True

        self._open_node("definition", None, term, line, char_start)
        if remainder:
            _append_text(self.stack[-1], remainder, line, char_end)
        return True

    def _try_bracket_item(self, line: BodyLine, text: str, char_start: int, char_end: int, was_heading_group: bool) -> bool:
        """Bracket items (subsection/paragraph/subparagraph/sub_subparagraph)
        are classified by content shape + sequence context, not boldness --
        Act-name citations are commonly bolded throughout these Acts."""
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
                    # those two (see _bracket_level), there's no sequence-
                    # continuity ambiguity to resolve here.
                    bracket_match, level = m3, "sub_subparagraph"
        if not (level and bracket_match):
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
            # "Non-fatal strangulation" as two lines. A genuine subsection
            # number is never bold on its own, and a genuine Subdivision
            # only opens right after a clean sentence break -- guards
            # against a bold bracket that's actually a pinpoint citation
            # ("section 9A(1A) or\n(1B) of ...") wrapping mid-sentence
            # instead.
            level = "subdivision"
        self._open_node(level, bracket_match.group(1), None, line, char_start)
        if remainder:
            _append_text(self.stack[-1], remainder, line, char_end)
        return True

    def _try_bold_emphasis(self, line: BodyLine, text: str, char_start: int, char_end: int, next_text: str) -> bool:
        """Any remaining bold line: a wrapped heading tail, a bare topical
        heading_group, or plain inline emphasis folded into the open node.
        A bold line always ends here -- this never returns False for one."""
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
            # ("Theft, robbery, burglary, &c."), distinguishable from
            # inline bold emphasis only by shape -- see
            # _looks_like_group_heading. It always sits between two
            # sections, never inside one, but tree reconstruction
            # (build_hierarchy_tree in akn_export.py, shared by both
            # exporters) replays node *types* from this flat list
            # independently of this function's own stack -- so that's
            # where attachment gets corrected, not here.
            self.nodes.append({
                "type": "heading_group", "number": None, "heading": text, "text": text,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            })
            self.prev_was_heading_group = True
        else:
            # Bold at body size with no structural pattern -- inline
            # emphasis (e.g. a defined term or, as above, an Act-name
            # citation), not a boundary.
            if not self.stack:
                self._open_node(self._preamble_level, None, "Preliminary", line, char_start)
            _append_text(self.stack[-1], text, line, char_end)
        return True

    def _consume_as_continuation(self, line: BodyLine, text: str, char_start: int, char_end: int) -> None:
        """Continuation of whatever is currently open. If nothing is open
        yet (preamble text before the first Part), open a synthetic holder
        rather than dropping it."""
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
    skip_front_matter: bool = False,
) -> ParseResult:
    """top_level_type: "section" for an enacted Act (the default), "clause"
    to parse a Bill instead -- same drafting shape and the same profile
    patterns apply either way (see _LineParser's own docstring on this
    parameter), just the resulting node type differs.

    skip_front_matter: True for a Bill (see _skip_bill_front_matter) --
    an enacted Act's PDF has nothing equivalent to skip, so this defaults
    to False and leaves Act parsing completely unaffected. The skipped
    lines are recorded as a warning (not silently dropped) so the
    completeness count in the result stays an honest, auditable reflection
    of what was deliberately excluded and why -- see the module
    docstring's own completeness guarantee."""
    patterns = load_profile(profile_name)
    hierarchy_order = load_hierarchy(profile_name)
    lines = _flatten_lines(pages)
    warnings: list[str] = []
    if skip_front_matter:
        remaining = _skip_bill_front_matter(lines)
        skipped = len(lines) - len(remaining)
        if skipped:
            warnings.append(f"skipped {skipped} front-matter line(s) (title page + Table of Provisions) before the enacting words")
        lines = remaining
    parser = _LineParser(patterns, _body_font_size(lines), hierarchy_order, top_level_type=top_level_type)
    parser.feed(lines)
    result = parser.result()
    result.warnings = warnings + result.warnings
    return result
