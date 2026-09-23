"""
Connects two pieces of an Act's amendment history that the pipeline
already has, but hasn't linked together yet.

An Act's margin notes name the Act that changed a provision, but only by
number -- "S. 3 def. of accused amended by No. 68/2009 s. 51(b)(i)".
Those are already parsed and attached to each node
(corpus/history_notes.py), so the numbers are there, structured.
What that number actually means is in the Endnotes' Table of Amendments
(corpus/endnotes.py): No. 68/2009 is the Criminal Procedure
Amendment (Consequential and Transitional Provisions) Act 2009, assented
24.11.09, sections 3-58 commencing 25.11.09.

This module looks one up from the other, with a fallback: if an Act is
named in a margin note but isn't in this Act's own Table of Amendments
(maybe it amended a provision that's since been repealed), it can still
be identified from corpus/act_registry.json, which has every
Victorian Act's title, number and current in-force status. That fallback
doesn't include assent or commencement dates, and the result always says
which source it came from, so a caller never mistakes a registry-only
guess for the Act's own official record.

This is a plain lookup: it works entirely on data already parsed
elsewhere, so it doesn't open any files itself.
"""
import re

from corpus.exporters.akn_export import _extract_citations


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
    """A lookup that works both ways -- by "68/2009" and by the bare Act
    number "68" -- because a pre-1970s margin note sometimes cites an Act
    by number alone, with no year ("No. 8679"). Within one Act's own
    Table of Amendments those numbers are unique anyway.

    Returns {"by_citation": {...}, "by_act_no": {...}, "registry": {...}},
    which resolve_citation reads. Built once per Act and reused -- it's a
    full pass over the registry, so nothing expensive, but no reason to
    redo it."""
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
    Act's own Table of Amendments nor the registry recognises it.

    Checked in that order on purpose: this Act's own endnotes are the
    authoritative record for this Act, and the only place with assent
    and commencement dates. The registry is just a general list that can
    only give a title and whether the Act is still in force."""
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
        # Zero matches: neither source knows this Act. More than one:
        # the number alone is ambiguous and there's no year to choose
        # between them -- naming the wrong Act would be worse than
        # naming none.
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
    """Breaks one margin note into the pieces a renderer needs in order to
    link it: a list of {"text"} runs, where a run naming an amending Act
    carries either {"record"} (this Act's own endnotes, or the general
    registry, recognise it) or {"citation"} (it looks like a citation, but
    nothing recognises what it names).

    Every citation found becomes one of those two kinds of run -- never
    left as plain, unclickable text. A run with a "record" links to this
    Act's own Endnotes entry for that citation. A run with just a
    "citation" links instead to the standing page at
    /legislation/<act_no>[-<year>] (see dashboard.py), which redirects to
    that Act's parse once one exists, and otherwise says plainly that it
    hasn't been parsed yet. A reader should never come across a citation
    this pipeline spotted and then said nothing about.

    The note itself only ever writes the bare citation ("No. 68/2009"),
    which is what a reader wants to click -- the full name and dates
    belong in the link's tooltip, not spelled out next to every note.

    Runs are returned in order and, joined together, reproduce `raw`
    exactly, so a renderer can just escape each one in turn without doing
    any character-position math of its own. If no citations are found at
    all, the result is just the whole note as one plain run."""
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
    section = path.get("section") or path.get("clause") or path.get("item")
    if not section:
        return node.get("heading") or node.get("type") or "?"
    label = f"s. {section}"
    label += "".join(f"({path[level]})" for level in _PROVISION_SUBLEVELS if path.get(level))
    if path.get("definition"):
        label += f' def. of "{path["definition"]}"'
    return label


def summarise_by_act(nodes: list[dict], index: dict) -> list[dict]:
    """Every amending Act named in this Act's own margin notes, with the
    provisions each one touched -- the Table of Amendments read the other
    way round, answering the question a reader actually has ("what did
    No. 68/2009 change here?").

    Ordered the same way the Table of Amendments is: chronologically by
    assent. An Act named in a note but missing from that table (found
    from the registry instead) is added at the end rather than dropped."""
    by_citation: dict[str, dict] = {}
    for node in nodes:
        for note in node.get("history") or []:
            for record in resolve_note(note["raw"], index):
                key = record.get("citation") or record["title"]
                entry = by_citation.setdefault(key, {"record": record, "provisions": [], "seen": set()})
                path = node.get("path") or {}
                label = provision_label(node)
                # One Act often amends the same provision more than once
                # (in different years, or with several notes on one
                # section), and listing a provision four times would look
                # like a mistake rather than real history. So it's counted
                # once, keeping the first note.
                if label in entry["seen"]:
                    continue
                entry["seen"].add(label)
                entry["provisions"].append({
                    "label": label,
                    "note": note["raw"],
                    # Carried so a renderer can link the provision to its own
                    # page without re-deriving it from the label string.
                    "section_number": path.get("section") or path.get("clause") or path.get("item"),
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
