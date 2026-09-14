"""
Parses a Bill's Explanatory Memorandum (EM) PDF into a flat node list --
"Clause N ..." entries plus organisational Chapter/Part headers (see
corpus/em_parser.py for why an EM's own structure is this much
flatter than an Act or Bill, and doesn't reuse rule_parser.py).

Usage:
    python run_em_pipeline.py acts/criminal-procedure-bill-2008-em.pdf

Writes:
    data/extracted/<em-slug>.json     -- cleaned per-page text
    data/parsed/<em-slug>.json     -- the structured node list

Next step: python review.py <em-slug>
    (an EM entry has no children of its own -- an EM's structure is flat
    -- so group_into_units naturally gives each entry its own single-
    piece review unit.)
"""
import argparse
import json
import sys
from pathlib import Path

from corpus.em_parser import parse_em
from corpus.extract import extract_pages, pages_to_dicts, slugify
from corpus.hierarchy import HIERARCHY_ORDER, group_into_units
from corpus.reparse import apply_remap, describe_remap, parse_fingerprint
from corpus.versions import read_front_matter


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf_path")
    args = ap.parse_args()

    pdf_path = Path(args.pdf_path)
    em_slug = slugify(pdf_path.stem)

    print(f"Extracting text from {pdf_path} ...")
    pages = extract_pages(str(pdf_path))

    extracted_dir = Path("data/extracted")
    extracted_dir.mkdir(parents=True, exist_ok=True)
    (extracted_dir / f"{em_slug}.json").write_text(json.dumps(pages_to_dicts(pages), indent=2), encoding="utf-8")

    result = parse_em(pages)
    print(f"[{em_slug}] -> {len(result.nodes)} nodes, {result.lines_consumed}/{result.lines_total} lines consumed")
    for w in result.warnings:
        print(f"  ! {w}")

    parsed_dir = Path("data/parsed")
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out_path = parsed_dir / f"{em_slug}.json"
    out_path.write_text(
        json.dumps(
            {
                "act": em_slug, "source": str(pdf_path), "engine": "em_rules", "document_type": "em",
                # Recorded for the same reason run_pipeline.py records it:
                # every downstream consumer (the exporters, the browse
                # view) reads the resolved order back from here rather
                # than re-deriving it. An EM has no containers of its own
                # -- its Chapter/Part lines are heading_groups, not
                # hierarchy levels -- so this is just the default order,
                # under which its "clause" entries rank as sections do.
                # An EM has no Authorised Version of its own -- recorded
                # anyway so every parsed document has the same shape, and
                # so its title comes through (see corpus/versions.py).
                "version": read_front_matter(pdf_path),
                "hierarchy": list(HIERARCHY_ORDER),
                # See run_pipeline.py's own note: this is what tells a
                # stored review row whether its node position still means
                # what it meant when a human accepted it.
                "fingerprint": parse_fingerprint(result.nodes),
                "nodes": result.nodes, "unattached_notes": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {len(result.nodes)} nodes to {out_path}")

    remap = apply_remap(em_slug, result.nodes, group_into_units(result.nodes))
    if remap is not None:
        print(f"Review progress: {describe_remap(remap)}")

    complete = result.lines_consumed == result.lines_total
    print(f"Completeness: {result.lines_consumed}/{result.lines_total} lines accounted for ({'OK' if complete else 'MISMATCH'})")
    if not complete:
        # Same hard invariant as rule_parser.py's own Act/Bill parsing --
        # see em_parser.py's module docstring.
        print(f"ERROR: {em_slug} -- completeness invariant violated, aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"Next: python review.py {em_slug}")


if __name__ == "__main__":
    main()
