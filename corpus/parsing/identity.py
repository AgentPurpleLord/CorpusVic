"""A name for a provision that says what it is, not where it sits.

A reviewer's work is attached to provisions, and until now that
attachment was a position in the parse's node list. A position means
nothing once the parse changes: re-parse an Act with one line read
differently and every provision after it shifts, taking somebody else's
verification with it.

So a provision is named the way a lawyer names it -- by its place in the
Act rather than in the file:

    pt2/div1/s97            Part 2, Division 1, section 97
    s97/d/i                 section 97(d)(i)
    s15/definition-injury/a paragraph (a) of the definition of "injury"

Two provisions can still come out with the same name, almost always
because the parse made two of one -- a citation that wrapped onto its own
line and was read as a fresh subsection. The second and any after it
carry a short digest of their own wording, so they are still told apart
and the first keeps the plain name.
"""
import hashlib
import re

from corpus.domain.hierarchy import HIERARCHY_ORDER, make_ranks
from corpus.parsing.extract import reflow
from corpus.parsing.tree import annotate_paths

# How a level is written. The bracket-numbered levels write bare --
# "s97/d/i" reads as section 97(d)(i), which is how the provision is
# cited -- and everything else carries a short prefix so the name can be
# read back without knowing the hierarchy.
_PREFIX = {
    "schedule": "sch",
    "chapter": "ch",
    "part": "pt",
    "division": "div",
    "subdivision": "subdiv",
    "section": "s",
    "clause": "cl",
    "item": "item",
}
_BARE = {"subsection", "subclause", "subitem", "paragraph", "subparagraph", "sub_subparagraph"}

# Enough of the wording to tell two provisions apart without being
# disturbed by an edit to the rest of it.
_DIGEST_CHARS = 120


def _slug(value) -> str:
    """A number or heading as it can appear in a name. Dots survive
    because Part numbers use them ("Part 2.1")."""
    return re.sub(r"[^A-Za-z0-9.]+", "-", str(value)).strip("-").lower()[:40]


def _segment(level: str, value) -> str:
    if level == "dictionary":
        return "dict"   # there is only ever one
    if level in _BARE:
        return _slug(value)
    if level in _PREFIX:
        return f"{_PREFIX[level]}{_slug(value)}"
    return f"{level}-{_slug(value)}"


