"""
Which version of an Act a PDF is, and which versions of it we hold.

An Authorised Version states its own identity on its first page, above the
Table of Provisions:

    Authorised Version No. 114
    Criminal Procedure Act 2009
    No. 7 of 2009
    Authorised Version incorporating amendments as at
    1 July 2026

Two of those lines are the timeline this whole diff feature hangs off. The
**version number** is the Act's own sequential count of its reprints and is
what orders them -- No. 110 is unambiguously older than No. 111, with no
date arithmetic and no reliance on filenames. The **as-at date** is what a
reader actually wants to see ("as at 1 July 2026"), and is what connects a
change to the amending Act that caused it, since the Table of Amendments
records commencement dates (see amendments.py).

The other two lines identify the *work* rather than this expression of it:
the title, and "No. 7 of 2009" -- the Act's own number, fixed for its
whole life. Two PDFs sharing that number are versions of the same Act
however they are named, which is what lets a directory of files be read as
one Act's history rather than trusted from its filenames.

A Bill, an Explanatory Memorandum, and a consolidated Act printed before
this convention carry no version block at all. That is not an error: they
have no version, and `version` comes back None.
"""
import re
from datetime import datetime
from pathlib import Path

# "Authorised Version No. 114". The Act's own count of its reprints.
_VERSION_RE = re.compile(r"Authorised\s+Version\s+No\.?\s*(\d+)", re.IGNORECASE)

# "Authorised Version incorporating amendments as at\n1 July 2026" -- the
# date sits on the line below the phrase, so this reads across the break.
_AS_AT_RE = re.compile(
    r"incorporating\s+amendments\s+as\s+at\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)

# "Criminal Procedure Act 2009", "Evidence Bill 2008" -- a whole line, so a
# mention of an Act inside a sentence elsewhere on the page can't match.
_TITLE_RE = re.compile(r"^([A-Z][\w' \-()]+?\s(?:Act|Bill)\s(\d{4}))\s*$", re.MULTILINE)

# "No. 7 of 2009", and the pre-1970s five-digit form "No. 10096 of 1984".
# Never matches the version line above it: that one is followed by the Act's
# title, not by " of <year>".
_ACT_NO_RE = re.compile(r"No\.\s*(\d+)\s+of\s+(\d{4})")

# How many pages of front matter to read. The whole block sits on page 1;
# the second is read only so a PDF whose first page is a cover still
# resolves, and reading more would mean paying for a Table of Provisions
# that can run to twenty pages.
_FRONT_MATTER_PAGES = 2


def _iso_date(printed: str) -> "str | None":
    """"1 July 2026" -> "2026-07-01". Returned as ISO because these dates
    get sorted and compared far more often than they get printed, and the
    printed form sorts alphabetically into nonsense."""
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(printed.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_front_matter(text: str) -> dict:
    """{"version", "as_at", "as_at_printed", "title", "act_no", "year"} for
    a document's own front matter. Every field is None where the document
    doesn't state it -- a Bill has no version or as-at date, and an Act
    reprinted before the current convention may have neither.

    `version` is an int so it sorts numerically; `as_at` is ISO for the
    same reason, with the date as printed kept alongside it for display.
    """
    version_m = _VERSION_RE.search(text)
    as_at_m = _AS_AT_RE.search(text)
    title_m = _TITLE_RE.search(text)
    act_no_m = _ACT_NO_RE.search(text)
    printed = as_at_m.group(1).strip() if as_at_m else None
    year = None
    if act_no_m:
        year = int(act_no_m.group(2))
    elif title_m:
        year = int(title_m.group(2))
    return {
        "version": int(version_m.group(1)) if version_m else None,
        "as_at": _iso_date(printed) if printed else None,
        "as_at_printed": printed,
        "title": title_m.group(1) if title_m else None,
        "act_no": act_no_m.group(1) if act_no_m else None,
        "year": year,
    }


def read_front_matter(pdf_path: "str | Path") -> dict:
    """parse_front_matter over the first pages of a PDF. An unreadable or
    missing file gives the same all-None result a document with no version
    block does: this is metadata about a document, and not having it is
    never a reason to fail a parse that otherwise succeeded."""
    path = Path(pdf_path)
    if not path.exists():
        return parse_front_matter("")
    try:
        import fitz

        with fitz.open(path) as doc:
            text = "\n".join(doc[i].get_text() for i in range(min(_FRONT_MATTER_PAGES, doc.page_count)))
    except Exception:
        return parse_front_matter("")
    return parse_front_matter(text)


def discover_versions(directory: "str | Path") -> list[dict]:
    """Every Authorised Version in one directory, oldest first, each as its
    front matter plus the "path" it was read from.

    A document with no version number is not a version of anything and is
    left out -- a Bill and its Explanatory Memorandum commonly sit in the
    same directory as the Act they became, and they belong to that Act's
    history without being points on its timeline.

    Ordered by the Act's own version number rather than by date or
    filename: it is the only one of the three the Act itself guarantees to
    be sequential.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.pdf")):
        meta = read_front_matter(path)
        if meta["version"] is None:
            continue
        found.append({**meta, "path": str(path)})
    return sorted(found, key=lambda v: v["version"])


def describe(meta: dict) -> str:
    """One line naming a version, for a log or a heading. Says only what
    the document actually states, so a Bill doesn't get an empty "version
    None as at None"."""
    bits = []
    if meta.get("version") is not None:
        bits.append(f"Version {meta['version']}")
    if meta.get("as_at_printed"):
        bits.append(f"as at {meta['as_at_printed']}")
    if not bits:
        return meta.get("title") or "unversioned document"
    return f"{meta['title']} — {', '.join(bits)}" if meta.get("title") else ", ".join(bits)
