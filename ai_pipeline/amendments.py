"""
Joins the two halves of an Act's amendment history that the pipeline
already holds but has never connected.

An Act's margin notes cite the Act that changed a provision by number
alone -- "S. 3 def. of accused amended by No. 68/2009 s. 51(b)(i)". Those
are parsed and attached per node (ai_pipeline/history_notes.py), so the
codes are structured. What they mean is in the Endnotes' own Table of
Amendments (ai_pipeline/endnotes.py): No. 68/2009 is the Criminal
Procedure Amendment (Consequential and Transitional Provisions) Act 2009,
assented 24.11.09, ss 3-58 commencing 25.11.09.

This module is the lookup between them, plus a fallback: an Act cited in
a margin note but absent from this Act's own Table of Amendments (it
amended a provision that has since been repealed, say) can still be named
from ai_pipeline/act_registry.json, which knows every Victorian Act's
title, number and in-force status. That fallback carries no assent or
commencement detail -- the record says which source it came from, so a
caller never presents registry-only data as if it were the Act's own
endnotes.

Pure lookup: everything comes in as already-parsed data, so this holds no
file paths and reads nothing.
"""
import re

from .akn_export import _extract_citations


def citations_in(raw: str) -> list[dict]:
    """Every amending-Act citation in one margin note, as
    {label, act_no, year}. Re-exported from akn_export so a caller
    resolving notes doesn't have to reach into that module's internals --
    one definition of what a citation looks like, shared by the AKN export
    and by this."""
    return _extract_citations(raw)


def _registry_by_number(act_registry: dict) -> dict[str, list[dict]]:
    """act_no -> the registry entries carrying it. Victorian Act numbers
    restart each year, so a number alone can name several Acts; the year
    picks between them where a citation has one."""
    index: dict[str, list[dict]] = {}
    for title, entry in act_registry.items():
        index.setdefault(str(entry.get("act_no")), []).append({**entry, "title": title})
    return index


def build_amendment_index(endnotes: dict | None, act_registry: dict | None = None) -> dict:
    """A lookup keyed both ways -- by "68/2009" and by the bare act number
    "68" -- because a pre-1970s margin note cites an Act by a number with
    no year at all ("No. 8679"), and within one Act's own Table of
    Amendments those numbers are unique anyway.

    Returns {"by_citation": {...}, "by_act_no": {...}, "registry": {...}},
    which resolve_citation reads. Built once per Act and reused; nothing
    here is expensive, but it's a full pass over the registry."""
    by_citation: dict[str, dict] = {}
    by_act_no: dict[str, dict] = {}
    for record in (endnotes or {}).get("amending_acts") or []:
        entry = {
            "title": record.get("title"),
            "citation": record.get("citation"),
            "act_no": record.get("act_no"),
            "year": record.get("year"),
            "is_statutory_rule": record.get("is_statutory_rule", False),
            "source": "endnotes",
            **record.get("fields", {}),
        }
        if record.get("citation"):
            by_citation.setdefault(record["citation"], entry)
        if record.get("act_no"):
            by_act_no.setdefault(str(record["act_no"]), entry)
    return {
        "by_citation": by_citation,
        "by_act_no": by_act_no,
        "registry": _registry_by_number(act_registry or {}),
    }


def resolve_citation(citation: dict, index: dict) -> dict | None:
    """What one margin-note citation refers to, or None if neither the
    Act's own Table of Amendments nor the registry knows it.

    Tried in that order deliberately: this Act's own endnotes are the
    authoritative, Act-specific record (and the only source of assent and
    commencement dates), while the registry is a general index that can
    only supply a title and whether the Act is still in force."""
    act_no = str(citation.get("act_no") or "")
    year = citation.get("year")
    if not act_no:
        return None

    if year is not None:
        found = index["by_citation"].get(f"{act_no}/{year}")
        if found is not None:
            return found
    found = index["by_act_no"].get(act_no)
    if found is not None and (year is None or str(found.get("year")) == str(year)):
        return found

    candidates = index["registry"].get(act_no) or []
    if year is not None:
        candidates = [c for c in candidates if str(c.get("year")) == str(year)]
    if len(candidates) != 1:
        # Zero: an Act neither source knows. More than one: the number
        # alone is ambiguous and there's no year to pick with -- naming
        # the wrong Act is worse than naming none.
        return None
    entry = candidates[0]
    return {
        "title": entry["title"],
        "citation": f"{act_no}/{entry['year']}" if entry.get("year") else act_no,
        "act_no": act_no,
        "year": entry.get("year"),
        "in_force": entry.get("in_force"),
        "source": "registry",
    }


def describe(record: dict) -> str:
    """One line naming an amending Act and what is known about it, for a
    tooltip or a listing. Only states what the record actually holds: a
    registry-sourced one gets its title and nothing more, rather than an
    empty "assented" that reads like missing data."""
    bits = [record["title"] or f"Act No. {record.get('citation')}"]
    if record.get("assent_date"):
        bits.append(f"assented {record['assent_date']}")
    if record.get("date_of_making"):
        bits.append(f"made {record['date_of_making']}")
    commencement = record.get("commencement_date") or record.get("date_of_commencement")
    if commencement:
        bits.append(f"commenced {commencement}")
    if record.get("source") == "registry" and record.get("in_force") is False:
        bits.append("no longer in force")
    return " — ".join(bits)


