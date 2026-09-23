"""
Review work lent from one version of an Act to another.

corpus/domain/lineage.py decides which version vouches for which
provision; this is the half that reads the rows it decides about and
puts them where a reader will see them. A provision a version inherits
is shown in that version exactly as the reviewed version has it --
corrected, accepted, flagged -- because the raw words were the same, so
the reviewer's decisions about them are too.

Only `verified` rows travel. Boxes drawn on a page, links, and the
corrections log are about one PDF's pages and one reviewer's session,
and a reprint has neither. A unit a reviewer restructured (split,
inserted into, moved) is not lent at all: the edit is about positions in
one parse, and applying it to another is how reviewed provisions have
been deleted before (see review.finished_units).

Rows are matched by name *within the unit*: "1/a" under whichever
section it is, since a reprint can move a Section between Divisions
without changing a word of it, and the Division is part of the full
name.
"""
import json
from pathlib import Path

from corpus.domain import diffing, lineage
from corpus.parsing.identity import annotate_ids
from corpus.parsing.versions import split_document_slug
from corpus.storage import db

# The fields of a verified row that are a reviewer's decision about the
# words. Page, position and margin notes stay the borrowing version's
# own: they describe where this reprint printed the provision.
_DECISION_FIELDS = ("type", "number", "heading", "text", "verified_at", "needs_followup")


def relative_id(node_id: str, root_id: str) -> str:
    if node_id == root_id:
        return ""
    if node_id.startswith(root_id + "/"):
        return node_id[len(root_id) + 1:]
    return node_id


def unit_key(nodes: list[dict], root: int, keys_by_index: dict) -> tuple:
    """A unit's identity across versions: its provision key, or, for a
    unit with no number (a topical heading, the front matter), its own
    name -- which is how an unnumbered heading can still be compared."""
    return keys_by_index.get(root) or ("unit", nodes[root].get("id") or f"#{root}")


def unit_state(nodes: list[dict], units: list[list[int]], rows_by_index: dict, finished,
               edits: "dict | None" = None) -> dict:
    """What one version's own review says about each of its units.

    `nodes` is the raw parse with ids, `units` the review units (as
    indices) that `finished` numbers, `rows_by_index` the verified rows
    placed against the parse, and `edits` the placed structure edits.

    A unit is reviewed when every node in it has a row, or when the
    reviewer finished it and some of it survived -- the rest having been
    merged away, which is the same thing finished_units reads.
    """
    edits = edits or {}
    edited = set(edits) | {e.get("after") for e in edits.values() if e.get("after") is not None}
    keys_by_index = {p["node_index"]: key for key, p in diffing.provisions(nodes).items()}
    state = {"units": {}, "key_of_id": {}, "raw": {}, "reviewed": set(), "blocked": set()}
    for u, unit in enumerate(units):
        root = unit[0]
        if root >= len(nodes):
            continue  # inserted by a reviewer: the parse has no such unit
        key = unit_key(nodes, root, keys_by_index)
        if key in state["units"]:
            continue  # the same rule diffing.provisions applies: the first one wins
        parsed = [i for i in unit if i < len(nodes)]
        root_id = nodes[root].get("id") or ""
        rel = {relative_id(nodes[i].get("id") or "", root_id): i for i in parsed}
        rows = {name: rows_by_index[i] for name, i in rel.items() if i in rows_by_index}
        state["units"][key] = {"root": root, "root_id": root_id, "rel": set(rel), "rows": rows}
        for i in parsed:
            if nodes[i].get("id"):
                state["key_of_id"][nodes[i]["id"]] = key
        state["raw"][key] = (nodes[root].get("heading"), diffing.unit_text(nodes, root))
        if rows and (len(rows) == len(parsed) or u in finished):
            state["reviewed"].add(key)
        if any(i in edited for i in unit):
            state["blocked"].add(key)
    return state


def load_state(slug: str, base_dir=None) -> dict:
    """unit_state for one version, read the way review.py reads it."""
    from corpus.review import review

    data = json.loads((Path(base_dir or ".") / "data" / "parsed" / f"{slug}.json").read_text(encoding="utf-8"))
    nodes, unattached, fingerprint = data["nodes"], data.get("unattached_notes", []), data.get("fingerprint")
    annotate_ids(nodes, data.get("hierarchy") or None)
    edits = review.load_structure_edits(slug, fingerprint, base_dir, nodes=nodes)
    node_at = review._node_at(nodes, edits)
    _order, units = review.order_and_units(len(nodes), edits, node_at)
    verified = db.load_verified(slug, base_dir)
    rows_by_index, _unplaced = review.verified_by_index(verified, review.names_by_index(nodes, edits))
    finished = (review.finished_units(units, list(verified), markers_are_complete=True)
                if review.positions_are_trustworthy(slug, fingerprint) else set())
    state = unit_state(nodes, units, rows_by_index, finished, edits)
    state["unattached"] = unattached
    state["links"] = db.load_provision_links(slug, base_dir)
    state["parser_version"] = data.get("parser_version")
    state["meta"] = data.get("version") or {}
    return state


