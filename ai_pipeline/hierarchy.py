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
defaults, used as the fallback when a node list carries no hierarchy of
its own.

"schedule" sits shallower than "chapter", not nested under it: a Schedule
doesn't belong to any enclosing Part/Division the way the rest of the
Act's own structure does -- it's a separate, self-contained sequence that
starts after the Act's substantive Parts/Divisions/Sections end and
restarts its own numbering from 1 (see basic-structure.yaml's own notes
on this). Putting it at the very top means opening one always closes out
whatever Chapter/Part/Division/.../Section was still open, which is
exactly right. Its own internal numbered items reuse the ordinary
section/subsection/paragraph/subparagraph types rather than getting
schedule-specific ones -- real Schedules number their own clauses "in the
same way as sections" (again see basic-structure.yaml), so the existing
patterns already recognise most Schedule content correctly with no extra
code; anything that doesn't match (a reprinted treaty's own numbering,
say) just falls back to being the Schedule's own plain text, same as any
other unrecognised line does everywhere else in this parser.
"sub_subparagraph" -- bracketed capital letters, "(A)", "(B)" -- is the
one level rarer than the rest (drafters avoid it where possible), but
real Acts do use it in heavily-amended sections.
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


# The two types that sit at section rank: an Act's "section" and a Bill's
# (or an Explanatory Memorandum's) "clause" -- see make_ranks below, which
# aliases the second onto the first. Anything that asks "is this a
# top-level provision?" -- which gets its own page in the browse view and
# its own file in the Markdown export, which types don't repeat their
# number as a heading inside their own page -- has to accept both, or a
# Bill/EM browses as an empty document.
SECTION_LEVEL_TYPES = ("section", "clause")


def make_ranks(order: list[str]) -> dict[str, int]:
    """level -> its index in `order` (0 = shallowest), plus "clause" mapped
    onto "section"'s own rank -- see the module-level HIERARCHY_RANK's own
    comment for why -- and "definition" mapped onto "subsection"'s: a
    defined term inside a Definitions/Interpretation section (see
    rule_parser.py's _try_definition_start) nests at exactly the same
    depth a numbered subsection would -- a fresh definition closes out
    whatever bracketed list belonged to the previous one, without closing
    the enclosing Section itself -- it just isn't numbered, so it can't
    literally share the "subsection" node type the way a Bill's clause
    shares "section"'s. The dict form of the ordering, for the very
    common "is level A shallower/deeper than level B" test -- a lookup
    instead of an O(n) list.index() scan."""
    ranks = {level: i for i, level in enumerate(order)}
    if "section" in ranks:
        ranks["clause"] = ranks["section"]
    if "subsection" in ranks:
        ranks["definition"] = ranks["subsection"]
    return ranks


def heading_levels(order: list[str]) -> set[str]:
    """The levels whose headings are set bold at a distinct font size in
    the source PDF (see rule_parser.py's module docstring) -- everything
    from the top of the hierarchy down to and including "section" (plus
    "clause", a Bill's own name for that same level -- see HIERARCHY_RANK's
    comment), as opposed to subsection/paragraph/subparagraph, which are
    never bold."""
    if "section" in order:
        levels = set(order[: order.index("section") + 1])
        levels.add("clause")
        return levels
    return set(order)


# Defaults, for a node list that carries no hierarchy of its own.
HIERARCHY_RANK = make_ranks(HIERARCHY_ORDER)
# "clause" is a Bill's own name for the same top-level numbered provision
# an Act calls a "section" -- same drafting shape, same nesting rank
# (subsection/paragraph/subparagraph nest under either identically), just
# the pre-enactment term (see rule_parser.py's top_level_type parameter,
# used to parse a Bill instead of an Act). Mapped onto section's own rank
# rather than getting the next free integer, since the two are never both
# open at once -- a document is either an Act or a Bill, never both -- and
# giving it a distinct rank would wrongly let one nest inside the other.
# (Handled by make_ranks/heading_levels themselves for a resolved per-Act
# hierarchy; the module-level defaults above get it the same way.)
HEADING_LEVELS = heading_levels(HIERARCHY_ORDER)

# A Section's (or a Bill's own Clause's -- same nesting rank, see
# HIERARCHY_RANK above) own body runs from itself up to (not including)
# the next node of one of these types -- the "everything nested under
# this Section/Clause" grouping review.py's group_into_units,
# link_targets.py's definition-index builder, and bill_linking.py's full-
# text reconstruction all need independently. Lives here, not duplicated
# in each, for the same reason HIERARCHY_ORDER itself does. Fixed
# regardless of an individual Act's own resolved hierarchy order -- a
# Chapter/Part/Division/Subdivision/Section/Clause/Schedule heading always
# closes off whatever came before it, whatever order a particular profile
# nests them in. Schedule is a boundary only, not a root: it doesn't
# itself absorb the numbered items nested under it into one review unit
# the way a Section does -- each of those is its own ordinary "section"-
# type node (see rule_parser.py's own note on why Schedule reuses that
# type rather than getting one of its own), so it already starts its own
# unit via UNIT_ROOT_TYPES below with no help needed here.
UNIT_BOUNDARY_TYPES = {"schedule", "chapter", "part", "division", "subdivision", "section", "clause", "heading_group"}
UNIT_ROOT_TYPES = {"section", "clause"}
