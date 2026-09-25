"""The changes between consecutive versions of a work, one sub-provision
at a time, and which of them a reviewer has confirmed.

A change is found by comparing text, and text differs for two reasons:
Parliament amended it, or the parser read the two reprints differently.
Nothing here can tell them apart for certain -- the amending Act's
instruction is strong evidence, and is shown, but the decision is a
person's. Until it is made, the change does not exist as far as the
public histories are concerned (see gate and dashboard._timeline).

A change is named by the provision (a diffing.provisions key), the two
versions, and the piece: its path within the provision ("1/f"),
"heading", or "whole" for the provision appearing or going. Names, not
positions, so a re-parse leaves a decision on the change it was about.
"""
import json

from corpus.domain import diffing
from corpus.publishing import html_view

WHOLE, HEADING = "whole", "heading"


def key_json(key: tuple) -> str:
    return json.dumps(list(key))


def key_label(key: tuple) -> str:
    kind, schedule, number = key
    if kind == "schedule":
        return f"Schedule {number}"
    if kind != "provision":
        return f"{kind.capitalize()} {number}"
    return f"Schedule {schedule} clause {number.upper()}" if schedule else f"s {number.upper()}"


def stands_for_section(node: dict) -> "str | None":
    """The section a row of stars stands for, where it is a whole section's.

    A repealed section leaves only its row, printed after the section
    before it and parsed as part of that one. attach_history gives the row
    the section's number and its "S. 99 repealed by ..." note, and that
    note, naming the section with no sub-path, is what tells it from a
    subsection's row."""
    number = node.get("number")
    if node.get("type") != "repealed" or not number:
        return None
    for note in node.get("history") or []:
        if (isinstance(note, dict) and (note.get("section") or "").lower() == number.lower()
                and not note.get("sub_path") and not note.get("target_kind") and not note.get("schedule")):
            return number
    return None


def _repealed_rows(nodes: list[dict]) -> dict:
    """{provision key: the row of stars standing for it}."""
    return {diffing.provision_identity("section", None, number): node
            for node in nodes for number in [stands_for_section(node)] if number}


def _units(nodes: list[dict], provision: dict, hierarchy) -> list[dict]:
    end = diffing.unit_end(nodes, provision["node_index"])
    units = html_view._wording_units(nodes[provision["node_index"]:end], hierarchy or html_view.HIERARCHY_ORDER)
    # Another section's row is not a piece of this one: left in, the
    # section before a repeal read as having gained a piece.
    return [u for u in units if not stands_for_section(u["tree_node"]["node"])]


def _row_html(row: dict) -> str:
    return html_view._provision_html(row["type"], "", html_view._esc(row.get("text") or ""), 0)


def _row_at(row: dict) -> "dict | None":
    return _where({"tree_node": {"node": row}})


def _where(unit: "dict | None") -> "dict | None":
    """The page and boxes a piece is printed at, for setting the two
    printed pages side by side."""
    if unit is None:
        return None
    node = unit["tree_node"]["node"]
    rects = node.get("rects") or []
    page = rects[0]["page"] if rects else node.get("page_start")
    # The words too: with no boxes, the page is searched for them.
    return {"page": page, "page_end": node.get("page_end") or page, "rects": rects,
            "text": " ".join((node.get("text") or node.get("heading") or "").split()[:12])} if page else None


def _index(units: list[dict], unit: dict) -> int:
    return next(k for k, u in enumerate(units) if u is unit)


def _located(units: list[dict], k: int) -> "dict | None":
    """units[k]'s page: its own, else the nearest piece's around it in the
    same provision -- earlier first -- searched for its own words. A piece
    the parse drew no box round (a paragraph's continuation) is still on
    a page, and without one there was nothing to set beside the other
    version's."""
    own = _where(units[k])
    if own is not None:
        return own
    for j in [*range(k - 1, -1, -1), *range(k + 1, len(units))]:
        near = _where(units[j])
        if near is not None:
            return {**near, "rects": [], "text": " ".join((units[k]["tree_node"]["node"].get("text") or "").split()[:12])}
    return None


