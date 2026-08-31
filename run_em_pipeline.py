"""
Parses a Bill's Explanatory Memorandum (EM) PDF into a flat node list --
"Clause N ..." entries plus organisational Chapter/Part headers (see
ai_pipeline/em_parser.py for why an EM's own structure is this much
flatter than an Act or Bill, and doesn't reuse rule_parser.py).

Usage:
    python run_em_pipeline.py acts/criminal-procedure-bill-2008-em.pdf

Writes:
    data/extracted/<em-slug>.json     -- cleaned per-page text
    data/ai_parsed/<em-slug>.json     -- the structured node list

Next step: python review.py <em-slug> --flat
    (the default section-grouped review mode has nothing to group an
    em_entry's own children under -- an EM has none -- so review it node-
    by-node instead; see review.py's own docstring for --flat.)
"""
import argparse
import json
import sys
from pathlib import Path

from ai_pipeline.em_parser import parse_em
from ai_pipeline.extract import extract_pages, pages_to_dicts, slugify


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

    parsed_dir = Path("data/ai_parsed")
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out_path = parsed_dir / f"{em_slug}.json"
    out_path.write_text(
        json.dumps({"act": em_slug, "source": str(pdf_path), "engine": "em_rules", "nodes": result.nodes, "unattached_notes": []}, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote {len(result.nodes)} nodes to {out_path}")

    complete = result.lines_consumed == result.lines_total
    print(f"Completeness: {result.lines_consumed}/{result.lines_total} lines accounted for ({'OK' if complete else 'MISMATCH'})")
    if not complete:
        # Same hard invariant as rule_parser.py's own Act/Bill parsing --
        # see em_parser.py's module docstring.
        print(f"ERROR: {em_slug} -- completeness invariant violated, aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"Next: python review.py {em_slug} --flat")


if __name__ == "__main__":
    main()
