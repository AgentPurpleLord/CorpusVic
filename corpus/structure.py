"""
Structural edits a reviewer makes that the parse itself cannot express:
inserting a node that isn't in the PDF's own text, deleting one that
shouldn't be there, and moving one to where it actually belongs.

Everything else in this pipeline identifies a node by its *position* in
data/parsed/<act>.json -- verified rows, link spans, blind reviews, AI
suggestions and scan findings all key on that integer, and
review.positions_are_trustworthy exists precisely because a re-parse can
invalidate it. So the one thing these edits must never do is renumber
anything: a node's index is its name, and adding, removing or reordering
nodes has to leave every other name alone.

Hence the split here between *identity* and *order*. The parse's own
indices, 0..parse_len-1, keep their meaning forever. An inserted node
takes the next index above parse_len, which no parse position can ever
collide with. And document order is not the index order at all: it is
rebuilt from a small set of edits, each saying either "this node is
gone" or "this node now follows that one".

An edit's anchor is the node it follows, DOCUMENT_START for the very
front. Anchoring rather than storing an absolute position is what keeps
a move stable: insert three nodes elsewhere afterwards and a node
anchored to s 5 is still directly after s 5, with no stored ordinal to
go stale.

Nothing here does I/O or knows about FastAPI: db.py persists the edits,
review.py applies them, and this module is the rule for what they mean.
"""

DOCUMENT_START = -1


class StructureError(ValueError):
    """An edit that would produce an order no reader could follow -- a
    node placed after itself, or after a node that already follows it."""


def _reject_cycles(edits: "dict[int, dict]") -> None:
    """A move can put a node after one that already follows it, and the
    two then have no order at all. Caught here, before anything is built,
    so an endpoint can try an edit and refuse it rather than persist a
    document that cannot be laid out.

    Walked with a memo of chains already proved sound, so checking every
    node costs one pass over the anchors rather than one per node."""
    settled: set[int] = set()
    for start in edits:
        if start in settled:
            continue
        chain: list[int] = []
        seen: set[int] = set()
        index = start
        while index is not None and index != DOCUMENT_START and index not in settled:
            if index in seen:
                raise StructureError(
                    f"node {index} would end up placed after itself, through a chain of moves"
                )
            seen.add(index)
            chain.append(index)
            edit = edits.get(index)
            index = edit.get("after") if edit else None
        settled.update(chain)


def document_order(parse_len: int, edits: "dict[int, dict]") -> list[int]:
    """Every live node's index, in reading order.

    `edits` maps a node index to {"after": int | None, "deleted": bool,
    "node": dict | None} -- "after" being its anchor (DOCUMENT_START for
    the front of the document, None to leave it where the parse put it),
    and a non-None "node" marking an index that was inserted rather than
    parsed.

    A deleted node keeps its place in the chain even though it is left
    out of the result: anything anchored to it still has somewhere to be,
    rather than quietly disappearing along with it.
    """
    _reject_cycles(edits)
    anchored: dict[int, list[int]] = {}
    deleted: set[int] = set()
    for index in sorted(edits):
        edit = edits[index]
        if edit.get("deleted"):
            deleted.add(index)
        anchor = edit.get("after")
        if anchor is not None:
            anchored.setdefault(anchor, []).append(index)
    placed = {index for children in anchored.values() for index in children}

    order: list[int] = []
    visited: set[int] = set()

    def emit(root: int) -> None:
        # An explicit stack rather than recursion: a reviewer who moves a
        # long run of provisions one at a time builds a chain as long as
        # the run, and Python's recursion limit is not a sensible bound
        # on how much restructuring an Act is allowed to need.
        stack = [root]
        while stack:
            index = stack.pop()
            visited.add(index)
            if index not in deleted:
                order.append(index)
            stack.extend(reversed(anchored.get(index, [])))

    for index in anchored.get(DOCUMENT_START, []):
        emit(index)
    for index in range(parse_len):
        if index not in placed:
            emit(index)
    # A node anchored to an index that does not exist at all -- which no
    # endpoint allows, but a hand-edited database could still contain. It
    # goes to the end rather than nowhere: a provision in an odd place is
    # a visible problem, a silently dropped one is not.
    for index in sorted(edits):
        if index not in visited and index not in deleted:
            order.append(index)
            visited.add(index)
    return order


def next_index(parse_len: int, edits: "dict[int, dict]") -> int:
    """The index to give a newly inserted node: above every parse
    position and above every index already handed out, including ones
    since deleted -- an index is a name, and a name is not reused."""
    return max([parse_len - 1, *edits], default=-1) + 1


def is_live(index: int, parse_len: int, edits: "dict[int, dict]") -> bool:
    """Whether this index names a node the document currently has."""
    edit = edits.get(index)
    if edit is not None and edit.get("deleted"):
        return False
    if 0 <= index < parse_len:
        return True
    return edit is not None and edit.get("node") is not None


def place_after(
    edits: "dict[int, dict]", index: int, anchor: int, order: "list[int] | tuple" = ()
) -> "dict[int, dict]":
    """`edits` with `index` placed *directly* after `anchor` -- a copy, so
    a caller can try it against document_order before committing to it.

    Not just "set its anchor and be done": an anchor can already have
    something hanging off it, and then two nodes claim the same place and
    the tie is broken by index, which is not an order anyone asked for.
    Moving a definition up one behind a definition already anchored there
    silently did nothing at all. So the chain is spliced rather than
    written to:

      * whatever followed `index` is re-hung on what `index` followed, so
        taking it out doesn't drag its followers along or leave them
        pointing at a node that has moved elsewhere;
      * whatever was directly after `anchor` is re-hung on `index`, so
        `index` genuinely goes between them.

    `order` is the current document order, needed only to know what
    `index` follows today; a node being inserted for the first time isn't
    in it yet and doesn't need to be.
    """
    updated = {k: dict(v) for k, v in edits.items()}
    order = list(order)
    if index in order:
        position = order.index(index)
        predecessor = order[position - 1] if position else DOCUMENT_START
        for other, edit in updated.items():
            if other != index and edit.get("after") == index:
                edit["after"] = predecessor
    for other, edit in updated.items():
        if other != index and edit.get("after") == anchor:
            edit["after"] = index
    updated.setdefault(index, {"after": None, "deleted": False, "node": None})["after"] = anchor
    return updated


def with_edit(edits: "dict[int, dict]", index: int, **fields) -> "dict[int, dict]":
    """`edits` with one node's entry updated -- a copy, so a caller can
    try an edit against document_order and find out whether it is legal
    before committing to it."""
    updated = {k: dict(v) for k, v in edits.items()}
    entry = updated.setdefault(index, {"after": None, "deleted": False, "node": None})
    entry.update(fields)
    return updated