def sibling_slugs(slug: str, base_dir=None) -> dict[int, str]:
    """{version: slug} for every parsed version of this slug's work."""
    work, version = split_document_slug(slug)
    if version is None:
        return {}
    found = {}
    for path in (Path(base_dir or ".") / "data" / "parsed").glob(f"{work}-v*.json"):
        other_work, other_version = split_document_slug(path.stem)
        if other_work == work and other_version is not None:
            found[other_version] = path.stem
    return found


def reference_version(versions: list[int], version: int) -> "int | None":
    """The version a reviewer's changes are shown against: the next one
    toward current, or, for current itself, the one before it."""
    position = versions.index(version)
    if position + 1 < len(versions):
        return versions[position + 1]
    return versions[position - 1] if position else None


def work_review(slug: str, base_dir=None) -> "dict | None":
    """Everything the review tool needs to review one version as a
    version of its work -- None for a document held in one version.

    {"version", "versions": [{"version", "slug", "as_at_printed",
    "to_review", "current", "parser_version"}], "states", "status",
    "slugs", "reference"}."""
    slugs = sibling_slugs(slug, base_dir)
    _work, version = split_document_slug(slug)
    if len(slugs) < 2 or version not in slugs:
        return None
    versions = sorted(slugs)
    states = {v: load_state(slugs[v], base_dir) for v in versions}
    status = resolve(versions, states)
    return {
        "version": version,
        "versions": [{
            "version": v, "slug": slugs[v],
            "as_at_printed": states[v]["meta"].get("as_at_printed"),
            "to_review": sum(1 for e in status[v].values() if e["status"] == lineage.TO_REVIEW),
            "current": v == versions[-1],
            "parser_version": states[v]["parser_version"],
        } for v in versions],
        "states": states, "status": status, "slugs": slugs,
        "reference": reference_version(versions, version),
    }


def effective_nodes(slug: str, review_state: dict, version: int) -> list[dict]:
    """One version's nodes with its own review and what it inherits."""
    from corpus.review import review

    nodes = review.build_current_nodes(slug)[0]
    return overlay(nodes, review_state["states"][version], review_state["status"][version],
                   review_state["states"])


def resolve(versions: list[int], states: dict) -> dict:
    """lineage.review_status over these states, with one more check it
    cannot make from signatures alone: a unit is lent only to a unit with
    the same pieces, so every row has somewhere to go and every piece a
    row to take."""
    links = {v: states[v].get("links") or {} for v in versions}
    status = lineage.review_status(
        versions,
        {v: states[v]["raw"] for v in versions},
        {v: states[v]["reviewed"] for v in versions},
        links=links,
        blocked={v: states[v]["blocked"] for v in versions},
    )
    for v in versions:
        for key, entry in status[v].items():
            if entry["status"] != lineage.INHERITS:
                continue
            source = states[entry["source"]]["units"][entry["source_key"]]
            if states[v]["units"][key]["rel"] != source["rel"]:
                status[v][key] = {"status": lineage.TO_REVIEW, "source": None, "source_key": None}
    return status


def overlay(current_nodes: list[dict], state: dict, statuses: dict, states: dict) -> list[dict]:
    """A version's nodes as a reader sees them, with each inherited unit
    taking the rows of the version it inherits from.

    `current_nodes` is review.build_current_nodes' output: a node with a
    row of this version's own is left alone. A piece the source reviewer
    merged away has no row there and is left out here too.
    """
    out = []
    for node in current_nodes:
        key = None if "_node_id" in node else state["key_of_id"].get(node.get("id"))
        entry = statuses.get(key) if key is not None else None
        if not entry or entry["status"] != lineage.INHERITS:
            out.append(node)
            continue
        unit = state["units"][key]
        source = states[entry["source"]]["units"][entry["source_key"]]
        row = source["rows"].get(relative_id(node["id"], unit["root_id"]))
        if row is None:
            continue
        lent = {**node, **{f: row.get(f) for f in _DECISION_FIELDS if f in row}}
        lent["_inherited_from"] = entry["source"]
        out.append(lent)
    return out


def checked_keys(nodes: list[dict]) -> set:
    """The provisions a human has vouched for in these effective nodes:
    every piece accepted and none flagged -- export_static_site's
    approved_units, asked of a provision rather than a page."""
    checked = set()
    for key, provision in diffing.provisions(nodes).items():
        root = provision["node_index"]
        end = diffing.unit_end(nodes, root)
        if all(n.get("verified_at") and not n.get("needs_followup") for n in nodes[root:end]):
            checked.add(key)
    return checked

