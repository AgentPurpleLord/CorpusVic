"""
Rule-based structural parser for a Bill's Explanatory Memorandum (EM) --
a much flatter document than an Act or Bill. Instead of nested Part/
Division/Section(/clause), an EM is a sequence of per-clause
explanations ("Clause 5 sets out that ...", "Clause 6(4) provides that
..."), interspersed with purely organisational Chapter/Part headers that
group the clauses under them for a reader's benefit without changing the
underlying flat sequence -- an EM Chapter/Part heading has no explanatory
text of its own, unlike the Bill/Act's own Part/Division, which do.

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
"""
import re
from collections import Counter
from dataclasses import dataclass, field

from .extract import BodyLine, PageText

_CHAPTER_RE = re.compile(r"^Chapter\s+(\d+[A-Za-z]*)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_PART_RE = re.compile(r"^Part\s+([\dA-Za-z.]+)\s*[—–-]\s*(.+)$", re.IGNORECASE)
_CLAUSE_RE = re.compile(r"^Clause\s+(\d+[A-Za-z]*(?:\(\w+\))?)\s*(.*)$")


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
        if _CLAUSE_RE.match(text) or ((_CHAPTER_RE.match(text) or _PART_RE.match(text)) and line.bold):
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

        heading_m = (_CHAPTER_RE.match(text) or _PART_RE.match(text)) if line.bold else None
        clause_m = _CLAUSE_RE.match(text)

        if heading_m:
            close_current()
            current = None
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
            current = {
                "type": "em_entry", "number": number, "heading": None, "text": rest,
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
                    "type": "em_entry", "number": None, "heading": None, "text": "",
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
