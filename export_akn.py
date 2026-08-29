"""
Exports a parsed Act to Akoma Ntoso XML.

Usage:
    python export_akn.py crimes-act
    python export_akn.py crimes-act --source data/ai_parsed/crimes-act.json

Prefers data/verified/<act>.json (what you approved in review.py) and falls
back to data/ai_parsed/<act>.json (the raw parser output) if you haven't
run review.py yet -- printed either way so you know which one you got.

Writes: data/akn/<act>.xml

See ai_pipeline/akn_export.py's module docstring for the structural mapping
and the amendment-history modelling this uses (lifecycle/analysis, not
temporalGroup/period), plus its documented limitation on undated citations.
"""
import argparse
import json
from pathlib import Path

from ai_pipeline.akn_export import write_akn


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
    ap.add_argument("--source", default=None, help="override: path to a specific parsed JSON file instead of the verified/ai_parsed lookup")
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
