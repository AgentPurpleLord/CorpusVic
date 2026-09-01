"""
Reconstructs hierarchy from the AI's flat, ordered node list (rather than
asking the model to emit nested JSON or explicit parent paths, which is more
error-prone) and attaches parsed amendment-history notes to the node they
belong to.
"""
from .hierarchy import HIERARCHY_ORDER, make_ranks
from .history_notes import collect_page_notes


def annotate_paths(nodes: list[dict], hierarchy_order: list[str] = HIERARCHY_ORDER) -> list[dict]:
    """Adds a node["path"] breadcrumb (e.g. {"part": "I", "division": "1",
    "section": "3", "subsection": "(2)", ...}) to every node, by tracking the
    most recent number seen at each hierarchy level and resetting deeper
    levels whenever a shallower one changes.

    "definition" is the one level identified by its heading rather than a
    number (see rule_parser.py's _try_definition_start -- a defined term
    has no legislative numbering of its own): using node.get("number")
    for it the way every other level does would just be None every time,
    so every paragraph/subparagraph nested under a defined term would
    silently carry path["definition"] = None forever, with nothing
    recording which definition they actually belong to. That's exactly
    the "wonky" labelling a Definitions section's own paragraphs used to
    get in review.py (several different terms' own "(a)"/"(b)" lists are
    all indistinguishable without this) -- see compute_unit_labels, which
    reads this same field back to disambiguate them.

    "definition" also needs its *own* reset rule, separate from the
    hierarchy_order-indexed loop below: because it's aliased onto
    subsection's own rank (see make_ranks) rather than getting a literal
    slot in hierarchy_order, that loop's `hierarchy_order[rank[t] + 1:]`
    slice never actually names "definition" as one of the keys it clears.
    Left alone, a Definitions section anywhere in the Act would leak its
    last term into path["definition"] for every following section's own
    subsections for the rest of the document -- there's no later
    "definition" node to overwrite it, since a Definitions section is
    usually the only one. Cleared here instead, explicitly, whenever
    anything at or shallower than that same rank opens (a new section, or
    a genuine numbered subsection instead of a defined term)."""
    rank = make_ranks(hierarchy_order)
    definition_rank = rank.get("definition")
    current = {level: None for level in hierarchy_order}
    for node in nodes:
        t = node.get("type")
        if t in rank:
            if t == "definition":
                current["definition"] = node.get("heading")
            else:
                current[t] = node.get("number")
                if definition_rank is not None and rank[t] <= definition_rank:
                    current["definition"] = None
            for deeper in hierarchy_order[rank[t] + 1 :]:
                current[deeper] = None
        node["path"] = dict(current)
    return nodes


def _runs_by(nodes: list[dict], level: str) -> dict:
    runs: dict[str, list[dict]] = {}
    for node in nodes:
        key = node["path"].get(level)
        if key is not None:
            runs.setdefault(key, []).append(node)
    return runs


def _normalize_number(s: str | None) -> str:
    return (s or "").strip("()").lower()


def _find_by_number(candidates: list[dict], number: str, types: set[str]) -> dict | None:
    target = _normalize_number(number)
    for node in candidates:
        if _normalize_number(node.get("number")) == target and node.get("type") in types:
            return node
    return None


def _find_definition(candidates: list[dict], def_name: str) -> dict | None:
    target = " ".join(def_name.lower().replace("-", " ").split())
    for node in candidates:
        if node.get("type") != "definition":
            continue
        heading = " ".join((node.get("heading") or "").lower().replace("-", " ").split())
        if heading and (heading == target or target in heading or heading in target):
            return node
    return None


def attach_history(nodes: list[dict], pages, hierarchy_order: list[str] = HIERARCHY_ORDER) -> list[dict]:
    """Attaches parsed margin notes to the most specific matching node's
    node["history"] list. Notes that can't be matched to any node are
    returned separately for manual follow-up, not discarded."""
    annotate_paths(nodes, hierarchy_order)
    section_runs = _runs_by(nodes, "section")
    division_runs = _runs_by(nodes, "division")
    part_runs = _runs_by(nodes, "part")

    unattached = []
    for note in collect_page_notes(pages):
        target = None
        # A note is only "confidence: high" when either (a) it names no
        # deeper reference and lands on the section/division/part itself, or
        # (b) it names one and we found that exact node. Falling back to a
        # broader node because the specific one couldn't be found is a
        # guess -- tag it "low" rather than presenting it as equally solid.
        wanted_specific = bool(note["sub_path"] or note["def_name"])
        found_specific = False

        if note["section"]:
            candidates = section_runs.get(note["section"], [])
            if candidates:
                if note["def_name"]:
                    target = _find_definition(candidates, note["def_name"])
                if target is None and note["sub_path"]:
                    sub_path = list(note["sub_path"])
                    while sub_path and target is None:
                        target = _find_by_number(
                            candidates, sub_path[-1], {"subsection", "paragraph", "subparagraph"}
                        )
                        sub_path.pop()
                found_specific = target is not None
                if target is None:
                    target = candidates[0]
        elif note["division"]:
            candidates = division_runs.get(note["division"], [])
            if candidates:
                if note["sub_path"]:
                    target = _find_by_number(candidates, note["sub_path"][-1], {"subdivision"})
                found_specific = target is not None
                if target is None:
                    target = candidates[0]
        elif note["part"]:
            candidates = part_runs.get(note["part"], [])
            if candidates:
                target = candidates[0]

        if target is not None:
            note["confidence"] = "high" if (found_specific or not wanted_specific) else "low"
            target.setdefault("history", []).append(note)
        else:
            unattached.append(note)

    return unattached
