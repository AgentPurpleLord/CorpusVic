"""
A rule-based structural parser for a Bill's Explanatory Memorandum (EM)
-- a much flatter document than an Act or Bill. Instead of nested Part,
Division, Section and clause, an EM is a sequence of per-clause
explanations ("Clause 5 sets out that ...", "Clause 6(4) provides that
..."), interspersed with purely organisational Chapter and Part headers
that group the clauses under them for a reader's benefit, without
changing the underlying flat sequence -- an EM's Chapter/Part heading
has no explanatory text of its own, unlike the Bill's or Act's own Part
or Division, which do.

Each entry is typed "clause" -- the same type the Bill's own provisions
get (see run_pipeline.py's top_level_type) -- because that's what an
entry is about, and what it's numbered by. It used to have its own
type, "em_entry", which made every EM node a type with no place in the
hierarchy: hierarchy.py had no depth defined for it, so grouping,
nesting, indentation and the browse view all treated a whole EM as a
flat run of unrelated fragments. "clause" is now treated as the same
depth as "section" (see hierarchy.make_ranks), so an entry sits under
the Chapter/Part heading above it and reads as a real provision.

Detection relies almost entirely on the text, not the font, unlike
rule_parser.py's Act/Bill parser: "Clause N" (optionally with a
pinpoint, "Clause 6(4)") is set in plain body text, not bold -- the
only reliable signal is that a genuine new entry's line starts with the
literal word "Clause", never mid-sentence ("... the accused.  Clause 3
defines direct indictment." is a continuation, not a new entry, and is
correctly left alone because that line doesn't *start* with "Clause").
Chapter and Part headers keep the same bold-and-pattern-matched
detection as rule_parser.py, since they're set the same way here.

A "Clause N(4)"-style pinpoint reference gets its own entry, rather
than being folded into the base clause's own entry: both readings are
defensible (it's still explaining clause 4 as a whole), but treating
each distinctly-worded explanation as its own entry is simpler, loses
no information, and -- crucially -- doesn't require guessing where the
base clause's explanation ends and the pinpoint's begins. Reducing
"6(4)" down to base clause "6" to match it against the Bill's own
clause numbers is bill_linking.py's job at link-resolution time, not
this module's.

A genuine entry-opening line and a passing cross-reference look
exactly the same, though, so "starts with Clause" alone can't tell
them apart: "Clause 384 comes into operation on 1 July 2010" reads
exactly like a real entry, but could just as easily be one sentence
inside clause 2's own commencement note, mentioning when a much later
clause takes effect. The OCPC's own drafting guide requires a note for
*every* clause of the Bill, so a genuine new entry's number is
essentially always the very next one in sequence (or the first entry
after a Chapter, Part or Schedule heading, where numbering can
legitimately jump or restart) -- a big jump forward with no such
heading in between is a cross-reference, not a new entry, and is left
as continuation text of whatever's currently open instead (see
_is_plausible_next_clause).
"""
import re
from dataclasses import dataclass, field

from corpus.parsing.extract import BodyLine, PageText, join_printed_line

