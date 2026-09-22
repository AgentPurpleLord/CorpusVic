"""Which Act a provision's cross-references belong to.

Victorian drafting hands a whole list over to another Act in one lead-in
line and then never repeats the name:

    (b) an offence under any of the following provisions of the
        Crimes Act 1958—
        (i) Division 2 of Part I (other than sections 75, 75A, 76 ...);
       (ii) Division 2AA of Part I;

Every "section", "Division" and "Part" beneath (b) is a Crimes Act
reference. A linkifier reading one provision at a time cannot see that,
so it resolves them against the document it is rendering and links them
into the wrong Act -- which looks right and reads wrong, the worst kind
of error a legislation site can make (issue #57).

Deliberately narrow. A lead-in has to *end* with the Act's citation and
the dash that introduces the list, because the cost of being wrong here
is asymmetric: missing a lead-in leaves a reference unlinked, while
matching a passing mention hands a whole subtree to an Act that was only
named in passing.
"""
import re

from corpus.domain.act_registry import load_act_registry
from corpus.review.link_targets import load_known_acts

# A citation as it appears inline: capitalised words and the small
# connectives a long title carries, ending in "Act"/"Bill" and a year.
# Never trusted on its own -- a match is thrown away unless it is found
# word for word in known_acts.yaml or act_registry.json, so an
# over-matched span simply fails to resolve rather than resolving wrong.
#
# The (?-i:...) wrapper matters: callers compile this case-insensitively
# for the sake of the patterns it sits beside, and under that flag [A-Z]
# would match a lowercase letter too -- which is exactly the
# capitalisation check this pattern exists to make.
_ACT_TITLE_WORD = r"[A-Z][\w'()-]*"
_ACT_TITLE_CONNECTOR = r"(?:of|the|and|for|in|on|to|by|or)"
ACT_TITLE_SPAN_RE = (
    rf"(?-i:\b{_ACT_TITLE_WORD}(?:\s+(?:{_ACT_TITLE_WORD}|{_ACT_TITLE_CONNECTOR}))*"
    rf"\s+(?:Act|Bill)\s+(?:18|19|20)\d{{2}}\b)"
)

# "...of the Crimes Act 1958—" at the very end of the provision. The
# trailing dash or colon is required, not optional: it is what makes a
# line a lead-in rather than a sentence that happens to mention an Act.
_LEAD_IN_RE = re.compile(
    rf"(?:of|in|under)\s+the\s+({ACT_TITLE_SPAN_RE})\s*[\u2014\u2013:-]\s*$",
    re.IGNORECASE,
)


def governing_act(text: "str | None") -> "str | None":
    """The Act this provision hands its children to, or None.

    The title is returned exactly as the Act is cited, so callers can
    look it up the same way they look up any other citation."""
    if not text:
        return None
    match = _LEAD_IN_RE.search(text.strip())
    if not match:
        return None
    title = match.group(1)
    # Resolved or nothing. An unrecognised title is left to mean nothing
    # at all rather than become a guess about where its children point.
    known = {t.lower() for t in load_known_acts().values()}
    if title.lower() in known:
        return title
    registry = {t.lower(): t for t in load_act_registry()}
    return registry.get(title.lower()) and title


def scope_by_unit(units: list) -> list:
    """One entry per unit, in render order: the Act title governing that
    unit's cross-references, or None for the document being rendered.

    Units carry their nesting depth already (see
    markdown_export._iter_body_units), so scope is just a stack: a
    lead-in governs everything nested under it and nothing at its own
    level, which is what puts the next sibling back on this Act."""
    scopes = []
    stack: list = []   # (depth, title), outermost first
    for unit in units:
        depth = unit["depth"]
        while stack and depth <= stack[-1][0]:
            stack.pop()
        # Before pushing: a lead-in's own text names the Act outright, so
        # it is not governed by itself.
        scopes.append(stack[-1][1] if stack else None)
        title = governing_act(unit.get("text"))
        if title:
            stack.append((depth, title))
    return scopes
