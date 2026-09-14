"""
Rebuilds the Chapter/Part/Division/Section tree from the parser's flat,
ordered list of nodes, and attaches each parsed amendment-history note to
the node it belongs to. No AI model is asked to produce a nested tree or
parent references directly -- that's more error-prone than building the
tree ourselves from a flat list we already trust.
"""
from .hierarchy import HIERARCHY_ORDER, make_ranks, schedule_numbers
from .history_notes import collect_page_notes


def annotate_paths(nodes: list[dict], hierarchy_order: list[str] = HIERARCHY_ORDER) -> list[dict]:
    """Adds a node["path"] breadcrumb (e.g. {"part": "I", "division": "1",
    "section": "3", "subsection": "(2)", ...}) to every node. It works by
    remembering the most recent number seen at each level, and clearing
    out anything deeper whenever a shallower level changes.

    "definition" is the odd one out: a defined term is identified by its
    heading, not a number (see rule_parser.py's _try_definition_start --
    a defined term has no legislative number of its own). If we used
    node.get("number") for it like every other level, it would always be
    None, and every paragraph nested under a defined term would have no
    record of which term it actually belongs to. That used to make a
    Definitions section's own sub-paragraphs impossible to tell apart in
    review.py -- several different terms' own "(a)"/"(b)" lists all
    looked the same. See compute_unit_labels, which reads this same field
    back to tell them apart.

    "continuation" is the other odd one out, for the opposite reason: it
    is not a level at all, so it never *starts* a context -- it inherits
    the one it resumes. See its own branch below.

    "definition" also needs its own separate clearing rule below, because
    it doesn't get a real slot in hierarchy_order (it shares subsection's
    rank instead -- see make_ranks), so the generic loop that clears
    deeper levels never mentions it by name. Without a rule of its own,
    a Definitions section anywhere in the Act would leave its last term
    stuck in path["definition"] for every later section's subsections,
    for the rest of the document -- there's usually no later Definitions
    section to overwrite it. So it's cleared explicitly here, whenever
    anything at or above its own level starts (a new section, or a real
    numbered subsection rather than a defined term)."""
    rank = make_ranks(hierarchy_order)
    definition_rank = rank.get("definition")
    current = {level: None for level in hierarchy_order}
    for node in nodes:
        t = node.get("type")
        if t == "continuation":
            # A continuation is not a level of its own: it resumes the
            # provision whose list just closed, so its path is that
            # provision's. depth_rank is where the parser actually put it
            # (see rule_parser's _consume_as_continuation); its *type's*
            # rank is only a default, and reading that instead cleared
            # levels it sits inside -- a wrap-up under s 11(1)(b)'s list
            # lost the (b), and one inside a defined term lost the term,
            # leaving both labelled as though they belonged to nothing.
            effective = node.get("depth_rank")
            if effective is None:
                effective = rank[t]
            for deeper in hierarchy_order[effective:]:
                current[deeper] = None
            node["path"] = dict(current)
            continue
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
    """Attaches each parsed margin note to the most specific node it
    matches, adding it to that node's node["history"] list. Notes that
    don't match any node are returned separately for manual follow-up,
    not thrown away."""
    annotate_paths(nodes, hierarchy_order)
    section_runs = _runs_by(nodes, "section")
    division_runs = _runs_by(nodes, "division")
    part_runs = _runs_by(nodes, "part")
    chapter_runs = _runs_by(nodes, "chapter")

    # Which Schedule each node sits in, so "Sch. 1 cl. 4A" reaches clause
    # 4A of Schedule 1, not section 4A of the main body -- a Schedule
    # starts numbering its own provisions from 1 again.
    schedules = schedule_numbers(nodes)
    schedule_roots = {
        node.get("number"): node for node in nodes if node["type"] == "schedule" and node.get("number")
    }
    in_schedule: dict[str, list[dict]] = {}
    for idx, node in enumerate(nodes):
        if schedules[idx]:
            in_schedule.setdefault(schedules[idx], []).append(node)

    unattached = []
    for note in collect_page_notes(pages):
        target = None
        if note.get("kind") == "provenance":
            # Says where the provision came from, not how it changed --
            # it doesn't name any provision of this Act to attach to.
            # Kept for the reviewer to see, but never counted as a
            # failed link.
            unattached.append(note)
            continue
        # A note only counts as "confidence: high" when either (a) it
        # doesn't name anything more specific than the section/division/
        # part itself, or (b) it names something more specific and we
        # actually found that exact node. Falling back to a broader node
        # because the specific one couldn't be found is a guess -- so
        # it's tagged "low" rather than shown as equally reliable.
        wanted_specific = bool(note["sub_path"] or note["def_name"])
        found_specific = False

        if note.get("schedule"):
            root = schedule_roots.get(note["schedule"])
            if root is not None:
                target = root
                if note["section"]:
                    # A clause of the Schedule. Schedule items reuse the
                    # "section" type (see rule_parser.py), so this is the
                    # same lookup, just narrowed to that Schedule's own
                    # nodes.
                    within = in_schedule.get(note["schedule"], [])
                    found = _find_by_number(within, note["section"], {"section", "clause"})
                    if found is not None:
                        target = found
                        found_specific = True
                    wanted_specific = True
        elif note["section"]:
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
        elif note.get("chapter"):
            # An Act that groups its Parts under Chapters cites just the
            # Chapter for a note about the Chapter's own heading:
            # "Ch. 10 (Heading and s. 439) inserted by No. 68/2009 s. 55."
            candidates = chapter_runs.get(note["chapter"], [])
            if candidates:
                target = candidates[0]

        if target is not None:
            note["confidence"] = "high" if (found_specific or not wanted_specific) else "low"
            target.setdefault("history", []).append(note)
        else:
            unattached.append(note)

    return unattached
