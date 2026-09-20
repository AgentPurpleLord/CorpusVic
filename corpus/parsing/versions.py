"""
Works out which version of an Act a PDF is, and which versions of it we
hold.

An Authorised Version states its own identity on its first page, above
the Table of Provisions:

    Authorised Version No. 114
    Criminal Procedure Act 2009
    No. 7 of 2009
    Authorised Version incorporating amendments as at
    1 July 2026

Two of those lines are what the whole version-timeline feature relies
on. The **version number** is the Act's own count of its reprints, and
is what orders them -- No. 110 is unambiguously older than No. 111, no
date maths or filenames needed. The **as-at date** is what a reader
actually wants to see ("as at 1 July 2026"), and is also what connects a
change to the amending Act that caused it, since the Table of
Amendments records commencement dates too (see amendments.py).

The other two lines identify the Act itself, rather than this particular
reprint of it: the title, and "No. 7 of 2009" -- the Act's own number,
fixed for its whole life. Two PDFs that share that number are versions
of the same Act no matter what they're named, which is what lets a
directory of files be read as one Act's history instead of having to
trust filenames.

A Bill, an Explanatory Memorandum, or an Act printed before this
convention started carry no version information at all. That's not an
error -- they simply have no version, and `version` comes back None.
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
    """"1 July 2026" -> "2026-07-01". Returned in this YYYY-MM-DD form
    because these dates get sorted and compared far more often than
    they get displayed, and sorting the printed form alphabetically
    would give nonsense results."""
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(printed.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_front_matter(text: str) -> dict:
    """{"version", "as_at", "as_at_printed", "title", "act_no", "year"} for
    a document's front matter. Every field is None where the document
    doesn't state it -- a Bill has no version or as-at date, and an Act
    reprinted before the current convention may have neither.

    `version` is a plain int so it sorts numerically; `as_at` is in
    YYYY-MM-DD form for the same reason, with the date as it was printed
    kept alongside it for display.
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
    """Runs parse_front_matter over the first pages of a PDF. An
    unreadable or missing file gives the same all-None result as a
    document with no version information -- this is just metadata about
    a document, and not having it is never a reason to fail a parse that
    otherwise succeeded."""
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
    """Every Authorised Version in one directory, oldest first, each
    given as its front matter plus the "path" it was read from.

    A document with no version number isn't a version of anything, and
    is left out -- a Bill and its Explanatory Memorandum commonly sit in
    the same directory as the Act they became, and they're part of that
    Act's history without being points on its version timeline.

    Ordered by the Act's own version number rather than by date or
    filename, since that's the only one of the three the Act itself
    guarantees will be in sequence.
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


# ---------------------------------------------------------------------------
# Works and their versions
#
# A "work" is the Act itself -- the Criminal Procedure Act 2009 -- and a
# "version" is one Authorised Version of it. Everything in this pipeline
# addresses a document by a single slug, from the parse's filename to the
# review database's key to the browse URL, so a version gets a slug of
# its own rather than tracking a second identifier alongside the first:
# "criminal-procedure-act-v114". This module builds and reads apart that
# slug, so every existing caller keeps working with just one string.
#
# An Act opts into version tracking by having its PDFs put in a
# directory named after it (acts/criminal-procedure-act/cpa-114.pdf). A
# PDF sitting directly in acts/ keeps its own filename as its slug no
# matter what version it states -- so adding this feature changed
# nothing about documents already parsed, and nobody's review work on
# them moved.
# ---------------------------------------------------------------------------

_DOCUMENT_SLUG_RE = re.compile(r"^(?P<work>[a-z0-9]+(?:-[a-z0-9]+)*?)-v(?P<version>\d+)$")


def document_slug(work: str, version: "int | None") -> str:
    """The slug one version of a work is stored and addressed under. An
    unversioned document just uses its work's own slug -- a Bill isn't
    version 1 of anything."""
    return f"{work}-v{version}" if version is not None else work


def split_document_slug(slug: str) -> "tuple[str, int | None]":
    """The inverse: ("criminal-procedure-act", 114), or (slug, None) for a
    document that isn't a version of anything."""
    m = _DOCUMENT_SLUG_RE.match(slug)
    return (m.group("work"), int(m.group("version"))) if m else (slug, None)


def work_directory(pdf_path: "str | Path", acts_dir: "str | Path" = "acts") -> "str | None":
    """The work a PDF belongs to, based on where it sits:
    acts/<work>/<file>.pdf is a version of <work>; acts/<file>.pdf
    belongs to no work and keeps its own name. Returns the directory's
    name, or None.

    This is based on the file's location on purpose, not on reading the
    PDF's own contents. An Act states its version number whether or not
    anyone wants it tracked, so if this went by content alone, every
    document here would have been renamed the moment this feature
    landed, along with its review work. Putting a file in a directory is
    what actually opts it in."""
    pdf_path = Path(pdf_path)
    acts_dir = Path(acts_dir)
    try:
        relative = pdf_path.resolve().relative_to(acts_dir.resolve())
    except (ValueError, OSError):
        return None
    return relative.parts[0] if len(relative.parts) > 1 else None


def group_versions(documents: list[dict]) -> dict[str, list[dict]]:
    """{work slug -> its versions, oldest first} for documents that are
    versions of something. Each document can be whatever the caller
    holds, as long as it has a "slug" -- the work and version are read
    back out of that, so this can never disagree with what the
    documents are actually stored under.

    A document that isn't a version of anything is left out: it has no
    timeline, and a work with only one version has nothing to compare
    it against.
    """
    works: dict[str, list[dict]] = {}
    for document in documents:
        work, version = split_document_slug(document["slug"])
        if version is None:
            continue
        works.setdefault(work, []).append({**document, "work": work, "version": version})
    return {work: sorted(vs, key=lambda d: d["version"]) for work, vs in sorted(works.items())}


def current_version(versions: list[dict]) -> "dict | None":
    """The one that is in force of a work's versions -- the highest
    Authorised Version number. Everything below it is superseded."""
    return max(versions, key=lambda d: d["version"]) if versions else None