def _absent(at: "dict | None") -> "dict | None":
    """Where a piece a version hasn't got would be: the page of what it
    would follow, marked so, and nothing on it outlined. A change can only
    be judged with both versions' pages beside it -- an insertion and a
    repeal included."""
    return {**at, "rects": [], "text": "", "absent": True} if at else None


def _would_follow(here: list[dict], there: list[dict], unit: dict) -> "dict | None":
    """Where `unit`, a piece of `there`, would sit among `here`'s pieces:
    after the nearest piece before it that `here` has, else at the
    provision's start."""
    paths = {u.get("path"): k for k, u in enumerate(here)}
    for j in range(_index(there, unit) - 1, -1, -1):
        k = paths.get(there[j].get("path"))
        if k is not None:
            return _absent(_located(here, k))
    return _absent(_located(here, 0)) if here else None


def step_changes(key: tuple, older: list[dict], newer: list[dict]) -> list[dict]:
    """The changed pieces of one provision from one version to the next."""
    out = []
    for change in html_view._compare_pieces(older, newer):
        if change["label"] == "Heading":
            piece = HEADING
        else:
            piece = ((change["new"] or change["old"]).get("path") or "") or WHOLE
        old_at = (_located(older, _index(older, change["old"])) if change["old"] is not None
                  else _would_follow(older, newer, change["new"]))
        new_at = (_located(newer, _index(newer, change["new"])) if change["new"] is not None
                  else _would_follow(newer, older, change["old"]))
        out.append({"piece": piece, "label": change["label"], "op": change["op"],
                    "old_html": change["old_html"], "new_html": change["new_html"],
                    "old_at": old_at, "new_at": new_at,
                    "_pair": (change["old"], change["new"])})
    return out


def _provision_would_follow(key: tuple, nodes: list[dict], order, prov: dict, other: dict) -> "dict | None":
    """Where the provision `key`, which only `other`'s version has, would
    sit in this one: at the end of the provision it follows there that
    this version has too, else the start of the one after it."""
    keys = list(other)
    at = keys.index(key)
    for near, last in ([(k, True) for k in reversed(keys[:at])] + [(k, False) for k in keys[at + 1:]]):
        if near in prov:
            units = _units(nodes, prov[near], order)
            if units:
                return _absent(_located(units, len(units) - 1 if last else 0))
    return None


