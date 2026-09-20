"""
Exports a parsed Act to a browsable set of Markdown files (AustLII-style:
one page per Section, an Act index, defined terms and section/Part/Division
references hyperlinked between pages).

Usage:
    python export_markdown.py crimes-act
    python export_markdown.py crimes-act --out data/markdown/crimes-act

Uses whatever review.py's build_current_nodes finds in data/legislation.db
(human-verified where a unit's been reviewed there, the raw parser output
from data/parsed/<act>.json everywhere else) -- so exporting mid-review
still includes every node, not just the ones reviewed so far.

Writes (default --out data/markdown/<act>/):
    index.md
    sections/sXX.md   (one per Section)

See corpus/markdown_export.py's module docstring for how cross-linking
works and its limitations.
"""
import argparse
import json
from pathlib import Path

from corpus.exporters.akn_export import _detect_act_citation
from corpus.exporters.markdown_export import export_to_markdown
from corpus.review.review import build_current_nodes


def load_nodes_for_export(act: str) -> dict:
    parsed_path = Path("../../data/parsed") / f"{act}.json"
    if not parsed_path.exists():
        raise SystemExit(f"No parsed output found at {parsed_path} -- run run_pipeline.py first.")
    base = json.loads(parsed_path.read_text(encoding="utf-8"))
    nodes, unattached_notes, hierarchy = build_current_nodes(act)
    base["nodes"] = nodes
    base["unattached_notes"] = unattached_notes
    if hierarchy:
        base["hierarchy"] = hierarchy
    reviewed = sum(1 for n in nodes if n.get("verified_at") or n.get("needs_followup"))
    if reviewed:
        print(f"Using {reviewed}/{len(nodes)} human-reviewed node(s) from data/legislation.db, the raw parse for the rest")
    else:
        print(f"No verified data yet -- using raw parser output from {parsed_path} (run review.py first for higher confidence)")
    return base


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--out", default=None, help="output directory (default: data/markdown/<act>)")
    ap.add_argument("--source", default=None, help="override: path to a specific parsed JSON file")
    args = ap.parse_args()

    parsed = json.loads(Path(args.source).read_text(encoding="utf-8")) if args.source else load_nodes_for_export(args.act)

    citation = _detect_act_citation(parsed.get("source"))
    act_title = citation.get("title") or args.act

    out_dir = args.out or f"data/markdown/{args.act}"
    stats = export_to_markdown(parsed, out_dir, act_title=act_title)
    print(f"Wrote {stats['sections']} section page(s) and index.md to {out_dir}/")
    print(f"Linked {stats['definitions']} defined term(s)")


if __name__ == "__main__":
    main()
