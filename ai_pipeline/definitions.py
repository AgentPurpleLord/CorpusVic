"""
Heuristically extracts defined terms from an Act's "Definitions"/
"Interpretation" section(s), so markdown_export.py can hyperlink a term
back to where it's defined -- the AustLII-style linked-definitions idea.

This is pattern-based, not a re-run of the PDF's font info (italics aren't
captured at extraction time), so it's a navigation aid rather than a
legal-grade extraction: it looks for the standard drafting convention
"term means ..." / "term has the same meaning as ..." / "term includes ..."
at the start of a paragraph inside a section whose heading suggests it
defines terms, and will miss anything phrased unusually. False negatives
just mean a missed hyperlink, not lost or corrupted text -- the underlying
markdown content is unaffected either way.
"""
import re

_DEFINITIONS_HEADING_RE = re.compile(r"definition|interpretation", re.IGNORECASE)

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
    r'^"?(?P<terms>[A-Za-z][\w \'()/-]*?"?(?:\s*,\s*"?[A-Za-z][\w \'()/-]*?"?)*'
    r'(?:\s+and\s+"?[A-Za-z][\w \'()/-]*?"?)?)\s+'
    r"(?:means\b|means,|has(?: the| its)? same meanings?\b|includes\b)",
    re.MULTILINE,
)

_MAX_TERM_WORDS = 6


def looks_like_definitions_section(heading: str | None) -> bool:
    return bool(heading and _DEFINITIONS_HEADING_RE.search(heading))


def _split_terms(raw: str) -> list[str]:
    raw = raw.strip().strip('"')
    parts = re.split(r"\s*,\s*|\s+and\s+", raw)
    return [p.strip().strip('"').lower() for p in parts if p.strip()]


def extract_terms(text: str) -> list[str]:
    """Every term this node's text defines, one match per line that opens
    with the "term means ..." convention."""
    terms = []
    for m in _DEF_RE.finditer((text or "").strip()):
        terms.extend(t for t in _split_terms(m.group("terms")) if t and len(t.split()) <= _MAX_TERM_WORDS)
    return terms