def work_changes(versions: list[tuple]) -> list[dict]:
    """Every change across a work's versions, oldest step first.

    `versions` is [(version, nodes, hierarchy)] oldest first -- the text as
    reviewed so far. Each change: {"key", "provision" (key as JSON),
    "section", "from", "to", "piece", "label", "op", "old_html",
    "new_html", "old_at", "new_at", "_older", "_newer"} -- the last two the
    provision's units either side, for the caller's own evidence."""
    out = []
    held = [(v, nodes, hierarchy, diffing.provisions(nodes)) for v, nodes, hierarchy in versions]
    for (a, a_nodes, a_order, a_prov), (b, b_nodes, b_order, b_prov) in zip(held, held[1:]):
        a_rows, b_rows = _repealed_rows(a_nodes), _repealed_rows(b_nodes)
        for key in dict.fromkeys([*a_prov, *b_prov]):
            base = {"key": key, "provision": key_json(key), "section": key_label(key), "from": a, "to": b}
            if key not in a_prov or key not in b_prov:
                present = b_prov.get(key) or a_prov.get(key)
                units = _units(b_nodes if key in b_prov else a_nodes, present, b_order if key in b_prov else a_order)
                # Gone, and its row of stars printed where it was: repealed,
                # one change, the words on one side and the row on the
                # other. Still the whole provision's going, so confirming it
                # confirms the repeal the public history shows. The same
                # the other way for a repealed section coming back.
                row = b_rows.get(key) if key in a_prov else a_rows.get(key)
                present_at = _located(units, 0) if units else None
                # The version without it: where it would be.
                missing_at = (_provision_would_follow(key, a_nodes, a_order, a_prov, b_prov) if key in b_prov
                              else _provision_would_follow(key, b_nodes, b_order, b_prov, a_prov))
                if row is not None:
                    going = key in a_prov
                    words = html_view._units_html(units)
                    row_at = _row_at(row) or missing_at
                    out.append({**base, "piece": WHOLE, "label": "whole provision",
                                "op": "repeal" if going else "insert",
                                "old_html": words if going else _row_html(row),
                                "new_html": _row_html(row) if going else words,
                                "old_at": present_at if going else row_at,
                                "new_at": row_at if going else present_at,
                                "_older": units if going else [], "_newer": [] if going else units,
                                "_row": row})
                    continue
                out.append({**base, "piece": WHOLE, "label": "whole provision",
                            "op": "insert" if key in b_prov else "delete",
                            "old_html": None if key in b_prov else html_view._units_html(units),
                            "new_html": html_view._units_html(units) if key in b_prov else None,
                            "old_at": missing_at if key in b_prov else present_at,
                            "new_at": present_at if key in b_prov else missing_at,
                            "_older": [] if key in b_prov else units, "_newer": units if key in b_prov else []})
                continue
            older, newer = _units(a_nodes, a_prov[key], a_order), _units(b_nodes, b_prov[key], b_order)
            for change in step_changes(key, older, newer):
                out.append({**base, **change, "_older": older, "_newer": newer})
    return out


def confirmed(decisions: dict) -> dict:
    """{to_version: {"text": provision keys with a confirmed change of
    words or heading, "whole": provision keys whose appearing or going is
    confirmed}}, from db.load_history_decisions."""
    out: dict = {}
    for (provision, _from, to, piece), decision in decisions.items():
        if decision != "confirmed":
            continue
        step = out.setdefault(to, {"text": set(), "whole": set()})
        step["whole" if piece == WHOLE else "text"].add(tuple(json.loads(provision)))
    return out


def gate(chain: dict, whole: dict) -> dict:
    """A chain of wordings (lineage.provision_chains) with every absent
    span -- the provision not yet in the Act, or gone from it -- that
    nobody has confirmed taken out. `whole` is {to_version: keys} of
    confirmed appearances and goings.

    Text changes are gated where the chain is built (lineage._amended);
    an absent span is not a text comparison, so it is gated here. A gap in
    the middle whose ends are unconfirmed -- a provision the parser lost
    from one reprint -- closes up, and its two sides become one wording."""
    wordings = chain["wordings"]
    kept: list[dict] = []
    for n, wording in enumerate(wordings):
        wording = dict(wording)   # the chain is the timeline's, cached
        if wording["absent"]:
            present = next((w for w in wordings if not w["absent"]), None)
            key = present["key"] if present else None
            if n == 0:
                # Absent first: the provision arrived at the next wording.
                nxt = wordings[1]["from"]["version"] if len(wordings) > 1 else None
                ok = key in whole.get(nxt, ())
            else:
                # Absent later: it went at this wording's first version.
                ok = key in whole.get(wording["from"]["version"], ())
            if ok:
                kept.append(wording)
            continue
        if kept and not kept[-1]["absent"] and wordings[n - 1]["absent"]:
            # Rejoining across a gap nobody confirmed: one wording.
            joined = {**wording, "from": kept[-1]["from"], "versions": kept[-1]["versions"] + wording["versions"]}
            kept[-1] = joined
            continue
        kept.append(wording)
    for n, wording in enumerate(kept):
        if n + 1 == len(kept):
            wording.pop("ended_by", None)
    return {**chain, "wordings": kept}
