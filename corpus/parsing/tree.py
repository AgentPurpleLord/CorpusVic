"""
Rebuilds the Chapter/Part/Division/Section tree from the parser's flat,
ordered list of nodes, and attaches each parsed amendment-history note to
the node it belongs to. No AI model is asked to produce a nested tree or
parent references directly -- that's more error-prone than building the
tree ourselves from a flat list we already trust.
"""
import re

from corpus.domain.hierarchy import HIERARCHY_ORDER, make_ranks, schedule_numbers
from corpus.parsing.history_notes import collect_page_notes


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
                # A Preamble has no number; its recitals sit under it all
                # the same.
                current[t] = node.get("number") or ({"preamble": "preamble", "dictionary": "dictionary"}.get(t))
                if definition_rank is not None and rank[t] <= definition_rank:
                    current["definition"] = None
            for deeper in hierarchy_order[rank[t] + 1 :]:
                current[deeper] = None
            # The types sharing a level's depth without a slot of their own
            # -- "clause", "item", "preamble" -- close like it, or a
            # Schedule's clause stayed in the path of everything after.
            # A definition is not one of them: it can sit inside a
            # subsection (s 4(6)) as well as beside one.
            for other in list(current):
                if t != "definition" and other not in (t, "definition") and rank.get(other, -1) >= rank[t] and (
                        other not in hierarchy_order or t not in hierarchy_order):
                    current[other] = None
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


# The levels a citation's bracketed tail can name, in order, so that
# "(1)(c)" reads as subsection 1, paragraph c.
_SUB_LEVELS = ("subsection", "paragraph", "subparagraph", "sub_subparagraph")


def _inside(node: dict, sub_path: list[str]) -> bool:
    """Whether this node sits at or below the provision a citation's
    bracketed tail names.

    At or below, not exactly at: a note printed under s 6(1) follows that
    subsection's last paragraph, so the parser records it as sitting
    inside paragraph (c). The citation says "s. 6(1)" and means that note
    all the same, so the levels it does not mention must not be required
    to be empty.

    The brackets are matched in order but not by position, because which
    level a bracket names depends on the provision rather than on where
    it sits in the citation: s 6 numbers subsections and "(1)" is one,
    while s 119 has no subsections at all and its "(c)" is a paragraph
    hanging straight off the section. Reading the first bracket as a
    subsection either way put every note in s 119 out of reach."""
    path = node.get("path") or {}
    remaining = [_normalize_number(path.get(level)) for level in _SUB_LEVELS if path.get(level)]
    for number in sub_path:
        wanted = _normalize_number(number)
        if wanted not in remaining:
            return False
        remaining = remaining[remaining.index(wanted) + 1:]
    return True


def _levels(node: dict) -> list[str]:
    path = node.get("path") or {}
    return [_normalize_number(path.get(level)) for level in _SUB_LEVELS if path.get(level)]


def _find_provision(candidates: list[dict], sub_path: list[str]) -> "dict | None":
    """The provision a citation's bracketed tail names, by the whole tail.

    Matching the last bracket alone sent "S. 124(4)(c) repealed" to
    s 124(3)(c) and "S. 124(3)(a) amended" to (1AA)(a): every subsection
    has its own (a). Levels are matched in order, not by position, for
    the reason _inside gives."""
    wanted = [_normalize_number(n) for n in sub_path]
    for node in candidates:
        if node.get("type") in _SUB_LEVELS and _normalize_number(node.get("number")) == wanted[-1] \
                and _levels(node) == wanted:
            return node
    return None


# A citation whose last word is that the provision was repealed:
# "S. 124(6) inserted by No. 55/2014 s. 109(4), repealed by No. 38/2022".
_REPEALED_RE = re.compile(r"\brepealed by\b[^,]*$", re.IGNORECASE)

_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _roman(token: str) -> int:
    total = 0
    for a, b in zip(token, token[1:] + " "):
        total += -_ROMAN[a] if _ROMAN.get(b, 0) > _ROMAN[a] else _ROMAN[a]
    return total


def _before(a: str, b: str) -> bool:
    """Does provision number `a` come before `b` in a run? As drafted:
    7, 7A, 8; b, ba, c; iii, iv -- numerals by value only where one of
    the two has more than one letter, since (c) and (d) read the same
    either way."""
    da, db = re.match(r"(\d+)(.*)", a), re.match(r"(\d+)(.*)", b)
    if da and db:
        return (int(da.group(1)), da.group(2)) < (int(db.group(1)), db.group(2))
    if re.fullmatch(r"[ivxlcdm]+", a) and re.fullmatch(r"[ivxlcdm]+", b) and max(len(a), len(b)) > 1:
        return _roman(a) < _roman(b)
    return a < b


