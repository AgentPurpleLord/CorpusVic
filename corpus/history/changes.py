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


def _units(nodes: list[dict], provision: dict, hierarchy) -> list[dict]:
    end = diffing.unit_end(nodes, provision["node_index"])
    return html_view._wording_units(nodes[provision["node_index"]:end], hierarchy or html_view.HIERARCHY_ORDER)


def _where(unit: "dict | None") -> "dict | None":
    """The page and boxes a piece is printed at, for setting the two
    printed pages side by side."""
    if unit is None:
        return None
    node = unit["tree_node"]["node"]
    rects = node.get("rects") or []
    page = rects[0]["page"] if rects else node.get("page_start")
    return {"page": page, "rects": rects} if page else None


def step_changes(key: tuple, older: list[dict], newer: list[dict]) -> list[dict]:
    """The changed pieces of one provision from one version to the next."""
    out = []
    for change in html_view._compare_pieces(older, newer):
        if change["label"] == "Heading":
            piece = HEADING
        else:
            piece = ((change["new"] or change["old"]).get("path") or "") or WHOLE
        out.append({"piece": piece, "label": change["label"], "op": change["op"],
                    "old_html": change["old_html"], "new_html": change["new_html"],
                    "old_at": _where(change["old"]), "new_at": _where(change["new"]),
                    "_pair": (change["old"], change["new"])})
    return out


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
        for key in dict.fromkeys([*a_prov, *b_prov]):
            base = {"key": key, "provision": key_json(key), "section": key_label(key), "from": a, "to": b}
            if key not in a_prov or key not in b_prov:
                present = b_prov.get(key) or a_prov.get(key)
                units = _units(b_nodes if key in b_prov else a_nodes, present, b_order if key in b_prov else a_order)
                out.append({**base, "piece": WHOLE, "label": "whole provision",
                            "op": "insert" if key in b_prov else "delete",
                            "old_html": None if key in b_prov else html_view._units_html(units),
                            "new_html": html_view._units_html(units) if key in b_prov else None,
                            "old_at": None if key in b_prov else _where(units[0] if units else None),
                            "new_at": _where(units[0] if units else None) if key in b_prov else None,
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
