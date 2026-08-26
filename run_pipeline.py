"""
Extracts clean text from an Act PDF and asks Claude to structure it into
hierarchical components (Part/Division/Section/...), ready for human review.

Usage:
    python run_pipeline.py acts/crimes-act.pdf
    python run_pipeline.py acts/crimes-act.pdf --start-page 3 --end-page 40
    python run_pipeline.py acts/crimes-act.pdf --pages-per-chunk 10

Writes:
    data/extracted/<act-slug>.json  -- cleaned per-page text (body/margin/etc.)
    data/ai_parsed/<act-slug>.json  -- the AI's structured node list

Next step: python review.py <act-slug>
"""
import argparse
import json
from pathlib import Path

from ai_pipeline.extract import extract_pages, pages_to_dicts, slugify
from ai_pipeline.llm_backend import get_backend
from ai_pipeline.structure import structure_act
from ai_pipeline.tree import attach_history


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf_path")
    ap.add_argument("--pages-per-chunk", type=int, default=8)
    ap.add_argument("--start-page", type=int, default=1, help="1-indexed; use to skip the Table of Provisions")
    ap.add_argument("--end-page", type=int, default=None)
    ap.add_argument(
        "--backend", choices=["ollama", "claude"], default="ollama",
        help="which model interprets the text (default: ollama, runs fully local/free)",
    )
    ap.add_argument("--model", default=None, help="override the backend's default model name")
    args = ap.parse_args()

    pdf_path = Path(args.pdf_path)
    act_slug = slugify(pdf_path.stem)

    print(f"Extracting text from {pdf_path} ...")
    pages = extract_pages(str(pdf_path))

    start = max(args.start_page - 1, 0)
    end = args.end_page if args.end_page is not None else len(pages)
    pages = pages[start:end]
    print(f"Using pages {start + 1}-{end} of {len(pages) + start} ({len(pages)} pages)")

    extracted_dir = Path("data/extracted")
    extracted_dir.mkdir(parents=True, exist_ok=True)
    (extracted_dir / f"{act_slug}.json").write_text(
        json.dumps(pages_to_dicts(pages), indent=2), encoding="utf-8"
    )

    backend = get_backend(args.backend, model=args.model)
    print(f"Structuring with backend={args.backend!r} model={backend.model!r} ...")
    nodes = structure_act(pages, act_slug, backend, pages_per_chunk=args.pages_per_chunk)

    print("Attaching amendment-history margin notes ...")
    unattached_notes = attach_history(nodes, pages)
    if unattached_notes:
        print(f"  {len(unattached_notes)} note(s) could not be auto-linked to a node (kept for manual review)")

    parsed_dir = Path("data/ai_parsed")
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out_path = parsed_dir / f"{act_slug}.json"
    out_path.write_text(
        json.dumps(
            {
                "act": act_slug,
                "source": str(pdf_path),
                "backend": args.backend,
                "model": backend.model,
                "nodes": nodes,
                "unattached_notes": unattached_notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {len(nodes)} nodes to {out_path}")
    print(f"Next: python review.py {act_slug}")


if __name__ == "__main__":
    main()