def _claim_repealed(candidates: list[dict], sub_path: list[str], claimed: set, number_it: bool = True,
                    inside: bool = False) -> "dict | None":
    """The row of stars printed where a repealed provision stood.

    A repeal leaves no number on the page to find it by, only the row. Its
    place is the row under the same provision whose nearest earlier
    sibling comes closest before the repealed number: after (b) for a
    repealed (c). The row takes the number -- unless what was repealed is
    the provision's note, which is not the provision -- and is claimed, so
    the next note looking for a row does not take the same one.

    `inside` looks for the row within the provision itself, after its
    last piece: where a provision's note was printed, and repealed."""
    wanted = [_normalize_number(n) for n in sub_path]
    parent, number = (wanted, None) if inside else (wanted[:-1], wanted[-1])
    best, best_prev = None, None
    for node in candidates:
        if node.get("type") != "repealed" or id(node) in claimed:
            continue
        levels = _levels(node)
        if levels[:len(parent)] != parent or (inside and len(levels) == len(parent)):
            continue
        prev = levels[len(parent)] if len(levels) > len(parent) else None
        if prev is not None and number is not None and not _before(prev, number):
            continue
        if best is None or (prev is not None and (best_prev is None or _before(best_prev, prev))):
            best, best_prev = node, prev
    if best is not None:
        claimed.add(id(best))
        if number_it:
            best["number"] = number
    return best


def _claim_repealed_section(section_runs: dict, number: str, claimed: set) -> "dict | None":
    """The row where a repealed section stood: among the rows closing the
    section before it, the first not yet claimed -- their notes come in
    printed order, 375 before 375A."""
    prev = None
    for k in section_runs:
        if _before(_normalize_number(k), _normalize_number(number)) and (
                prev is None or _before(_normalize_number(prev), _normalize_number(k))):
            prev = k
    if prev is None:
        return None
    run = section_runs[prev]
    tail = len(run)
    while tail and run[tail - 1].get("type") == "repealed":
        tail -= 1
    for node in run[tail:]:
        if id(node) not in claimed:
            claimed.add(id(node))
            node["number"] = number
            return node
    return None


def _find_annotation(candidates: list[dict], sub_path: list[str], kind: str, wanted_id) -> tuple:
    """The note or example a citation like "Note to s. 6(1)" is about.

    These citations are about the note printed under a provision, not
    about the provision -- "Note to s. 6(1) substituted as Notes" records
    a change to s 6(1)'s note, while s 6(1) itself says what it always
    said. Attaching it to the subsection put the history of the note onto
    the provision that carries it.

    Where the citation numbers the note ("Note 1 to s. 55(4)") that is
    the answer. Where it does not and the provision has just one, so is
    that. Where it does not and the provision has several -- an
    amendment that turned one note into two, or "Notes to s. 41 amended"
    -- nothing in the citation says which, so the first is returned as a
    guess.

    Returns (node, certain). `certain` is False for that guess, so the
    caller can mark it low-confidence and a reviewer can re-point it,
    rather than it sitting among the matches that are actually known."""
    found = [n for n in candidates if n.get("type") == kind and _inside(n, sub_path)]
    if wanted_id:
        numbered = [n for n in found if _normalize_number(n.get("number")) == _normalize_number(wanted_id)]
        if numbered:
            return numbered[0], True
    if not found:
        return None, False
    return found[0], len(found) == 1


def _term(name: str) -> str:
    """A defined term as it can be compared: hyphens dropped, with any
    space a line break left after one -- a margin note breaks
    "correspon- ding" and "non- disclosure" alike, and only the second
    hyphen is the term's -- case and spacing ignored."""
    return " ".join(re.sub(r"-\s*", "", name).lower().split())


def _find_definition(candidates: list[dict], def_name: str, leading: bool = False) -> dict | None:
    """The definition of exactly this term. One term containing the other
    is not enough: "Family Violence Court Division" is not "court".

    `leading` also takes a term that begins the cited one, for a term the
    parse read short where it wrapped ("litigation restraint order" for
    "... order proceeding") -- a guess, asked only once nothing else fits."""
    target = _term(def_name)
    for node in candidates:
        if node.get("type") == "definition" and node.get("heading"):
            term = _term(node["heading"])
            if term == target or (leading and target.startswith(term + " ")):
                return node
    return None


def _as_printed(def_name: str, words: set) -> str:
    """A term from a margin note, with each word the note broke at a line
    end ("correspon- ding", "overseas- registered") put back as the Act
    spells it: joined where the Act has the joined word, hyphenated where
    it does not."""
    def join(m):
        whole = m.group(1) + m.group(2)
        return whole if whole.lower() in words else f"{m.group(1)}-{m.group(2)}"
    return re.sub(r"(\w+)- (\w+)", join, def_name).strip()


def _words(nodes: list[dict]) -> set:
    return {w.lower() for n in nodes for w in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*",
                                                         f"{n.get('heading') or ''} {n.get('text') or ''}")}


# "def. of associated defendant amended as associated accused": the term
# was renamed, and is printed under its new name.
_RENAMED_RE = re.compile(r"\bamended as (.+?) by\b")


