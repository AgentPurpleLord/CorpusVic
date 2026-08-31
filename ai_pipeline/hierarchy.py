"""
Single source of truth for the legislative hierarchy levels and their
nesting order.

The rule parser, tree reconstruction (tree.py), and both exporters
(akn_export.py, markdown_export.py) all need to know that a subsection
nests inside a section which nests inside a division, and to compare two
levels' relative depth. That list used to be copy-pasted into each of
those modules; it lives here now so a change to the hierarchy is one edit.

HIERARCHY_RANK is the same information as a dict, for the very common
"is level A shallower/deeper than level B" test -- a dict lookup instead
of the O(n) list.index() scan those comparisons were doing per node and
per stack frame.
"""

HIERARCHY_ORDER = [
    "part",
    "division",
    "subdivision",
    "section",
    "subsection",
    "paragraph",
    "subparagraph",
]

# level -> its index in HIERARCHY_ORDER (0 = shallowest).
HIERARCHY_RANK = {level: i for i, level in enumerate(HIERARCHY_ORDER)}

# "clause" is a Bill's own name for the same top-level numbered provision
# an Act calls a "section" -- same drafting shape, same nesting rank
# (subsection/paragraph/subparagraph nest under either identically), just
# the pre-enactment term (see rule_parser.py's top_level_type parameter,
# used to parse a Bill instead of an Act). Mapped onto section's own rank
# rather than getting the next free integer, since the two are never both
# open at once -- a document is either an Act or a Bill, never both -- and
# giving it a distinct rank would wrongly let one nest inside the other.
HIERARCHY_RANK["clause"] = HIERARCHY_RANK["section"]

# The levels whose headings are set bold at a distinct font size in the
# source PDF (see rule_parser.py's module docstring) -- as opposed to
# subsection/paragraph/subparagraph, which are never bold.
HEADING_LEVELS = {"part", "division", "subdivision", "section", "clause"}

# A Section's (or a Bill's own Clause's -- same nesting rank, see
# HIERARCHY_RANK above) own body runs from itself up to (not including)
# the next node of one of these types -- the "everything nested under
# this Section/Clause" grouping review.py's group_into_units,
# link_targets.py's definition-index builder, and bill_linking.py's full-
# text reconstruction all need independently. Lives here, not duplicated
# in each, for the same reason HIERARCHY_ORDER itself does.
UNIT_BOUNDARY_TYPES = {"part", "division", "subdivision", "section", "clause", "heading_group"}
UNIT_ROOT_TYPES = {"section", "clause"}
