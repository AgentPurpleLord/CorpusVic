"""
Parses the amendment-history margin notes -- the sidebar text saying when
a section, subsection or definition was inserted, amended, substituted or
repealed, and by which Act -- and works out which provision each one is
about.

This is done with plain pattern matching on the note's own opening words,
since the notes already say what they amend ("S. 3(2) inserted by...",
"Pt 1 Div. 1 Subdiv. (4) ..."). No AI model is needed here. On the Crimes
Act this covers about 98% of notes; anything that doesn't match a known
pattern is kept as an unattached note rather than thrown away, so it can
still be linked by hand during review.
"""
import re

from .extract import PageText

# A margin note can wrap across more than one extracted text block on the
# same page; a fragment that doesn't start with a recognisable citation is a
# continuation of the previous note, not a new one.
_CITATION_START_RE = re.compile(
    r"^("
    r"Notes?\s*[\w]*\s*to\s+s\.|Examples?\s+to\s+s\.|Heading\s+preceding|Heading\s+(inserted|amended|substituted|repealed)"
    r"|S\.|Ss\b|Pt\s|Ch\.\s|Schs?\.?\s*\d|Dictionary\s"
    # A provenance note ("No. 6103 s. 15.", "cf. [1819] 60 George III")
    # starts a note of its own rather than continuing the one above it --
    # without this they were glued onto whichever amendment note happened
    # to precede them, taking that note's own citation with them.
    r"|Nos?\.?\s*\d|cf\.|See:"
    r"|New\s+(s\.|Pt\b|Ch\.|Sch)"
    r")",
    re.IGNORECASE,
)

_NEW_PREFIX_RE = re.compile(r"^New\s+", re.IGNORECASE)

_CITATION_RE = re.compile(
    r"^(?:Notes?\s*(?P<noteid>[\w]*)\s*to\s+s\.\s*(?P<nsection>\d+[A-Za-z]*)(?P<nsub>(?:\([^)]*\))*))"
    r"|^(?:Examples?\s+to\s+s\.\s*(?P<esection>\d+[A-Za-z]*)(?P<esub>(?:\([^)]*\))*))"
    r"|^(?:Heading\s+preceding\s+s\.\s*(?P<hsection>\d+[A-Za-z]*))"
    # A Schedule, and optionally one clause of it: "Sch. 2 repealed by
    # ...", "Sch. 1 cl. 4A(1) amended by ...". Schedules number their own
    # clauses from 1 again, so the clause only means anything alongside
    # the Schedule it belongs to (see hierarchy.schedule_numbers).
    r"|^(?:Schs?\.?\s*(?P<schedule>\d+[A-Za-z]*)(?:\s*[–-]\s*\d+[A-Za-z]*)?"
    r"(?:\s*(?:cl|item)s?\.?\s*(?P<schclause>\d+[A-Za-z]*)(?P<schsub>(?:\([^)]*\))*))?)"
    # An Act that groups its Parts under Chapters cites both: "Ch. 8
    # Pt 8.2 Div. 5 (Heading) amended by ...". The Part/Division is what
    # identifies the provision; the Chapter is recorded but not needed to
    # find it, since a Part number is unique within the Act.
    r"|^(?:Ch\.\s*(?P<chapter>[\dA-Za-z.]+)"
    r"(?:\s+Pt\.?\s*(?P<cpart>[\dA-Za-z.]+))?(?:\s+Div\.?\s*(?P<cdiv>[\dA-Za-z.]+))?)"
    r"|^(?:Ss?\.?\s*(?P<section>\d+[A-Za-z]*)(?:\s*[–-]\s*\d+[A-Za-z]*)?(?P<sub>(?:\([^)]*\))*))"
    r"|^(?:Pt\s+(?P<part>\S+)\s+Div\.?\s*(?P<div>\S+?)(?:\s+Subdivs?\.?\s*\((?P<subdiv>[^)]*)\))?(?=[\s(]|$))"
    r"|^(?:Pt\s+(?P<part2>\S+)\s*\(Heading)",
    re.IGNORECASE,
)

