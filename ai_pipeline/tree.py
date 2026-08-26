"""
Reconstructs hierarchy from the AI's flat, ordered node list (rather than
asking the model to emit nested JSON or explicit parent paths, which is more
error-prone) and attaches parsed amendment-history notes to the node they
belong to.
"""
from .history_notes import collect_page_notes

HIERARCHY_ORDER = ["part", "division", "subdivision", "section", "subsection", "paragraph", "subparagraph"]


def annotate_paths(nodes: list[dict]) -> list[dict]:
    """Adds a node["path"] breadcrumb (e.g. {"part": "I", "division": "1",
    "section": "3", "subsection": "(2)", ...}) to every node, by tracking the
    most recent number seen at each hierarchy level and resetting deeper
    levels whenever a shallower one changes."""
    current = {level: None for level in HIERARCHY_ORDER}
    for node in nodes:
        t = node.get("type")
        if t in HIERARCHY_ORDER:
            idx = HIERARCHY_ORDER.index(t)
            current[t] = node.get("number")
            for deeper in HIERARCHY_ORDER[idx + 1 :]:
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


def _find_by_number(candidates: list[dict], number: str, types: set[str]) -> dict | None:
    for node in candidates:
        if node.get("number") == number and node.get("type") in types:
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


def attach_history(nodes: list[dict], pages) -> list[dict]:
    """Attaches parsed margin notes to the most specific matching node's
    node["history"] list. Notes that can't be matched to any node are
    returned separately for manual follow-up, not discarded."""
    annotate_paths(nodes)
    section_runs = _runs_by(nodes, "section")
    division_runs = _runs_by(nodes, "division")
    part_runs = _runs_by(nodes, "part")

    unattached = []
    for note in collect_page_notes(pages):
        target = None
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
                if target is None:
                    target = candidates[0]
        elif note["division"]:
            candidates = division_runs.get(note["division"], [])
            if candidates:
                if note["sub_path"]:
                    target = _find_by_number(candidates, note["sub_path"][-1], {"subdivision"})
                if target is None:
                    target = candidates[0]
        elif note["part"]:
            candidates = part_runs.get(note["part"], [])
            if candidates:
                target = candidates[0]

        if target is not None:
            target.setdefault("history", []).append(note)
        else:
            unattached.append(note)

    return unattached