def resolve_note(raw: str, index: dict) -> list[dict]:
    """Every amending Act one margin note names, resolved. A note commonly
    cites several ("substituted by Nos 8679 s. 2, 37/1986 s. 8")."""
    out = []
    seen = set()
    for citation in citations_in(raw):
        record = resolve_citation(citation, index)
        if record is None:
            continue
        key = record.get("citation") or record.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append({**record, "cited_as": citation["label"]})
    return out


def linkify_note(raw: str, index: "dict | None") -> list[dict]:
    """One margin note broken into the pieces a renderer needs to link it:
    a list of {"text"} runs, where a run naming an amending Act carries
    either {"record"} (this index -- this Act's own endnotes, or the
    general registry -- knows it) or {"citation"} (the shape of a citation
    was detected but nothing knows what it names).

    Every detected citation becomes a run of one of those two kinds --
    never left as bare, unclickable text. A record-carrying run is for its
    caller to link into this Act's own Endnotes entry for that citation; a
    citation-carrying run is for the caller to link into the standing
    resolver at /legislation/<act_no>[-<year>] instead (see dashboard.py),
    which redirects to that Act's own parse once one exists and otherwise
    says plainly that it hasn't been parsed yet. A reader should never
    meet a citation this pipeline noticed and then said nothing about.

    The note itself only ever writes the bare citation ("No. 68/2009"),
    and that citation is the thing a reader wants to click -- the full
    name and its dates belong in the link's own tooltip, not spelled out
    beside every note.

    Runs are returned in order and concatenate back to `raw` exactly, so a
    renderer escapes each one and never has to do span arithmetic of its
    own. Without any citations detected at all, the result is simply the
    whole note as one plain run."""
    resolved = {}
    for record in resolve_note(raw, index) if index else []:
        resolved[record["cited_as"]] = record

    runs: list[dict] = []
    cursor = 0
    for citation in citations_in(raw):
        if citation["start"] > cursor:
            runs.append({"text": raw[cursor : citation["start"]]})
        record = resolved.get(citation["label"])
        run = {"text": raw[citation["start"] : citation["end"]]}
        if record is not None:
            run["record"] = record
        else:
            run["citation"] = {"act_no": citation["act_no"], "year": citation["year"]}
        runs.append(run)
        cursor = citation["end"]
    if cursor < len(raw):
        runs.append({"text": raw[cursor:]})
    return runs


_PROVISION_SUBLEVELS = ("subsection", "paragraph", "subparagraph", "sub_subparagraph")


def provision_label(node: dict) -> str:
    """"s. 28(1)(b)" -- where in the Act a margin note sits, read off the
    path breadcrumb tree.py stamps on every node. Falls back to the node's
    own heading (a defined term has no number of its own) and finally to
    its type, so a provision is never listed as an empty string."""
    path = node.get("path") or {}
    section = path.get("section") or path.get("clause")
    if not section:
        return node.get("heading") or node.get("type") or "?"
    label = f"s. {section}"
    label += "".join(f"({path[level]})" for level in _PROVISION_SUBLEVELS if path.get(level))
    if path.get("definition"):
        label += f' def. of "{path["definition"]}"'
    return label


def summarise_by_act(nodes: list[dict], index: dict) -> list[dict]:
    """Every amending Act that this Act's own margin notes cite, with the
    provisions each one touched -- the Table of Amendments read the other
    way round, which is the question a reader actually has ("what did
    No. 68/2009 change here?").

    Ordered by the Table of Amendments' own order, which is chronological
    by assent; an Act cited in a note but absent from that table (resolved
    from the registry instead) is appended after it rather than dropped."""
    by_citation: dict[str, dict] = {}
    for node in nodes:
        for note in node.get("history") or []:
            for record in resolve_note(note["raw"], index):
                key = record.get("citation") or record["title"]
                entry = by_citation.setdefault(key, {"record": record, "provisions": [], "seen": set()})
                path = node.get("path") or {}
                label = provision_label(node)
                # One Act commonly amends the same provision more than once
                # (in different years, or several notes on one section), and
                # a provision listed four times reads as an error rather
                # than as history. Counted once, keeping the first note.
                if label in entry["seen"]:
                    continue
                entry["seen"].add(label)
                entry["provisions"].append({
                    "label": label,
                    "note": note["raw"],
                    # Carried so a renderer can link the provision to its own
                    # page without re-deriving it from the label string.
                    "section_number": path.get("section") or path.get("clause"),
                })

    order = {c: i for i, c in enumerate(index["by_citation"])}
    ranked = sorted(
        by_citation.items(),
        key=lambda kv: (order.get(kv[0], len(order)), kv[0]),
    )
    return [
        {
            "citation": citation,
            "record": entry["record"],
            "provisions": entry["provisions"],
            "count": len(entry["provisions"]),
        }
        for citation, entry in ranked
    ]


def anchor_id(citation: str | None) -> str:
    """A stable HTML id for one amending Act ("68/2009" -> "act-68-2009"),
    so a margin note can link straight to that Act's own row on the
    endnotes page."""
    return "act-" + re.sub(r"[^a-z0-9]+", "-", (citation or "unknown").lower()).strip("-")
