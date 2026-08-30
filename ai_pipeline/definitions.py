"""
Heuristically extracts defined terms so markdown_export.py can hyperlink a
term back to where it's defined -- the AustLII-style linked-definitions
idea. A term counts as "defined" under either of two conditions:

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

This is pattern-based, not a re-run of the PDF's font info (italics aren't
captured at extraction time), so it's a navigation aid rather than a
legal-grade extraction: it looks for the standard drafting conventions
above and will miss anything phrased unusually. False negatives just mean
a missed hyperlink, not lost or corrupted text -- the underlying markdown
content is unaffected either way.
"""
import re

_DEFINITIONS_HEADING_RE = re.compile(r"definition|interpretation", re.IGNORECASE)

_TERMS = r'"?[A-Za-z][\w \'()/-]*?"?(?:\s*,\s*"?[A-Za-z][\w \'()/-]*?"?)*(?:\s+and\s+"?[A-Za-z][\w \'()/-]*?"?)?'

# A definition clause can define more than one term at once, e.g.
# "custodial officer, emergency worker on duty and emergency worker have
# the same meanings as in section 10AA ...". Capture the (possibly
# multi-term, comma/and-separated) subject up to the defining verb.
#
# Applied per-line (re.MULTILINE, matched at the start of any line within a
# node's text), not just at the very start of the text: a "Definitions"
# section's un-numbered clauses often end up concatenated into one node's
# text blob ("abortion has the meaning ...;\nchild means\n(a) ...— see the
# module docstring), each still starting its own source line even though
# the rule parser didn't split them into separate nodes. A definition whose
# list of paragraphs lives in *sibling* nodes (rather than inline in the
# same clause) still won't be caught this way -- multi-node list
# reconstruction is out of scope here, same as it is for the rule parser's
# own node model.
_DEF_RE = re.compile(
    # "means" not followed by "of" specifically excludes the common idiom
    # "by means of ..." ("by" preceding "means" would otherwise parse as a
    # one-word defined term "by"). "has"/"have" both appear in real Act
    # text -- a compound subject ("X, Y and Z have the same meanings as
    # ...") takes the plural verb, same as "means"/"mean" would.
    rf"^(?P<terms>{_TERMS})\s+(?:means\b(?!\s+of\b)|means,|(?:has|have)(?: the| its)? same meanings?\b|includes\b)",
    re.MULTILINE,
)

# Legislative defined terms are never a bare closed-class function word --
# this catches anything that slips through the phrasing patterns above by
# accident (e.g. a term regex matching just the tail of a larger idiom).
_STOPWORDS = {
    "a", "an", "the", "by", "of", "in", "on", "at", "to", "for", "with",
    "and", "or", "but", "is", "are", "as", "it", "this", "that", "these",
    "those", "any", "each", "such", "means",
}

# The specific "references a Section" sub-case: same leading shape, but
# requires "same meaning(s) as in section N" and captures N -- except when
# it's actually "section N of the/that ... Act", which names a *different*
# Act's section (as in "firearm has the same meaning as in section 3(1) of
# the Firearms Act 1996"), not this Act's. The lookahead tolerates an
# optional pinpoint cite ("(1)") sitting between the number and the "of ...
# Act" tell, same reasoning as markdown_export.py's own section-reference
# guard for prose mentions.
_SAME_MEANING_SECTION_RE = re.compile(
    rf"^(?P<terms>{_TERMS})\s+(?:has|have)(?: the| its)? same meanings?\s+as\s+in\s+section\s+(?P<section>\d+[A-Za-z]*)\b"
    r"(?!(?:\s*\([^)]*\))?\s+of\s+(?:the|that|any)\b)",
    re.MULTILINE | re.IGNORECASE,
)

_MAX_TERM_WORDS = 6


def looks_like_definitions_section(heading: str | None) -> bool:
    return bool(heading and _DEFINITIONS_HEADING_RE.search(heading))


def is_definition_start(line: str) -> bool:
    """Does this single line open a new "term means ..." clause? Used to
    re-paragraph a Definitions section's text (its clauses commonly arrive
    concatenated into one text blob -- see the module/rule_parser notes)
    without breaking apart clauses that merely wrapped onto a second
    printed line."""
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
    same meaning as in section N" clause, wherever it occurs -- unlike
    extract_terms, not gated on the enclosing section's heading."""
    results = []
    for m in _SAME_MEANING_SECTION_RE.finditer((text or "").strip()):
        terms = [t for t in _split_terms(m.group("terms")) if t and len(t.split()) <= _MAX_TERM_WORDS]
        if terms:
            results.append((terms, m.group("section")))
    return results


def split_definition_clauses(text: str) -> list[str]:
    """Splits a node's text into one chunk per "term means ..." clause
    (plus a leading chunk for any lead-in text before the first clause,
    e.g. "In this Part—"), re-joining lines that merely wrapped within a
    clause. Used both to render each clause as its own paragraph (instead
    of the whole node collapsing into one run-on line) and to anchor a
    term's link at the specific clause that defines it rather than the top
    of a node that might hold several."""
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
