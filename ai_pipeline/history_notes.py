"""
Parses the amendment-history margin notes (the sidebar notations saying when
a section/subsection/definition was inserted, amended, substituted, or
repealed, and by which amending Act) and links each one back to the node it
annotates.

This is pure regex over the note's own citation prefix -- the notes already
name what they amend ("S. 3(2) inserted by...", "Pt 1 Div. 1 Subdiv. (4)
..."), so no LLM call is needed here. Coverage on the Crimes Act is ~98%;
anything that doesn't parse is kept as an unattached note rather than
dropped, for manual linking during review.
"""
import re

from .extract import PageText

# A margin note can wrap across more than one extracted text block on the
# same page; a fragment that doesn't start with a recognisable citation is a
# continuation of the previous note, not a new one.
_CITATION_START_RE = re.compile(
    r"^(Notes?\s*[\w]*\s*to\s+s\.|Heading\s+preceding|S\.|Ss\b|Pt\s|New\s+(s\.|Pt\b))",
    re.IGNORECASE,
)

_NEW_PREFIX_RE = re.compile(r"^New\s+", re.IGNORECASE)

_CITATION_RE = re.compile(
    r"^(?:Notes?\s*(?P<noteid>[\w]*)\s*to\s+s\.\s*(?P<nsection>\d+[A-Za-z]*)(?P<nsub>(?:\([^)]*\))*))"
    r"|^(?:Heading\s+preceding\s+s\.\s*(?P<hsection>\d+[A-Za-z]*))"
    r"|^(?:Ss?\.?\s*(?P<section>\d+[A-Za-z]*)(?:\s*[–-]\s*\d+[A-Za-z]*)?(?P<sub>(?:\([^)]*\))*))"
    r"|^(?:Pt\s+(?P<part>\S+)\s+Div\.?\s*(?P<div>\S+?)(?:\s+Subdivs?\.?\s*\((?P<subdiv>[^)]*)\))?(?=[\s(]|$))"
    r"|^(?:Pt\s+(?P<part2>\S+)\s*\(Heading)",
    re.IGNORECASE,
)

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
    a division/part reference, and/or a definition term."""
    text = _NEW_PREFIX_RE.sub("", raw.strip())
    result = {
        "raw": raw.strip(),
        "section": None,
        "sub_path": [],
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
        elif gd.get("hsection"):
            result["section"] = gd["hsection"]
        elif gd.get("section"):
            result["section"] = gd["section"]
            result["sub_path"] = _split_subpath(gd.get("sub"))
        elif gd.get("div"):
            result["part"] = gd.get("part")
            result["division"] = gd["div"]
            result["sub_path"] = _split_subpath(gd.get("subdiv"))
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
