"""How much of a document a human has checked, provision by provision.

One answer for the public site ("12 of 112 provisions checked") and the
dashboard's cards. The dashboard used to count the unit review would
resume at, which is a position: a re-parse, a merge or an older row set
moved it, so the figure changed under edits that checked nothing.
"""


def approved_units(nodes: list, units: list[list[int]]) -> set[int]:
    """Which units a reviewer has actually approved -- positions into
    `units`, for the effective nodes review.build_effective_nodes_indexed
    returns (a merged-away node is None there, and doesn't count against
    the unit it used to be in).

    Approved means every node still in the unit carries verified_at and
    none is flagged for follow-up. The two are deliberately exclusive in
    review.py: flagging a piece means "not sure, revisit this", and
    commit_unit leaves such a node unstamped on purpose. So a flagged
    provision counts as unchecked and says so on its own page, which is
    the point of the reviewer having flagged it."""
    approved = set()
    for u, unit in enumerate(units):
        live = [nodes[i] for i in unit if nodes[i] is not None]
        if live and all(n.get("verified_at") and not n.get("needs_followup") for n in live):
            approved.add(u)
    return approved


def approved_page_slugs(nodes: list, units: list[list[int]], by_node_index: dict[int, str]) -> set[str]:
    """The page ids (build_page_index's own "s14", "s14_2", ...) whose
    provision a human has checked. A page is one unit -- a Section and
    everything nested under it -- so it is checked exactly when that unit
    is. Every page carries its text either way; this decides which of
    them have to say they haven't been confirmed."""
    approved = approved_units(nodes, units)
    unit_of_root = {unit[0]: u for u, unit in enumerate(units)}
    return {
        page for node_index, page in by_node_index.items()
        if unit_of_root.get(node_index) in approved
    }


def provisions_checked(nodes: list, units: list[list[int]], by_node_index: dict[int, str]) -> tuple[int, int]:
    """(checked, total) provisions -- a provision being one page: a
    Section, a Schedule's clause or item, a Schedule that is one page of
    prose, a Preamble."""
    pages = set(by_node_index.values())
    return len(approved_page_slugs(nodes, units, by_node_index) & pages), len(pages)
