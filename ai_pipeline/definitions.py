"""
Finds defined terms, by pattern rather than any certain method, so
markdown_export.py can hyperlink a term back to where it's defined --
the AustLII-style linked-definitions idea. A term counts as "defined"
under either of two conditions:

  1. It's introduced by "term means ..." / "has the same meaning ..." /
     "includes ..." inside a section whose heading suggests it defines
     terms ("Definitions", "Interpretation").
  2. Anywhere at all, a clause says "term has the same meaning as in
     section N" -- Victorian Acts sometimes point a term's meaning at a
     specific section instead of (or in addition to) a Definitions clause,
     e.g. the Crimes Act pointing at section 325 for a term used only in
     one place. This one doesn't need the enclosing section to look like a
     definitions section at all -- referencing another section by number is
     itself the signal -- and the term should link to *that* section, not
     wherever the pointer happens to sit.

This works from text patterns, not the PDF's own font info (italics
aren't captured at extraction time), so it's a navigation aid, not a
guarantee that every definition gets found -- it looks for the standard
drafting conventions above and will miss anything phrased unusually.
Missing a term just means one missed hyperlink, not lost or wrong text
-- the underlying document is unaffected either way.
"""
import re

_DEFINITIONS_HEADING_RE = re.compile(r"definition|interpretation", re.IGNORECASE)

_TERMS = r'"?[A-Za-z][\w \'()/-]*?"?(?:\s*,\s*"?[A-Za-z][\w \'()/-]*?"?)*(?:\s+and\s+"?[A-Za-z][\w \'()/-]*?"?)?'

# A definition clause can define more than one term at once, e.g.
# "custodial officer, emergency worker on duty and emergency worker have
# the same meanings as in section 10AA ...". This captures that whole
# subject (possibly several terms, separated by commas or "and") up to
# the word that introduces the definition.
#
# Checked on every line (re.MULTILINE), not just at the very start of the
# text: a "Definitions" section's clauses often end up joined into one
# block of text on one node ("abortion has the meaning ...;\nchild
# means\n(a) ..." -- see the module docstring), with each clause still
# starting its own line even though the rule parser didn't split them
# into separate nodes. A definition whose list of paragraphs lives on
# *separate* nodes (rather than inline in the same clause) still won't
# be caught this way -- putting a multi-node list back together is out
# of scope here, same as it is for the rule parser itself.
_DEF_RE = re.compile(
    # "means" not followed by "of" rules out the common phrase "by means
    # of ..." -- without this, "by" would get parsed as a one-word
    # defined term. "has"/"have" both appear in real Act text -- a
    # compound subject ("X, Y and Z have the same meanings as ...") takes
    # the plural verb, same as "means"/"mean" would.
    rf"^(?P<terms>{_TERMS})\s+(?:means\b(?!\s+of\b)|means,|(?:has|have)(?: the| its)? same meanings?\b|includes\b)",
    re.MULTILINE,
)

# A defined term in an Act is never just a plain word like "the" or
# "and" on its own -- this catches anything that slips through the
# patterns above by accident (e.g. the term pattern matching only the
# tail end of some other phrase).
_STOPWORDS = {
    "a", "an", "the", "by", "of", "in", "on", "at", "to", "for", "with",
    "and", "or", "but", "is", "are", "as", "it", "this", "that", "these",
    "those", "any", "each", "such", "means",
}

# The specific case of a term pointing to another section: same opening
# shape as above, but requires "same meaning(s) as in section N" and
# captures N -- except when it's really "section N of the/that ... Act",
# which names a section of a *different* Act (as in "firearm has the
# same meaning as in section 3(1) of the Firearms Act 1996"), not this
# one. This allows for an optional pinpoint cite ("(1)") between the
# number and the "of ... Act" part that gives it away, for the same
# reason as markdown_export.py's own check for section references in
# prose.
_SAME_MEANING_SECTION_RE = re.compile(
    rf"^(?P<terms>{_TERMS})\s+(?:has|have)(?: the| its)? same meanings?\s+as\s+in\s+section\s+(?P<section>\d+[A-Za-z]*)\b"
    r"(?!(?:\s*\([^)]*\))?\s+of\s+(?:the|that|any)\b)",
    re.MULTILINE | re.IGNORECASE,
)

_MAX_TERM_WORDS = 6


def looks_like_definitions_section(heading: str | None) -> bool:
    return bool(heading and _DEFINITIONS_HEADING_RE.search(heading))


def is_definition_start(line: str) -> bool:
    """Does this one line start a new "term means ..." clause? Used to
    split a Definitions section's text back into separate clauses (they
    often arrive joined into one block of text -- see the module and
    rule_parser notes) without breaking apart a clause that simply
    wrapped onto a second printed line."""
    return bool(_DEF_RE.match(line.strip()))


def _split_terms(raw: str) -> list[str]:
    raw = raw.strip().strip('"')
    parts = re.split(r"\s*,\s*|\s+and\s+", raw)
    terms = (p.strip().strip('"').lower() for p in parts if p.strip())
    return [t for t in terms if t not in _STOPWORDS]


def extract_terms(text: str) -> list[str]:
    """Every term this node's text defines, one match per line that opens
    with the "term means ..." convention."""
    terms = []
    for m in _DEF_RE.finditer((text or "").strip()):
        terms.extend(t for t in _split_terms(m.group("terms")) if t and len(t.split()) <= _MAX_TERM_WORDS)
    return terms


def extract_section_ref_terms(text: str) -> list[tuple[list[str], str]]:
    """[(terms, referenced_section_number), ...] for every "term has the
    same meaning as in section N" clause, wherever it appears -- unlike
    extract_terms, this doesn't care what the enclosing section's
    heading says."""
    results = []
    for m in _SAME_MEANING_SECTION_RE.finditer((text or "").strip()):
        terms = [t for t in _split_terms(m.group("terms")) if t and len(t.split()) <= _MAX_TERM_WORDS]
        if terms:
            results.append((terms, m.group("section")))
    return results


def split_definition_clauses(text: str) -> list[str]:
    """Splits a node's text into one chunk per "term means ..." clause
    (plus a leading chunk for any lead-in text before the first clause,
    e.g. "In this Part—"), joining lines back together where they had
    simply wrapped within a clause. Used to show each clause as its own
    paragraph, instead of the whole node running together as one block,
    and to link a term to the specific clause that defines it rather
    than the top of a node that might hold several definitions."""
    chunks: list[str] = []
    current: list[str] = []
    for raw_line in (text or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if current and is_definition_start(line):
            chunks.append(" ".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append(" ".join(current))
    return chunks
