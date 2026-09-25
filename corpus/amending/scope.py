"""Which amending Acts a work's held versions need: every Act the
newest reprint's Table of Amendments lists (the base's, normally -- it
lists every Act that ever amended this one), bar those already in force
by the oldest reprint held, which have nothing to be checked against.

The table is the Act's own record, so a margin note the parser misread
or missed loses no Act; its entry also gives the title that finds the
Act on legislation.vic.gov.au. Which step between reprints an Act's
amendments show in is still read from the margin notes each provision
gains, else from the first reprint whose table lists it -- an older
reprint's table does not always parse.
"""
from corpus import PROJECT_ROOT
from corpus.domain import diffing, lineage
from corpus.review.inheritance import sibling_slugs
from corpus.storage import parsed as parsed_files


def _notes(parsed: dict) -> dict:
    loose = lineage.loose_notes(parsed.get("unattached") or [])
    out = {key: list(p.get("history") or []) for key, p in diffing.provisions(parsed["nodes"]).items()}
    for key, notes in loose.items():
        out.setdefault(key, []).extend(notes)
    return out


def _table(parsed: dict) -> dict:
    return {a["citation"]: a for a in (parsed.get("endnotes") or {}).get("amending_acts") or [] if a.get("citation")}


def acts_between(work_slug: str, base_dir=None) -> list[dict]:
    """[{"citation", "title", "versions": the reprints it first shows in,
    "record": its Table of Amendments entry}], oldest Act first."""
    base = base_dir or PROJECT_ROOT
    slugs = sibling_slugs(work_slug, base)
    # Only each version's notes and table are kept, read in build order
    # (corpus/storage/parsed.py): a hundred whole Acts at once is too many.
    notes_of, tables = {}, {}
    for path in parsed_files.build_order(base / "data" / "parsed" / f"{slug}.json" for slug in slugs.values()):
        data = parsed_files.load(path)
        v = next(v for v, slug in slugs.items() if slug == path.stem)
        notes_of[v], tables[v] = _notes(data), _table(data)
    versions = sorted(notes_of)
    if len(versions) < 2:
        return []
    listed: dict = {}
    for v in reversed(versions):          # the newest reprint's entry wins
        for citation, record in tables[v].items():
            listed.setdefault(citation, record)
    noted: dict = {}
    for older, newer in zip(versions, versions[1:]):
        was, now = notes_of[older], notes_of[newer]
        for key, notes in now.items():
            for citation in lineage._cited(notes) - lineage._cited(was.get(key, [])):
                noted.setdefault(citation, set()).add(newer)
    oldest = versions[0]
    in_force = set(tables[oldest]) | lineage._cited([n for notes in notes_of[oldest].values() for n in notes])
    out = []
    for citation, record in listed.items():
        if citation in in_force and citation not in noted:
            continue   # a note new later is an amendment commencing later
        shows = noted.get(citation) or {next((v for v in versions[1:] if citation in tables[v]), None)} - {None}
        out.append({"citation": citation, "title": record["title"], "versions": sorted(shows), "record": record})
    return sorted(out, key=lambda e: (int(e["record"].get("year") or 0), int(e["record"].get("act_no") or 0)))