def _digest(node: dict) -> str:
    raw = "\x1f".join([
        node.get("type") or "",
        node.get("heading") or "",
        reflow(node.get("text") or "")[:_DIGEST_CHARS],
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]


def _base_id(node: dict, ranks: dict) -> str:
    """The provision's name before any duplicate is dealt with.

    Built from node["path"], the breadcrumb annotate_paths leaves on
    every node, walked in depth order rather than in the order the
    breadcrumb happens to store its keys. Those differ: a definition
    shares a subsection's depth (see hierarchy.make_ranks) but is kept
    last in the breadcrumb, and reading it last put a Definitions
    section's several "(a)" lists under one name.
    """
    path = node.get("path") or {}
    own_rank = ranks.get(node.get("type"))
    above = sorted((level for level, value in path.items() if value and level in ranks),
                   key=lambda level: (ranks[level], level == "definition"))

    parts = []
    for level in above:
        # Stop at the node's own depth: what follows is the node itself,
        # or something it contains.
        if own_rank is not None and (ranks[level] > own_rank
                                     or (ranks[level] == own_rank and level == node.get("type"))):
            break
        parts.append(_segment(level, path[level]))

    if node.get("number"):
        parts.append(_segment(node["type"], node["number"]))
    elif node.get("heading"):
        # A defined term is named by the term, a topical heading by its
        # words -- neither carries a number of its own.
        parts.append(f"{node['type']}-{_slug(node['heading'])}")
    else:
        # A note, an example, a repealed run: nothing but its wording
        # distinguishes it from the next one.
        parts.append(f"{node['type']}~{_digest(node)}")
    return "/".join(parts)


def _former_name(name: str) -> str:
    """The name a Schedule's provision had before Schedules numbered
    clauses and items (issue #72): "sch1/cl2/a" was "sch1/s2/a"."""
    segments = name.split("/")
    for i, segment in enumerate(segments):
        if segment.startswith("sch"):
            return "/".join(segments[:i + 1] + [re.sub(r"^(?:cl|item)(?=[0-9])", "s", s) for s in segments[i + 1:]])
    return name


def name_index(names) -> dict:
    """{name: index} from (index, name) pairs, answering to a Schedule
    provision's former name too, so review work recorded under it still
    finds the provision it was about. A name in use wins over a former
    one."""
    index_of = {}
    pairs = list(names)
    for index, name in pairs:
        index_of[name] = index
    for index, name in pairs:
        index_of.setdefault(_former_name(name), index)
    return index_of


def node_ids(nodes: list[dict], hierarchy_order: "list[str] | None" = None) -> list[str]:
    """One name per node, in the order the nodes are given.

    Every name is unique within the document. The first node to claim a
    name keeps it, so repairing a parse that produced a duplicate leaves
    the provision that was right where it was.
    """
    order = hierarchy_order or HIERARCHY_ORDER
    if any("path" not in node for node in nodes):
        annotate_paths(nodes, order)
    ranks = make_ranks(order)

    ids, taken = [], set()
    for node in nodes:
        name = disambiguate(_base_id(node, ranks), taken, node)
        taken.add(name)
        ids.append(name)
    return ids


def annotate_ids(nodes: list[dict], hierarchy_order: "list[str] | None" = None) -> list[dict]:
    """Put each node's name on it, as node["id"]."""
    for node, name in zip(nodes, node_ids(nodes, hierarchy_order)):
        node["id"] = name
    return nodes


def duplicated(nodes: list[dict], hierarchy_order: "list[str] | None" = None) -> list[tuple[int, str]]:
    """The nodes whose name had to be qualified, and what they became.

    Worth looking at: a provision that needed qualifying is usually one
    the parse duplicated rather than two provisions the Act really
    numbers the same.
    """
    return [(index, name) for index, name in enumerate(node_ids(nodes, hierarchy_order))
            if "~" in name.rsplit("/", 1)[-1] and not name.rsplit("/", 1)[-1].startswith(("note~", "example~"))]


def inserted_id(anchor: str, node: dict) -> str:
    """The name of a provision a reviewer added, which no parse contains.

    An inserted node has no place in the parse and so no path to build a
    name from. What it does have is the provision it was put after, so it
    is named against that: "s97/d+note-1aa". The anchor is itself a name,
    so an insert after an insert nests the same way.
    """
    if node.get("number"):
        own = _segment(node.get("type") or "node", node["number"])
    elif node.get("heading"):
        own = f"{node.get('type') or 'node'}-{_slug(node['heading'])}"
    else:
        own = f"{node.get('type') or 'node'}~{_digest(node)}"
    return f"{anchor}+{own}"


def disambiguate(name: str, taken: set, node: dict) -> str:
    """A name nothing has claimed yet, starting from the one built for it.

    The wording is tried first, because it is what actually differs
    between two provisions the parse gave one name. Where that is the
    same too -- two runs of asterisks marking two repeals -- nothing on
    the page tells them apart and order is all that is left.
    """
    if name not in taken:
        return name
    suffixed = f"~{_digest(node)}"
    qualified = name if name.endswith(suffixed) else name + suffixed
    if qualified not in taken:
        return qualified
    ordinal = 2
    while f"{qualified}-{ordinal}" in taken:
        ordinal += 1
    return f"{qualified}-{ordinal}"


def inserted_ids(names: list, inserts) -> dict:
    """Names for the provisions a reviewer added to a document.

    `names` is the parse's own names, in order. `inserts` is
    (index, after_index, node) for each added provision.

    Resolved by what each one follows rather than in index order,
    because those are not the same: a reviewer who adds a provision and
    then adds another one *above* it gives the second a higher index and
    the first a higher anchor. Taking them in index order left the first
    with no anchor to hang off. Each pass names every insert whose anchor
    is known; the passes stop when one adds nothing.

    Returns {index: name}. An insert whose anchor is the front of the
    document, or is gone, or is part of a cycle, hangs off "inserted".
    """
    by_index, taken = {}, set(names)
    pending = list(inserts)
    while pending:
        ready = [row for row in pending
                 if (row[1] is not None and 0 <= row[1] < len(names)) or row[1] in by_index]
        if not ready:
            # Nothing left can reach an anchor, so the rest hang off the
            # front rather than being left unnamed.
            ready = pending
        for index, after, node in sorted(ready):
            if after is not None and 0 <= after < len(names):
                anchor = names[after]
            else:
                anchor = by_index.get(after, "inserted")
            name = disambiguate(inserted_id(anchor, node), taken, node)
            taken.add(name)
            by_index[index] = name
        done = {row[0] for row in ready}
        pending = [row for row in pending if row[0] not in done]
    return by_index


def document_ids(nodes: list, edits: "dict | None" = None,
                 hierarchy_order: "list[str] | None" = None) -> dict:
    """{index: name} for a document as a reviewer sees it -- the parse,
    plus whatever they have added to it.

    `edits` is corpus.storage.db.load_structure_edits' shape,
    {index: {"after", "deleted", "node"}}.
    """
    names = node_ids(nodes, hierarchy_order)
    by_index = {index: name for index, name in enumerate(names)}
    inserts = sorted(
        (index, edit.get("after"), edit["node"])
        for index, edit in (edits or {}).items()
        if edit.get("node") is not None and index >= len(names)
    )
    by_index.update(inserted_ids(names, inserts))
    return by_index
