"""
The legislative hierarchy: the ordered list of container levels (Chapter,
Part, Division, ...), and helpers for reasoning about which level is
deeper than which.

The default order below covers every Victorian Act checked so far. A
particular Act can override it from its own profile (a `hierarchy:` list
in ai_pipeline/profiles/<act-slug>.yaml) -- for example, the Criminal
Procedure Act groups its Parts under Chapters, so its profile puts
"chapter" on top and adds a `chapter:` pattern. See profiles.py's
`load_hierarchy`.

Because the order can differ between Acts, code that walks a parsed Act
should take the resolved order as a parameter, rather than assuming the
default (the rule parser gets it from the profile; run_pipeline.py saves
it into data/ai_parsed/<act>.json so the exporters can read it back
without reloading the profile). The module-level HIERARCHY_ORDER /
HIERARCHY_RANK / HEADING_LEVELS below are just the defaults, used when a
node list doesn't carry its own hierarchy.

"schedule" sits above "chapter" in this list, not nested under it: a
Schedule doesn't belong to any enclosing Part or Division the way the
rest of the Act's structure does -- it's a separate, self-contained block
that comes after the Act's main Parts/Divisions/Sections end, and starts
its own numbering from 1 again (see basic-structure.yaml's own notes on
this). Putting it at the very top of the list means starting a new
Schedule always closes out whatever Chapter/Part/Division/.../Section
was still open, which is exactly right. Its own numbered items reuse the
ordinary section/subsection/paragraph/subparagraph types rather than
getting Schedule-specific ones of their own -- real Schedules number
their clauses "in the same way as sections" (again see basic-
structure.yaml), so the existing patterns already recognise most
Schedule content with no extra code needed. Anything that doesn't match
(a reprinted treaty with its own numbering, say) just becomes plain
Schedule text, the same way any other unrecognised line does elsewhere
in this parser.
"sub_subparagraph" -- bracketed capital letters, "(A)", "(B)" -- is the
rarest of these levels (drafters avoid it where they can), but real Acts
do use it in sections that have been heavily amended.
"""

HIERARCHY_ORDER = [
    "schedule",
    "chapter",
    "part",
    "division",
    "subdivision",
    "section",
    "subsection",
    "paragraph",
    "subparagraph",
    "sub_subparagraph",
]


# The two types that sit at the same depth as a section: an Act's
# "section" and a Bill's (or Explanatory Memorandum's) "clause" -- see
# make_ranks below, which treats the second as the same depth as the
# first. Anywhere that asks "is this a top-level provision?" (which gets
# its own page in the browse view and its own file in the Markdown
# export, and doesn't repeat its number as a heading on that page) has
# to accept both, or a Bill/EM would browse as an empty document.
SECTION_LEVEL_TYPES = ("section", "clause")


def make_ranks(order: list[str]) -> dict[str, int]:
    """level -> its position in `order` (0 = shallowest). "clause" is
    mapped onto "section"'s own depth, since a Bill's "clause" is just
    the pre-enactment name for what an Act calls a "section" (see the
    comment above HEADING_LEVELS below for more). "definition" is mapped
    onto "subsection"'s depth: a defined term inside a Definitions or
    Interpretation section (see rule_parser.py's _try_definition_start)
    sits at exactly the same depth a numbered subsection would -- a new
    definition closes out whatever bracketed list belonged to the
    previous one, without closing the Section itself. It just isn't
    numbered, so it can't literally share the "subsection" node type the
    way a Bill's clause shares "section"'s.

    Returning a dict rather than just using `order.index(...)` makes the
    very common "is level A shallower or deeper than level B?" check a
    quick lookup instead of scanning the whole list each time."""
    ranks = {level: i for i, level in enumerate(order)}
    if "section" in ranks:
        ranks["clause"] = ranks["section"]
    if "subsection" in ranks:
        ranks["definition"] = ranks["subsection"]
        # A continuation resumes the sentence its provision opened with,
        # after that provision's own list has finished, so it sits at the
        # same depth the list items do -- inside the provision, beside
        # them, and after them. Like a definition it carries no number of
        # its own, so it can't share the "subsection" type outright.
        ranks["continuation"] = ranks["subsection"]
    return ranks


def heading_levels(order: list[str]) -> set[str]:
    """The levels whose headings are printed bold, in a distinct size, in
    the source PDF (see rule_parser.py's module docstring) -- everything
    from the top of the hierarchy down to and including "section" (plus
    "clause", a Bill's own name for that same level), as opposed to
    subsection/paragraph/subparagraph, which are never bold."""
    if "section" in order:
        levels = set(order[: order.index("section") + 1])
        levels.add("clause")
        return levels
    return set(order)


# Defaults, for a node list that carries no hierarchy of its own.
HIERARCHY_RANK = make_ranks(HIERARCHY_ORDER)
# "clause" is a Bill's own name for the same top-level numbered provision
# an Act calls a "section" -- same shape, same nesting depth
# (subsection/paragraph/subparagraph nest under either one identically),
# just the term used before a Bill is enacted (see rule_parser.py's
# top_level_type parameter, used when parsing a Bill instead of an Act).
# It's mapped onto section's own depth rather than given a new one of its
# own, since the two are never open at the same time -- a document is
# either an Act or a Bill, never both -- and giving it a separate depth
# would wrongly let one nest inside the other.
# (make_ranks/heading_levels apply this for a particular Act's own
# hierarchy too; the module-level defaults above just do it once here.)
HEADING_LEVELS = heading_levels(HIERARCHY_ORDER)

