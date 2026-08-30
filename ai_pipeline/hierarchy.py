"""
The legislative hierarchy: the ordered list of container levels, and the
helpers for reasoning about relative depth.

The default order below covers every Victorian Act checked so far. An
individual Act can override it from its profile (a `hierarchy:` list in
ai_pipeline/profiles/<act-slug>.yaml) -- e.g. the Criminal Procedure Act
groups its Parts under Chapters, so its profile puts "chapter" on top and
adds a `chapter:` pattern. See profiles.py's `load_hierarchy`.

Because the order can vary per Act, code that walks a parsed Act should
take the resolved order as a parameter (the rule parser threads it in from
the profile; run_pipeline.py persists it into data/ai_parsed/<act>.json so
the exporters can read it back without re-loading the profile). The
module-level HIERARCHY_ORDER / HIERARCHY_RANK / HEADING_LEVELS are the
defaults, used for the AI-engine path and as the fallback when a node
list carries no hierarchy of its own.
"""

HIERARCHY_ORDER = [
    "chapter",
    "part",
    "division",
    "subdivision",
    "section",
    "subsection",
    "paragraph",
    "subparagraph",
]


def make_ranks(order: list[str]) -> dict[str, int]:
    """level -> its index in `order` (0 = shallowest). The dict form of the
    ordering, for the very common "is level A shallower/deeper than level
    B" test -- a lookup instead of an O(n) list.index() scan."""
    return {level: i for i, level in enumerate(order)}


def heading_levels(order: list[str]) -> set[str]:
    """The levels whose headings are set bold at a distinct font size in
    the source PDF (see rule_parser.py's module docstring) -- everything
    from the top of the hierarchy down to and including "section", as
    opposed to subsection/paragraph/subparagraph, which are never bold."""
    if "section" in order:
        return set(order[: order.index("section") + 1])
    return set(order)


# Defaults, for the AI-engine path and as a fallback.
HIERARCHY_RANK = make_ranks(HIERARCHY_ORDER)
HEADING_LEVELS = heading_levels(HIERARCHY_ORDER)
