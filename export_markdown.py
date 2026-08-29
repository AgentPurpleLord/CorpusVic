"""
Exports a parsed Act to a browsable set of Markdown files (AustLII-style:
one page per Section, an Act index, defined terms and section/Part/Division
references hyperlinked between pages).

Usage:
    python export_markdown.py crimes-act
    python export_markdown.py crimes-act --out data/markdown/crimes-act

Prefers data/verified/<act>.json (what you approved in review.py) and falls
back to data/ai_parsed/<act>.json if you haven't run review.py yet.

Writes (default --out data/markdown/<act>/):
    index.md
    sections/sXX.md   (one per Section)

See ai_pipeline/markdown_export.py's module docstring for how cross-linking
works and its limitations.
"""
import argparse
import json
from pathlib import Path

from ai_pipeline.akn_export import _detect_act_citation
from ai_pipeline.markdown_export import export_to_markdown


def load_nodes_for_export(act: str) -> dict:
    verified_path = Path("data/verified") / f"{act}.json"
    parsed_path = Path("data/ai_parsed") / f"{act}.json"
    if not parsed_path.exists():
        raise SystemExit(f"No parsed output found at {parsed_path} -- run run_pipeline.py first.")
    base = json.loads(parsed_path.read_text(encoding="utf-8"))
    if verified_path.exists():
        print(f"Using human-verified nodes from {verified_path}")
        base["nodes"] = json.loads(verified_path.read_text(encoding="utf-8"))
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