# A note that records where a provision *came from* rather than how it has
# been amended: the section of the previous consolidation it reproduces
# ("No. 6103 s. 15."), the English statute it derives from ("cf. [1819] 60
# George III ..."), or the Endnotes' own pointer to another Act's reprint
# ("See: Act No. 35/1999. Reprint No. 1 ..."). These name no provision of
# *this* Act, so they can never attach to one -- reporting them as notes
# that "could not be linked" made 29 correctly-handled notes look like
# parser failures.
_PROVENANCE_RE = re.compile(r"^(Nos?\.?\s*\d|cf\.|See:)", re.IGNORECASE)

_DEF_RE = re.compile(r"def\.?\s+of\s+([^,;]+?)(?:\s+(?:inserted|substituted|amended|repealed))", re.IGNORECASE)


def _split_subpath(subpath_str: str | None) -> list[str]:
    return [f"({p})" for p in re.findall(r"\(([^)]*)\)", subpath_str or "")]


def merge_continuations(notes: list[str]) -> list[str]:
    merged: list[str] = []
    for n in notes:
        n = n.strip()
        if not n:
            continue
        if merged and not _CITATION_START_RE.match(n):
            merged[-1] = merged[-1].rstrip() + " " + n
        else:
            merged.append(n)
    return merged


def parse_note(raw: str) -> dict:
    """Returns the note's raw text plus, where parseable, the provision it
    amends: a section number, an optional sub_path (e.g. ["(1)", "(a)"]),
    a schedule/division/part reference, and/or a definition term.

    "kind" is "provenance" for a note that records where a provision came
    from rather than how it has been amended (see _PROVENANCE_RE) --
    those name no provision of this Act and are not expected to attach to
    one; "amendment" for everything else."""
    text = _NEW_PREFIX_RE.sub("", raw.strip())
    result = {
        "raw": raw.strip(),
        "kind": "provenance" if _PROVENANCE_RE.match(text) else "amendment",
        "section": None,
        "sub_path": [],
        "schedule": None,
        "chapter": None,
        "division": None,
        "part": None,
        "def_name": None,
    }
    m = _CITATION_RE.match(text)
    if m:
        gd = m.groupdict()
        if gd.get("nsection"):
            result["section"] = gd["nsection"]
            result["sub_path"] = _split_subpath(gd.get("nsub"))
        elif gd.get("esection"):
            result["section"] = gd["esection"]
            result["sub_path"] = _split_subpath(gd.get("esub"))
        elif gd.get("schedule"):
            result["schedule"] = gd["schedule"]
            if gd.get("schclause"):
                result["section"] = gd["schclause"]
                result["sub_path"] = _split_subpath(gd.get("schsub"))
        elif gd.get("chapter"):
            result["chapter"] = gd["chapter"]
            result["part"] = gd.get("cpart")
            result["division"] = gd.get("cdiv")
        elif gd.get("hsection"):
            result["section"] = gd["hsection"]
        elif gd.get("section"):
            result["section"] = gd["section"]
            result["sub_path"] = _split_subpath(gd.get("sub"))
        elif gd.get("div"):
            result["part"] = gd.get("part")
            result["division"] = gd["div"]
            # Unlike nsub/sub (captured *with* their own parens, since
            # those groups can repeat -- "(1)(a)"), subdiv's capture group
            # sits inside a single literal \(...\) in the regex and so
            # only ever holds the bare content ("18", not "(18)") --
            # _split_subpath expects the parens still attached to split
            # multiple bracketed levels apart, so passing it the bare
            # content directly finds nothing to split and silently drops
            # the Subdivision reference instead of attaching it.
            if gd.get("subdiv"):
                result["sub_path"] = [f"({gd['subdiv']})"]
        elif gd.get("part2"):
            result["part"] = gd["part2"]
    defm = _DEF_RE.search(raw)
    if defm:
        result["def_name"] = defm.group(1).strip()
    return result


def collect_page_notes(pages: list[PageText]) -> list[dict]:
    """Flattens every page's margin notes into parsed, page-tagged records."""
    notes = []
    for page in pages:
        for raw in merge_continuations(page.margin_notes):
            parsed = parse_note(raw)
            parsed["page"] = page.page_no
            notes.append(parsed)
    return notes
