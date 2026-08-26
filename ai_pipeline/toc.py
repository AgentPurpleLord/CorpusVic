"""Detects where an Act's front-matter (title page + Table of Provisions)
ends and the substantive body begins, so run_pipeline.py doesn't need a
hand-found --start-page for every new Act. Best-effort: it's a starting
point you can always override, not a guarantee.

The Table of Provisions is laid out as a table (section number / title /
page number in separate columns), so its rows don't have a reliable text
shape to match against -- but its column header ("Section" / "Page",
repeated at the top of every TOC page, or a literal "TABLE OF PROVISIONS")
does. That marker is more robust than trying to pattern-match row content.
"""
import re

from .extract import PageText

_TOC_MARKER_RE = re.compile(r"^(table of provisions|section|contents)$", re.IGNORECASE)


def detect_body_start(pages: list[PageText]) -> int:
    """Returns the 1-indexed page number where real content likely starts,
    by scanning the leading run of pages carrying a Table-of-Provisions
    marker near the top."""
    body_start = 1
    for page in pages:
        lines = [l.text.strip() for l in page.body_lines if l.text.strip()]
        if not lines:
            body_start = page.page_no + 1
            continue
        toc_like = any(_TOC_MARKER_RE.match(t) for t in lines[:10])
        if toc_like:
            body_start = page.page_no + 1
        else:
            break
    return body_start
