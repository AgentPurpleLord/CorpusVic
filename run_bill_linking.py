"""
Links a parsed Bill to its enacted Act, and the Bill's Explanatory
Memorandum to whichever Act/section (or Bill clause) each entry actually
describes -- see ai_pipeline/bill_linking.py for how both kinds of link
are worked out.

Usage:
    python run_bill_linking.py criminal-procedure-bill-2008 criminal-procedure-act \
        --em criminal-procedure-bill-2008-em

Reads data/ai_parsed/<bill-slug>.json, <act-slug>.json, and (if --em is
given) <em-slug>.json -- run run_pipeline.py/run_em_pipeline.py first.

Writes:
    data/bill_links/<bill-slug>-to-<act-slug>.json         -- clause<->section links
    data/bill_links/<em-slug>-links.json                   -- EM entry targets (if --em given)

Every link record carries verified_at=None -- nothing here is confirmed
until a human reviewer says so (see bill_linking.py's own module
docstring); "flagged"/unmatched links and unresolved Act names are worth
a reviewer's attention before anything gets treated as settled.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

from ai_pipeline.act_registry import load_act_registry
from ai_pipeline.bill_linking import match_bill_to_act, resolve_em_links
from ai_pipeline.link_targets import load_known_acts


def load_nodes(slug: str) -> list[dict]:
    path = Path("data/ai_parsed") / f"{slug}.json"
    if not path.exists():
        raise SystemExit(f"No parsed output found at {path} -- run run_pipeline.py/run_em_pipeline.py first.")
    return json.loads(path.read_text(encoding="utf-8"))["nodes"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bill_slug")
    ap.add_argument("act_slug")
    ap.add_argument("--em", default=None, help="EM slug (data/ai_parsed/<em-slug>.json) -- links EM entries too, if given")
    args = ap.parse_args()

    bill_nodes = load_nodes(args.bill_slug)
    act_nodes = load_nodes(args.act_slug)

    bill_links = match_bill_to_act(bill_nodes, act_nodes)
    out_dir = Path("data/bill_links")
    out_dir.mkdir(parents=True, exist_ok=True)
    bill_out = out_dir / f"{args.bill_slug}-to-{args.act_slug}.json"
    bill_out.write_text(json.dumps(bill_links, indent=2), encoding="utf-8")

    status_counts = Counter(link["status"] for link in bill_links)
    print(f"[{args.bill_slug} -> {args.act_slug}] {len(bill_links)} clause link(s): {dict(status_counts)}")
    print(f"Wrote {bill_out}")

    if args.em:
        em_nodes = load_nodes(args.em)
        known_acts = load_known_acts()
        act_registry = load_act_registry()
        em_links = resolve_em_links(em_nodes, args.bill_slug, args.act_slug, bill_links, known_acts, act_registry)
        em_out = out_dir / f"{args.em}-links.json"
        em_out.write_text(json.dumps(em_links, indent=2), encoding="utf-8")

        kind_counts = Counter((link["target"]["kind"] if link["target"] else "unresolved") for link in em_links)
        print(f"[{args.em}] {len(em_links)} entry link(s): {dict(kind_counts)}")

        unresolved_targets = [
            link["target"]
            for link in em_links
            if link["target"] and link["target"]["kind"] == "act_section" and link["target"]["act_slug"] is None and link["target"]["act_title"]
        ]
        confirmed_but_unparsed = sorted({t["act_title"] for t in unresolved_targets if "in_force" in t})
        wholly_unrecognised = sorted({t["act_title"] for t in unresolved_targets if "in_force" not in t})
        if confirmed_but_unparsed:
            print(f"  {len(confirmed_but_unparsed)} Act name(s) confirmed real (via act_registry.json) but not in ai_pipeline/known_acts.yaml -- add them to link into their content:")
            for title in confirmed_but_unparsed:
                print(f"    - {title}")
        if wholly_unrecognised:
            print(f"  {len(wholly_unrecognised)} Act name(s) not found in known_acts.yaml OR act_registry.json -- check spelling/extraction:")
            for title in wholly_unrecognised:
                print(f"    - {title}")
        print(f"Wrote {em_out}")


if __name__ == "__main__":
    main()
