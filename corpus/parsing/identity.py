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
}
_BARE = {"subsection", "paragraph", "subparagraph", "sub_subparagraph"}

# Enough of the wording to tell two provisions apart without being
# disturbed by an edit to the rest of it.
_DIGEST_CHARS = 120


def _slug(value) -> str:
    """A number or heading as it can appear in a name. Dots survive
    because Part numbers use them ("Part 2.1")."""
    return re.sub(r"[^A-Za-z0-9.]+", "-", str(value)).strip("-").lower()[:40]


def _segment(level: str, value) -> str:
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
