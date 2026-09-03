"""
Rule-based structural parser for a Bill's Explanatory Memorandum (EM) --
a much flatter document than an Act or Bill. Instead of nested Part/
Division/Section(/clause), an EM is a sequence of per-clause
explanations ("Clause 5 sets out that ...", "Clause 6(4) provides that
..."), interspersed with purely organisational Chapter/Part headers that
group the clauses under them for a reader's benefit without changing the
underlying flat sequence -- an EM Chapter/Part heading has no explanatory
text of its own, unlike the Bill/Act's own Part/Division, which do.

Each entry is typed "clause" -- the same type the Bill's own provisions
get (see run_pipeline.py's top_level_type) -- because that is what an
entry is about and is numbered by. It used to get a type of its own,
"em_entry", which made every EM node an unranked type: hierarchy.py had
no rank for it, so grouping, nesting, indentation and the browse view all
treated a whole EM as a flat run of unrelated fragments. "clause" is
aliased onto "section"'s rank (see hierarchy.make_ranks), so an entry now
sits under the Chapter/Part heading above it and reads as a provision.

Detection is almost entirely textual rather than font-based, unlike
rule_parser.py's Act/Bill parser: "Clause N" (optionally with a pinpoint,
"Clause 6(4)") is set in plain body text, not bold -- the only reliable
signal is that a genuine new entry's line starts with the literal word
"Clause", never mid-sentence ("... the accused.  Clause 3 defines direct
indictment." is a continuation, not a new entry, and is correctly left
alone because that line doesn't *start* with "Clause"). Chapter/Part
headers keep the same bold-and-pattern-matched detection convention as
rule_parser.py, since they're set the same way here.

A "Clause N(4)"-style pinpoint reference gets its own entry rather than
being folded into the base clause's own entry: both readings are
defensible (it's still explaining clause 4 as a whole), but treating each
distinctly-worded explanation as its own entry is simpler, loses no
information, and -- crucially -- doesn't require guessing where the base
clause's own explanation ends and the pinpoint's begins. Normalising
"6(4)" down to base clause "6" for matching against the Bill's own clause
numbers is bill_linking.py's job at link-resolution time, not this
module's.

A genuine entry-opening line and an incidental cross-reference are the
*same* shape, though, which "starts with Clause" alone can't tell apart:
"Clause 384 comes into operation on 1 July 2010" reads exactly like a
real entry, but can just as easily be one sentence inside clause 2's own
commencement note, discussing when a much later clause takes effect. The
OCPC's own drafting guide requires a note for *every* clause of the Bill,
so a genuine new entry's number is essentially always the very next one
in sequence (or the first entry after a Chapter/Part/Schedule heading,
where numbering can legitimately jump or restart) -- a huge jump forward
with no such heading in between is a cross-reference, not a new entry,
and is left as continuation text of whatever's currently open instead
(see _is_plausible_next_clause).
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from .extract import BodyLine, PageText

_CHAPTER_RE = re.compile(r"^Chapter\s+(\d+[A-Za-z]*)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_PART_RE = re.compile(r"^Part\s+([\dA-Za-z.]+)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_SCHEDULE_RE = re.compile(r"^Schedule\s+(\d+[A-Za-z]*)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"^Clause\s+(\d+[A-Za-z]*(?:\(\w+\))?)\s*(.*)$")

# How far forward a clause number may plausibly jump between one
# confirmed entry and the next real one, absent an intervening Chapter/
# Part/Schedule heading -- generous enough for the occasional skipped or
# consolidated clause, small enough that a jump of dozens (a cross-
# reference to a much later clause, not that clause's own note) still
# gets caught. See _is_plausible_next_clause.
_MAX_FORWARD_GAP = 30


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


def parse_em(pages: list[PageText]) -> EMParseResult:
    lines = _flatten_lines(pages)
    warnings: list[str] = []

    # An EM's own title block ("Criminal Procedure Bill 2008" / "Introduction
    # Print" / "EXPLANATORY MEMORANDUM") sits once, on the same page as the
    # very first real content -- skip up to (not including) whichever
    # comes first, a genuine Chapter/Part header or a "Clause N" line
    # (some EMs open straight on Clause 1 with no Chapter 1 heading of its
    # own -- see the real sample this was built against), same reasoning
    # as rule_parser.py's _skip_bill_front_matter for the Bill's own Table
    # of Provisions.
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

    nodes: list[dict] = []
    current: dict | None = None
    open_heading: dict | None = None
    last_base: int | None = None
    cursor = 0
    lines_consumed = 0

    def close_current():
        if current is not None:
            current["text"] = current["text"].strip()

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
            # jump with no heading in between -- a cross-reference inside
            # the currently-open entry's own text, not a new entry (see
            # the module docstring). Treated as an ordinary non-match so
            # it falls through to the continuation branch below, keeping
            # the full line -- "Clause 384" included -- as part of the
            # flowing sentence it actually belongs to.
            clause_m = None

        if heading_m:
            close_current()
            current = None
            last_base = None
            open_heading = {
                "type": "heading_group", "number": None, "heading": text, "text": text,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            nodes.append(open_heading)
        elif clause_m:
            close_current()
            open_heading = None
            number, rest = clause_m.group(1), clause_m.group(2).strip()
            last_base = _base_number(number)
            current = {
                "type": "clause", "number": number, "heading": None, "text": rest,
                "page_start": line.page_no, "page_end": line.page_no,
                "char_start": char_start, "char_end": char_end, "source": "rules",
            }
            nodes.append(current)
        elif line.bold and open_heading is not None and current is None:
            # A Chapter/Part title that wrapped onto a second bold line
            # (e.g. "CHAPTER 2—COMMENCING A CRIMINAL" / "PROCEEDING") --
            # extend the heading in progress, not a new entry's text. Only
            # applies with no entry open yet (current is None): once a
            # clause entry has started, a later bold line is inline
            # emphasis within its explanation, not a heading continuation.
            open_heading["heading"] = f"{open_heading['heading']} {text}"
            open_heading["text"] = open_heading["heading"]
            open_heading["char_end"] = char_end
        else:
            if current is None:
                # Shouldn't happen -- the front-matter skip above already
                # advances past everything before the first "Clause N"
                # line -- but if some other layout puts real text before
                # the first entry anyway, file it under a synthetic holder
                # rather than dropping it (see rule_parser.py's own
                # synthetic-preamble fallback for the same reasoning).
                current = {
                    "type": "clause", "number": None, "heading": None, "text": "",
                    "page_start": line.page_no, "page_end": line.page_no,
                    "char_start": char_start, "char_end": char_start, "source": "rules",
                }
                nodes.append(current)
                warnings.append(f"page {line.page_no}: text before any recognised clause entry -- filed under a synthetic entry")
            current["text"] = (current["text"] + "\n" + text) if current["text"] else text
            current["page_end"] = line.page_no
            current["char_end"] = char_end

    close_current()
    return EMParseResult(nodes=nodes, lines_total=len(lines), lines_consumed=lines_consumed, warnings=warnings)
