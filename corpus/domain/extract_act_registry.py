"""
Extracts a comprehensive Victorian Act registry from the OCPC's own
"List of Acts in chronological order" (em/List-of-Acts-in-chronological-
order.pdf) -- a 213-page table of essentially every Act Victoria has
passed: Year / Act No. / Title / Repealed-by-Act-No. / Provision.

Usage:
    python extract_act_registry.py

Writes corpus/act_registry.json -- short title -> {year, act_no,
repealed_by, repealed_provision, in_force}, consulted by
corpus/act_registry.py as the fallback once known_acts.yaml itself
doesn't have an answer (see link_targets.resolve_act_citation and
bill_linking.resolve_em_links).

The table is laid out in fixed columns by x-position, but wrapped titles
(and occasionally wrapped "Repealed by"/"Provision" text) break any
fixed-row-height assumption -- a row's true boundary is "does this line
carry its own Act No.", the one column every genuine row has even when
its Year is blank (Year is only printed the first time it changes; every
subsequent Act from the same year leaves the cell blank, inheriting the
last one printed).
"""
import json
import re
from pathlib import Path

import pymupdf

from corpus import PROJECT_ROOT

# The source PDF is a project file at the repo root; the registry it
# builds belongs beside the module that loads it (act_registry.py), which
# is why only one of these two is anchored to PROJECT_ROOT.
PDF_PATH = PROJECT_ROOT / "em" / "List-of-Acts-in-chronological-order.pdf"
OUT_PATH = Path(__file__).parent / "act_registry.json"

# Column bands by x0 -- measured off the actual PDF, consistent across
# all 213 pages (old colonial entries and modern ones alike differ only
# in the *number format* used in a column, e.g. a bare "9" vs "21/2007",
# never in the column layout itself).
_COLUMN_BANDS = [
    (0, 120, "year"),
    (120, 165, "act_no"),
    (165, 395, "title"),
    (395, 430, "repealed_by"),
    (430, 1000, "provision"),
]
# Header ("List of Acts in chronological order", the column labels
# themselves) and footer ("Page N of 213") both sit outside this band on
# every page.
_TOP_CUTOFF = 165.0
_BOTTOM_CUTOFF = 745.0


def _classify_column(x0: float) -> str | None:
    for lo, hi, name in _COLUMN_BANDS:
        if lo <= x0 < hi:
            return name
    return None


def extract_rows(pdf_path: Path) -> list[dict]:
    """[{"year", "act_no", "title", "repealed_by", "provision"}, ...] in
    document (chronological) order, one per real table row -- a wrapped
    continuation line is folded into whichever column it belongs to on
    the row already open, never treated as a row of its own."""
    doc = pymupdf.open(str(pdf_path))
    rows: list[dict] = []
    current_row: dict | None = None
    current_year: str | None = None

    for page in doc:
        spans = []
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"].strip()
                    if not text:
                        continue
                    x0, y0 = span["bbox"][0], span["bbox"][1]
                    if y0 < _TOP_CUTOFF or y0 > _BOTTOM_CUTOFF:
                        continue
                    col = _classify_column(x0)
                    if col:
                        spans.append((y0, col, text))
        spans.sort(key=lambda s: s[0])

        for y0, col, text in spans:
            if col == "act_no":
                if current_row is not None:
                    rows.append(current_row)
                current_row = {"year": current_year, "act_no": text, "title": "", "repealed_by": "", "provision": ""}
            elif col == "year":
                # A Year cell always sits *above* its own row's Act No. in
                # reading order (see the sample rows in the module
                # docstring), i.e. before the row it belongs to has even
                # opened yet -- only ever updates the pending value the
                # *next* row opens with, never the row already open
                # (which belongs to the *previous* Year cell).
                current_year = text
            elif current_row is not None:
                current_row[col] = (current_row[col] + " " + text).strip()
            # A column value appearing before any row has opened on this
            # page shouldn't happen -- every page's first real span is an
            # Act No. -- but is silently skipped rather than crashing if
            # it somehow does; there's nothing sensible to attach it to.

    if current_row is not None:
        rows.append(current_row)
    return rows


# A Victorian Act's clean short title, stripped of any trailing archaic
# citation the table's own Title column sometimes carries verbatim for
# pre-Federation Acts ("Roman Catholic Relief Act 1830 10 Geo. IV No. 9"
# -- everything after "Act 1830" is the *original* imperial-era citation,
# not part of the short title). Matches up to and including the first
# "Act YYYY", the same convention bill_linking.py's own citation matcher
# uses, for consistency between what gets looked up and what's stored.
_SHORT_TITLE_RE = re.compile(r"^(.*?\bAct\s+\d{4})\b")


def short_title(raw_title: str) -> str | None:
    m = _SHORT_TITLE_RE.match(raw_title)
    return m.group(1).strip() if m else None


def build_registry(rows: list[dict]) -> dict[str, dict]:
    registry: dict[str, dict] = {}
    for row in rows:
        title = short_title(row["title"])
        if not title:
            continue
        repealed_by = row["repealed_by"].strip() or None
        registry[title] = {
            "year": row["year"],
            "act_no": row["act_no"],
            "repealed_by": repealed_by,
            "repealed_provision": row["provision"].strip() or None,
            "in_force": repealed_by is None,
        }
    return registry


def main():
    rows = extract_rows(PDF_PATH)
    registry = build_registry(rows)
    OUT_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Extracted {len(rows)} row(s) -> {len(registry)} Act(s) -> {OUT_PATH}")
    unparsed = [r["title"] for r in rows if not short_title(r["title"])]
    if unparsed:
        print(f"{len(unparsed)} row(s) had no recognisable \"... Act YYYY\" title and were skipped:")
        for t in unparsed[:20]:
            print(f"  - {t!r}")
        if len(unparsed) > 20:
            print(f"  ... and {len(unparsed) - 20} more")


if __name__ == "__main__":
    main()
