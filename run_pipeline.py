"""
Parses an Act PDF into hierarchical components (Part/Division/Section/...),
ready for human review.

Two engines:
  --engine rules (default) -- deterministic, offset-based parser
      (ai_pipeline/rule_parser.py). Matches numbering patterns and font
      weight/size against a per-act-family profile (ai_pipeline/profiles.py)
      with no model call at all: free, instant, and it can prove nothing
      was silently dropped (every input line ends up in exactly one node --
      see the completeness check in the diagnostics report). New Acts with
      a different numbering style are handled by adding a profile override,
      not by changing code -- see ai_pipeline/profiles.py's docstring.
  --engine ai -- the local/Claude model backend (ai_pipeline/structure.py),
      for text the rules engine can't confidently classify, or as a second
      opinion.

Usage:
    python run_pipeline.py acts/crimes-act.pdf
    python run_pipeline.py acts/crimes-act.pdf --start-page 29
    python run_pipeline.py acts/crimes-act.pdf --engine ai --backend ollama
    python run_pipeline.py acts/crimes-act.pdf --profile my-other-act

Writes:
    data/extracted/<act-slug>.json     -- cleaned per-page text
    data/ai_parsed/<act-slug>.json     -- the structured node list
    data/diagnostics/<act-slug>.json   -- (rules engine) the anomaly report

Next step: python review.py <act-slug>
"""
import argparse
import json
import sys
from pathlib import Path

from ai_pipeline.diagnostics import run_diagnostics
from ai_pipeline.extract import extract_pages, pages_to_dicts, slugify
from ai_pipeline.hierarchy import HIERARCHY_ORDER
from ai_pipeline.rule_parser import parse_act
from ai_pipeline.toc import detect_body_start
from ai_pipeline.tree import attach_history


def run_rules_engine(pages, act_slug: str, profile_name: str | None):
    result = parse_act(pages, profile_name=profile_name)
    print(f"[{act_slug}] rules engine -> {len(result.nodes)} nodes, {result.lines_consumed}/{result.lines_total} lines consumed")
    for w in result.warnings:
        print(f"  ! {w}")
    return result.nodes, result


def run_ai_engine(pages, act_slug: str, backend_name: str, model: str | None, pages_per_chunk: int):
    from ai_pipeline.llm_backend import get_backend
    from ai_pipeline.structure import structure_act

    backend = get_backend(backend_name, model=model)
    print(f"[{act_slug}] AI engine -> backend={backend_name!r} model={backend.model!r}")
    nodes = structure_act(pages, act_slug, backend, pages_per_chunk=pages_per_chunk)
    return nodes, backend


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf_path")
    ap.add_argument("--engine", choices=["rules", "ai"], default="rules")
    ap.add_argument("--profile", default=None, help="rules engine: pattern profile name (ai_pipeline/profiles/<name>.yaml)")
    ap.add_argument("--pages-per-chunk", type=int, default=8, help="AI engine only")
    ap.add_argument("--start-page", type=int, default=None, help="1-indexed; default: auto-detect end of Table of Provisions")
    ap.add_argument("--end-page", type=int, default=None)
    ap.add_argument("--backend", choices=["ollama", "claude"], default="ollama", help="AI engine only")
    ap.add_argument("--model", default=None, help="AI engine only: override the backend's default model name")
    args = ap.parse_args()

    pdf_path = Path(args.pdf_path)
    act_slug = slugify(pdf_path.stem)

    print(f"Extracting text from {pdf_path} ...")
    all_pages = extract_pages(str(pdf_path))

    if args.start_page is not None:
        start = args.start_page - 1
    else:
        detected = detect_body_start(all_pages)
        start = detected - 1
        print(f"Auto-detected body start at page {detected} (override with --start-page)")
    end = args.end_page if args.end_page is not None else len(all_pages)
    pages = all_pages[start:end]
    print(f"Using pages {start + 1}-{end} ({len(pages)} pages)")

    extracted_dir = Path("data/extracted")
    extracted_dir.mkdir(parents=True, exist_ok=True)
    (extracted_dir / f"{act_slug}.json").write_text(
        json.dumps(pages_to_dicts(pages), indent=2), encoding="utf-8"
    )

    parse_result = None
    if args.engine == "rules":
        nodes, parse_result = run_rules_engine(pages, act_slug, args.profile)
        engine_meta = {"engine": "rules", "profile": args.profile}
        hierarchy_order = parse_result.hierarchy
    else:
        nodes, backend = run_ai_engine(pages, act_slug, args.backend, args.model, args.pages_per_chunk)
        engine_meta = {"engine": "ai", "backend": args.backend, "model": backend.model}
        hierarchy_order = list(HIERARCHY_ORDER)

    print("Attaching amendment-history margin notes ...")
    unattached_notes = attach_history(nodes, pages, hierarchy_order)
    if unattached_notes:
        print(f"  {len(unattached_notes)} note(s) could not be auto-linked to a node (kept for manual review)")

    parsed_dir = Path("data/ai_parsed")
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out_path = parsed_dir / f"{act_slug}.json"
    out_path.write_text(
        json.dumps(
            {
                "act": act_slug, "source": str(pdf_path), **engine_meta,
                "hierarchy": hierarchy_order,
                "nodes": nodes, "unattached_notes": unattached_notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {len(nodes)} nodes to {out_path}")

    if parse_result is not None:
        report = run_diagnostics(parse_result, nodes, unattached_notes)
        diag_dir = Path("data/diagnostics")
        diag_dir.mkdir(parents=True, exist_ok=True)
        diag_path = diag_dir / f"{act_slug}.json"
        diag_path.write_text(json.dumps(report.to_dicts(), indent=2), encoding="utf-8")
        n_err = len(report.by_severity("error"))
        n_warn = len(report.by_severity("warning"))
        n_info = len(report.by_severity("info"))
        print(
            f"Completeness: {report.lines_consumed}/{report.lines_total} lines accounted for "
            f"({'OK' if report.complete else 'MISMATCH -- see diagnostics'})"
        )
        print(f"Diagnostics: {n_err} error(s), {n_warn} warning(s), {n_info} info -> {diag_path}")
        if not report.complete:
            # Every input line landing in exactly one node is a hard
            # invariant of the rules engine (see rule_parser.py's module
            # docstring), never an expected outcome -- a mismatch here
            # means something regressed, so this must fail loudly (CI
            # included), not just print a warning nobody's watching.
            print(f"ERROR: {act_slug} -- completeness invariant violated, aborting.", file=sys.stderr)
            sys.exit(1)

    print(f"Next: python review.py {act_slug}")


if __name__ == "__main__":
    main()
