"""
Exports a parsed Act to Akoma Ntoso XML.

Usage:
    python export_akn.py crimes-act
    python export_akn.py crimes-act --source data/parsed/crimes-act.json

Uses whatever review.py's build_current_nodes finds in data/legislation.db
(human-verified where a unit's been reviewed there, the raw parser output
from data/parsed/<act>.json everywhere else) -- so exporting mid-review
still includes every node, not just the ones reviewed so far.

Writes: data/akn/<act>.xml

See corpus/akn_export.py's module docstring for the structural mapping
and the amendment-history modelling this uses (lifecycle/analysis, not
temporalGroup/period), plus its documented limitation on undated citations.
"""
import argparse
import json
from pathlib import Path

from corpus.akn_export import write_akn
from review import build_current_nodes


def load_nodes_for_export(act: str) -> dict:
    parsed_path = Path("data/parsed") / f"{act}.json"
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
    ap.add_argument("--source", default=None, help="override: path to a specific parsed JSON file instead of the verified/parsed lookup")
    args = ap.parse_args()

    if args.source:
        parsed = json.loads(Path(args.source).read_text(encoding="utf-8"))
    else:
        parsed = load_nodes_for_export(args.act)

    out_dir = Path("data/akn")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.act}.xml"
    write_akn(parsed, str(out_path), source_pdf=parsed.get("source"))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
