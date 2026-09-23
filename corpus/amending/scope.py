"""Which amending Acts a work's held versions need: those whose
amendments fall between two reprints held here.

Read from the margin notes each provision gains between one reprint and
the next, kept only where the newer reprint's Table of Amendments lists
the Act -- which is also where its title comes from, and the title is
what finds the Act on legislation.vic.gov.au. An amendment from before
the oldest reprint held has nothing to be checked against, so its Act
is not needed.
"""
import json

from corpus import PROJECT_ROOT
from corpus.domain import diffing, lineage
from corpus.review.inheritance import sibling_slugs


def _notes(parsed: dict) -> dict:
    loose = lineage.loose_notes(parsed.get("unattached") or [])
    out = {key: list(p.get("history") or []) for key, p in diffing.provisions(parsed["nodes"]).items()}
    for key, notes in loose.items():
        out.setdefault(key, []).extend(notes)
    return out


def acts_between(work_slug: str, base_dir=None) -> list[dict]:
    """[{"citation", "title", "versions": the reprints it first shows in,
    "record": its Table of Amendments entry}], oldest Act first."""
    base = base_dir or PROJECT_ROOT
    slugs = sibling_slugs(work_slug, base)
    parsed = {v: json.loads((base / "data" / "parsed" / f"{slugs[v]}.json").read_text(encoding="utf-8"))
              for v in sorted(slugs)}
    versions = sorted(parsed)
    found: dict = {}
    for older, newer in zip(versions, versions[1:]):
        was, now = _notes(parsed[older]), _notes(parsed[newer])
        table = {a["citation"]: a for a in (parsed[newer].get("endnotes") or {}).get("amending_acts") or []
                 if a.get("citation")}
        for key, notes in now.items():
            for citation in lineage._cited(notes) - lineage._cited(was.get(key, [])):
                if citation in table:
                    entry = found.setdefault(citation, {"citation": citation, "title": table[citation]["title"],
                                                        "versions": set(), "record": table[citation]})
                    entry["versions"].add(newer)
    out = [{**e, "versions": sorted(e["versions"])} for e in found.values()]
    return sorted(out, key=lambda e: (int(e["record"].get("year") or 0), int(e["record"].get("act_no") or 0)))