_CHAPTER_RE = re.compile(r"^Chapter\s+(\d+[A-Za-z]*)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_PART_RE = re.compile(r"^Part\s+([\dA-Za-z.]+)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_SCHEDULE_RE = re.compile(r"^Schedule\s+(\d+[A-Za-z]*)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"^Clause\s+(\d+[A-Za-z]*(?:\(\w+\))?)\s*(.*)$")

# How far forward a clause number can plausibly jump between one
# confirmed entry and the next real one, with no Chapter, Part or
# Schedule heading in between -- generous enough for the occasional
# skipped or consolidated clause, small enough that a jump of dozens (a
# cross-reference to a much later clause, not that clause's own note)
# still gets caught. See _is_plausible_next_clause.
_MAX_FORWARD_GAP = 30

# An EM sets a bulleted list with the marker alone on its own extracted
# line and the item's text beside it at a deeper indent, so the marker
# never arrives attached to what it introduces.
_BULLET_RE = re.compile(r"^[\u2022\u00b7\u25cf\u25e6\u2023]$")

# Two indents count as the same column within this many points. Measured
# off a real EM: its body text sits at x0 195.5, its first-level list
# items at 226.8 and second-level ones at 255.1, so the columns are
# about 28pt apart and this is nowhere near ambiguous.
_INDENT_TOLERANCE = 6.0

# What a bulleted item becomes, by nesting depth. An EM's lists are the
# same kind of thing an Act's own paragraph lists are, so they get the
# same types: they then nest, indent, group into review units, and
# export to Markdown and AKN with no special handling needed anywhere
# downstream. Nothing deeper than three levels has ever been seen;
# anything beyond that stays at the innermost type rather than being
# dropped.
_LIST_TYPES = ("paragraph", "subparagraph", "sub_subparagraph")


def _base_number(number: str | None) -> int | None:
    m = re.match(r"\d+", number or "")
    return int(m.group(0)) if m else None


def _is_plausible_next_clause(candidate_number: str, last_base: int | None, current: dict | None) -> bool:
    candidate_base = _base_number(candidate_number)
    if last_base is None:
        return True  # first entry, or the first since a heading reset -- anything is plausible
    if current is not None and candidate_base is not None and candidate_base == _base_number(current.get("number")):
        return True  # a pinpoint continuing the currently-open entry's own base clause, e.g. "6" then "6(4)"
    return candidate_base is not None and 0 <= candidate_base - last_base <= _MAX_FORWARD_GAP


@dataclass
class EMParseResult:
    nodes: list[dict]
    lines_total: int
    lines_consumed: int
    warnings: list[str] = field(default_factory=list)


def _flatten_lines(pages: list[PageText]) -> list[BodyLine]:
    lines = []
    for page in pages:
        lines.extend(page.body_lines)
    return lines


# How far short of the margin a line has to fall to read as the last of
# its paragraph. A couple of characters' worth: justification leaves a
# little slack even on a full line.
_PARAGRAPH_SHORT_LINE = 12.0


def _paragraph_starts(lines: list[BodyLine]) -> set[int]:
    """Which lines begin a new paragraph, by the space above them.

    Two things have to agree, because either alone is wrong often enough
    to matter.

    Vertically, the typesetter left more room above this line than
    between the lines within a paragraph. The ordinary pitch is measured
    from the document itself -- the median step between consecutive lines
    on a page -- rather than assumed, because the two EMs here are set at
    different sizes. In the Criminal Procedure Bill's EM that pitch is
    about 12pt, with a clear second cluster around 18-21pt where the
    paragraphs break.

    Horizontally, the line above ends short of the margin. The text is
    justified, so every line of a paragraph but its last is pushed out to
    the full measure; a short line is the end of something. On its own
    the vertical test called 91 wrapped lines paragraph breaks in that
    same EM -- splitting sentences mid-clause -- and every one of them
    followed a line that ran the full width."""
    steps = [
        b.y0 - a.y0
        for a, b in zip(lines, lines[1:])
        if a.page_no == b.page_no and b.y0 > a.y0
    ]
    if not steps:
        return set()
    pitch = sorted(steps)[len(steps) // 2]
    if pitch <= 0:
        return set()
    # The right-hand edge of the text block, read off the lines that
    # reach it rather than from any page geometry.
    edges = sorted(line.x1 for line in lines)
    margin = edges[int(len(edges) * 0.9)]
    return {
        i + 1
        for i, (a, b) in enumerate(zip(lines, lines[1:]))
        if a.page_no == b.page_no
        and (b.y0 - a.y0) > pitch * 1.4
        and a.x1 < margin - _PARAGRAPH_SHORT_LINE
    }


def parse_em(pages: list[PageText]) -> EMParseResult:
    lines = _flatten_lines(pages)
    warnings: list[str] = []

    # An EM's own title block ("Criminal Procedure Bill 2008" /
    # "Introduction Print" / "EXPLANATORY MEMORANDUM") sits once, on the
    # same page as the very first real content -- skip up to (not
    # including) whichever comes first, a genuine Chapter/Part header or
    # a "Clause N" line (some EMs open straight on Clause 1 with no
    # Chapter 1 heading of its own -- see the real sample this was built
    # against), the same reasoning as rule_parser.py's
    # _skip_bill_front_matter for the Bill's own Table of Provisions.
    start = 0
    for i, line in enumerate(lines):
        text = line.text.strip()
        if _CLAUSE_RE.match(text) or ((_CHAPTER_RE.match(text) or _PART_RE.match(text) or _SCHEDULE_RE.match(text)) and line.bold):
            start = i
            break
    else:
        warnings.append("no \"Clause N\" entry or Chapter/Part header found anywhere in the document -- nothing parsed")
    if start:
        warnings.append(f"skipped {start} front-matter line(s) (title block) before the first entry")
    lines = lines[start:]
    # After the slice, not before it: the loop below indexes into this
    # list, and a set built against the unsliced one would mark the line
    # `start` positions further on.
    paragraph_starts = _paragraph_starts(lines)

    nodes: list[dict] = []
    current: dict | None = None
    open_heading: dict | None = None
    open_schedule: str | None = None
    last_base: int | None = None
    cursor = 0
    lines_consumed = 0

    # One open bulleted list, as (item text indent, node) per nesting
    # level. An item's own indent is read off the line *after* its
    # marker, since the marker arrives alone (see _BULLET_RE) -- and
    # it's the item text's column, not the marker's, that separates the
    # levels: the marker of a first-level item sits within 3pt of the
    # body column it interrupts, while its text sits a clear 30pt to
    # the right of it.
    item_stack: list[tuple[float, dict]] = []
    awaiting_item_text = False
    # Where plain continuation text goes. The clause's own node, until
    # a list opens under it; after that the clause's text is closed, so
    # prose resuming at the body column starts a fresh node instead of
    # being appended back onto a paragraph that now prints above the
    # list.
    sink: dict | None = None

    def close_current():
        if current is not None:
            current["text"] = current["text"].strip()

    def start_list_item(line, text: str, char_start: int, char_end: int) -> dict:
        """Opens one bulleted item at the nesting level its indent
        implies: deeper than the level above starts a new one, back at
        an outer level's column closes everything inside it."""
        while item_stack and line.x0 < item_stack[-1][0] - _INDENT_TOLERANCE:
            item_stack.pop()
        if item_stack and line.x0 <= item_stack[-1][0] + _INDENT_TOLERANCE:
            item_stack.pop()  # a sibling of the item that just closed, not a child
        node = {
            "type": _LIST_TYPES[min(len(item_stack), len(_LIST_TYPES) - 1)],
            "number": None, "heading": None, "text": text,
            "page_start": line.page_no, "page_end": line.page_no,
            "char_start": char_start, "char_end": char_end, "source": "rules",
        }
        item_stack.append((line.x0, node))
        nodes.append(node)
        return node

    def _continues_list_item(line, stack: list) -> bool:
        """Whether this line carries on the item it follows. A line back
        at an outer level's own column closes everything nested inside
        it and continues *that* item instead -- "... described as
        being—" then its own sub-list, then more of the same item."""
        while stack and line.x0 < stack[-1][0] - _INDENT_TOLERANCE:
            stack.pop()
        return bool(stack) and line.x0 <= stack[-1][0] + _INDENT_TOLERANCE

    def extend(node: dict, text: str, line, char_end: int, new_paragraph: bool = False) -> None:
        """One more printed line -- see extract.join_printed_line for what
        happens to the break, and paragraph_starts for the one kind of
        break an Explanatory Memorandum keeps.

        An EM is prose, not provisions: a clause note runs to several
        paragraphs, and where one ends is carried only by the space the
        typesetter left above the next. Joining every line without that
        left each note as one undifferentiated block; keeping every line
        break left it looking like verse."""
        if new_paragraph and node["text"]:
            node["text"] += "\n" + text
        else:
            node["text"] = join_printed_line(node["text"], text)
        node["page_end"] = line.page_no
        node["char_end"] = char_end

    for idx, line in enumerate(lines):
        text = line.text.strip()
        char_start = cursor
        char_end = cursor + len(text)
        cursor = char_end + 1
        lines_consumed += 1
        if not text:
            continue

        heading_m = (_CHAPTER_RE.match(text) or _PART_RE.match(text) or _SCHEDULE_RE.match(text)) if line.bold else None
        clause_m = _CLAUSE_RE.match(text)
        if clause_m and not _is_plausible_next_clause(clause_m.group(1), last_base, current):
            # Same shape as a genuine entry opener, but an implausible
            # jump with no heading in between -- a cross-reference
            # inside the currently-open entry's own text, not a new
            # entry (see the module docstring). Treated as an ordinary
            # non-match so it falls through to the continuation branch
            # below, keeping the full line -- "Clause 384" included --
            # as part of the sentence it actually belongs to.
            clause_m = None

        if heading_m:
            close_current()
            current = None
            last_base = None
            item_stack.clear()
            awaiting_item_text = False
            sink = None
            schedule_m = _SCHEDULE_RE.match(text)
            # A Schedule restarts clause numbering from 1, so an EM's
            # "Clause 11" under Schedule 1 and its "Clause 11" in the
            # body are different provisions sharing a number. Recording
            # which Schedule an entry sits under is what lets a reader
            # (and the cross-reference chips on an Act section) tell
            # them apart -- see bill_linking and dashboard's
            # _section_crossrefs.
            open_schedule = schedule_m.group(1) if schedule_m else None
            open_heading = {
                "type": "heading_group", "number": None, "heading": text, "text": text,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            if open_schedule:
                open_heading["schedule"] = open_schedule
            nodes.append(open_heading)
        elif clause_m:
            close_current()
            open_heading = None
            item_stack.clear()
            awaiting_item_text = False
            number, rest = clause_m.group(1), clause_m.group(2).strip()
            last_base = _base_number(number)
            current = {
                "type": "clause", "number": number, "heading": None, "text": rest,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            if open_schedule:
                current["schedule"] = open_schedule
            nodes.append(current)
            sink = current
        elif line.bold and open_heading is not None and current is None:
            # A Chapter/Part title that wrapped onto a second bold line
            # (e.g. "CHAPTER 2—COMMENCING A CRIMINAL" / "PROCEEDING") --
            # extend the heading in progress, not a new entry's text.
            # Only applies with no entry open yet (current is None):
            # once a clause entry has started, a later bold line is
            # inline emphasis within its explanation, not a heading
            # continuation.
            open_heading["heading"] = f"{open_heading['heading']} {text}"
            open_heading["text"] = open_heading["heading"]
            open_heading["char_end"] = char_end
        elif _BULLET_RE.match(text):
            # The marker alone. Which level the item belongs to depends
            # on its *text's* indent, which is on the next line -- so
            # nothing is decided until that arrives.
            awaiting_item_text = True
        elif awaiting_item_text:
            start_list_item(line, text, char_start, char_end)
            awaiting_item_text = False
            sink = None  # the clause's text is closed once a list opens under it
        elif item_stack and _continues_list_item(line, item_stack):
            extend(item_stack[-1][1], text, line, char_end, idx in paragraph_starts)
        else:
            # Back at the body column: whatever list was open ends
            # here.
            item_stack.clear()
            if current is None:
                # Shouldn't happen -- the front-matter skip above
                # already advances past everything before the first
                # "Clause N" line -- but if some other layout puts real
                # text before the first entry anyway, file it under a
                # synthetic holder rather than dropping it (see
                # rule_parser.py's own synthetic-preamble fallback for
                # the same reasoning).
                current = {
                    "type": "clause", "number": None, "heading": None, "text": "",
                    "page_start": line.page_no, "page_end": line.page_no,
                    "char_start": char_start, "char_end": char_start, "source": "rules",
                }
                nodes.append(current)
                sink = current
                warnings.append(f"page {line.page_no}: text before any recognised clause entry -- filed under a synthetic entry")
            if sink is None:
                # Prose resuming after a list. It belongs to this entry
                # but comes *after* its items, and the clause's own
                # text prints above them -- so it gets a node of its
                # own rather than being folded back into text that
                # would then read out of order. "note" is the type an
                # Act's own parser gives the same shape (trailing
                # commentary under a provision that's just finished a
                # list), so it nests and renders the same way here.
                sink = {
                    "type": "note", "number": None, "heading": None, "text": "",
                    "page_start": line.page_no, "page_end": line.page_no,
                    "char_start": char_start, "char_end": char_start, "source": "rules",
                }
                if open_schedule:
                    sink["schedule"] = open_schedule
                nodes.append(sink)
            extend(sink, text, line, char_end, idx in paragraph_starts)

    close_current()
    return EMParseResult(nodes=nodes, lines_total=len(lines), lines_consumed=lines_consumed, warnings=warnings)