# A Section's (or a Bill's Clause's -- same nesting depth, see
# HIERARCHY_RANK above) own content runs from itself up to, but not
# including, the next node of one of these types. That's the "everything
# nested under this Section/Clause" grouping that review.py's
# group_into_units, link_targets.py's definition-index builder, and
# bill_linking.py's full-text reconstruction all need on their own. It
# lives here, not copied into each of them, for the same reason
# HIERARCHY_ORDER itself does. This stays fixed no matter what order a
# particular Act's profile puts these levels in -- a Chapter, Part,
# Division, Subdivision, Section, Clause or Schedule heading always
# closes off whatever came before it. Schedule only marks a boundary
# here, it isn't a root: it doesn't gather the numbered items nested
# under it into one review unit the way a Section does. Each of those
# items is its own ordinary "section"-type node (see rule_parser.py's
# note on why Schedule reuses that type instead of getting its own), so
# it already starts its own unit through UNIT_ROOT_TYPES below with no
# extra help needed here.
UNIT_BOUNDARY_TYPES = {"schedule", "chapter", "part", "division", "subdivision", "section", "clause", "heading_group"}
UNIT_ROOT_TYPES = {"section", "clause"}


def group_into_units(nodes: list[dict]) -> list[list[int]]:
    """Splits a node list into the "review units" a human works through
    one at a time: a Section (or a Bill's Clause) together with every
    subsection, paragraph, definition and note nested under it, plus a
    standalone unit for each Chapter/Part/Division heading in between.
    Returns node *positions*, in order, covering every node exactly
    once.

    Every other node type marks a boundary that starts its own single-
    node unit (and, for Part/Division/Subdivision/heading_group,
    immediately ends it too). This matches exactly how the rules engine
    nests things as it parses -- see rule_parser.py's HIERARCHY_ORDER --
    without needing to build the full tree (build_hierarchy_tree in
    akn_export.py) just to find "everything under this Section": the
    flat node list is already in document order, so one pass through it
    is enough.

    This lives here rather than in review.py because it's about how the
    units are laid out, not about the review GUI: run_pipeline.py needs
    it to reattach stored review work after a re-parse (see
    ai_pipeline/reparse.py), and shouldn't have to import a FastAPI app
    just to get it."""
    units: list[list[int]] = []
    current: list[int] | None = None
    for i, node in enumerate(nodes):
        t = node["type"]
        if t in UNIT_ROOT_TYPES:
            current = [i]
            units.append(current)
        elif t in UNIT_BOUNDARY_TYPES:
            current = None
            units.append([i])
        elif current is not None:
            current.append(i)
        else:
            units.append([i])
    return units


def schedule_numbers(nodes: list[dict]) -> list["str | None"]:
    """Which Schedule each node sits in, by its position in the list --
    the Schedule's own number, or None for a node in the document's main
    body.

    A Schedule starts numbering its own provisions from 1 again (an
    Act's Schedule items are numbered "in the same way as sections", per
    acts/profiles/basic-structure.yaml), so a number alone doesn't
    identify a provision: the Criminal Procedure Act has both a section
    11 and a Schedule 1 clause 11, and its Bill and Explanatory
    Memorandum each have both too. Anything matching provisions between
    documents needs to use this as well as the number, or every Schedule
    provision would collide with the body provision sharing its number.

    If a node already records its own Schedule, that's trusted as-is (an
    Explanatory Memorandum's entries carry one, since its Schedule
    headings are plain heading_groups rather than proper container nodes
    -- see em_parser.py). Otherwise, the enclosing Schedule is whichever
    "schedule" node came most recently before it.
    """
    out: list[str | None] = []
    current: str | None = None
    for node in nodes:
        if node["type"] == "schedule":
            current = node.get("number")
        out.append(node.get("schedule") or current)
    return out


def _subtree_has_section_level(tree_node: dict) -> bool:
    """Whether a Section- or Clause-type node appears anywhere below this
    tree node, at any depth -- used only by schedule_is_pageable."""
    for child in tree_node["children"]:
        if child["node"]["type"] in SECTION_LEVEL_TYPES or _subtree_has_section_level(child):
            return True
    return False


def schedule_is_pageable(tree_node: dict) -> bool:
    """Whether this Schedule gets a page of its own in the browse view
    and the Markdown export, the way an ordinary Section always does.
    False for anything that isn't a Schedule.

    A Schedule whose own items are ordinary numbered provisions is
    already fully covered by each of those items' own pages: real
    Schedules number their clauses "in the same way as sections", and
    those items reuse the plain Section/Clause node type rather than
    getting one of their own (see UNIT_BOUNDARY_TYPES's note on why) --
    Schedule 1 of the Criminal Procedure Act, say, has a "section 1"
    that sits right alongside the body's own section 1, just in a
    different Schedule.

    A Schedule whose content is unnumbered prose sitting directly on its
    own node -- or spread across a run of un-numbered heading_group/note
    children beneath it -- doesn't get a page anywhere otherwise, not in
    the browse view and not in the Markdown export. Schedule 3 of the
    Criminal Procedure Act ("Persons who may witness statements in
    preliminary brief, full brief or hand-up brief") and Schedule 1 of
    the Evidence Act ("Style changes") are both like this, and both used
    to be unreachable by anything except the Act's own AKN export, which
    writes out the whole tree in one go rather than splitting it into
    pages.
    """
    if tree_node["node"]["type"] != "schedule":
        return False
    return not _subtree_has_section_level(tree_node)