def _claim_repealed_definition(candidates: list[dict], def_name: str, claimed: set,
                               words: "set | None" = None) -> "dict | None":
    """The row of stars where a repealed definition stood: after the term
    that comes closest before it alphabetically, as definitions are set.
    The row takes the term, so the reader sees which definition went."""
    target = _term(def_name)
    best, best_prev = None, None
    for node in candidates:
        if node.get("type") != "repealed" or id(node) in claimed:
            continue
        prev = _term((node.get("path") or {}).get("definition") or "")
        if prev and prev >= target:
            continue
        if best is None or (prev and (best_prev is None or prev > best_prev)):
            best, best_prev = node, prev
    if best is not None:
        claimed.add(id(best))
        best["heading"] = _as_printed(def_name, words or set())
    return best


def _dictionary_target(nodes: list[dict], note: dict, claimed: set, words: set) -> tuple:
    """(node, found_specific, wanted_specific) for a note citing an Act's
    Dictionary: a defined term of one of its Parts, or a clause of one --
    the Part itself where neither is found."""
    part = _normalize_number(note.get("part"))
    within = [n for n in nodes if (n.get("path") or {}).get("dictionary")
              and _normalize_number((n.get("path") or {}).get("part")) == part]
    root = next((n for n in within if n["type"] == "part"), None)
    target = None
    if note.get("def_name"):
        target = _find_definition(within, note["def_name"])
        renamed = _RENAMED_RE.search(note["raw"])
        if target is None and renamed:
            target = _find_definition(within, renamed.group(1))
        if target is None and _REPEALED_RE.search(note["raw"]):
            target = _claim_repealed_definition(within, note["def_name"], claimed, words)
        if target is not None:
            return target, True, True
        target = _find_definition(within, note["def_name"], leading=True)
    elif note.get("section"):
        clause = _find_by_number(within, note["section"], {"clause"})
        if clause is not None:
            run = [n for n in within if (n.get("path") or {}).get("clause") == clause.get("number")]
            specific = _find_provision(run, note["sub_path"]) if note["sub_path"] else clause
            if specific is not None:
                return specific, True, True
            target = clause
    return target or root, False, True


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
    claimed: set = set()   # repealed rows already given to a note
    words = _words(nodes)   # how the Act spells a word a margin note broke
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

        if note.get("dictionary"):
            target, found_specific, wanted_specific = _dictionary_target(nodes, note, claimed, words)
        elif note.get("schedule"):
            root = schedule_roots.get(note["schedule"])
            if root is not None:
                target = root
                if note["section"]:
                    # A clause of the Schedule. Schedule items reuse the
                    # "section" type (see rule_parser.py), so this is the
                    # same lookup, just narrowed to that Schedule's own
                    # nodes.
                    within = in_schedule.get(note["schedule"], [])
                    found = _find_by_number(within, note["section"], {"section", "clause", "item", "subclause", "subitem"})
                    if found is not None:
                        target = found
                        found_specific = True
                    wanted_specific = True
        elif note["section"]:
            candidates = section_runs.get(note["section"], [])
            if candidates:
                if note.get("target_kind"):
                    # "Note to s. 6(1)", "Example to s. 43A(2)" -- about
                    # what is printed under the provision, not about the
                    # provision. A note the citation says was repealed is
                    # no longer there to attach to, so this can come back
                    # empty and fall through to the provision below.
                    target, found_specific = _find_annotation(
                        candidates, note["sub_path"], note["target_kind"], note.get("target_id")
                    )
                    # Naming the note is itself the specific thing asked
                    # for, whether or not the citation also gave a
                    # subsection.
                    wanted_specific = True
                if target is None and note["def_name"]:
                    target = _find_definition(candidates, note["def_name"])
                    renamed = _RENAMED_RE.search(note["raw"])
                    if target is None and renamed:
                        target = _find_definition(candidates, renamed.group(1))
                    if target is None and _REPEALED_RE.search(note["raw"]):
                        target = _claim_repealed_definition(candidates, note["def_name"], claimed, words)
                    found_specific = target is not None
                    if target is None:
                        target = _find_definition(candidates, note["def_name"], leading=True)
                if target is None and note["sub_path"]:
                    repealed = _REPEALED_RE.search(note["raw"])
                    if repealed and note.get("target_kind"):
                        target = _claim_repealed(candidates, note["sub_path"], claimed, number_it=False, inside=True)
                    if target is None:
                        target = _find_provision(candidates, note["sub_path"])
                    if target is None and repealed:
                        target = _claim_repealed(candidates, note["sub_path"], claimed,
                                                 number_it=not note.get("target_kind"))
                    found_specific = target is not None and not note.get("target_kind")
                    # Not found: the provision it sits in, never a namesake
                    # under another -- and a guess, so low confidence.
                    sub_path = list(note["sub_path"][:-1])
                    while target is None and sub_path:
                        target = _find_provision(candidates, sub_path)
                        sub_path.pop()
                if target is None:
                    target = candidates[0]
            elif _REPEALED_RE.search(note["raw"]) and not note["sub_path"]:
                target = _claim_repealed_section(section_runs, note["section"], claimed)
                found_specific = target is not None
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
