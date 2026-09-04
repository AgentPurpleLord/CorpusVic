"""
Parses an Act PDF into hierarchical components (Part/Division/Section/...),
ready for human review.

Deterministic and offset-based (ai_pipeline/rule_parser.py): it matches
numbering patterns and font weight/size/position against a per-act-family
profile (ai_pipeline/profiles.py) with no model call at all -- free,
instant, and able to prove nothing was silently dropped, since every input
line ends up in exactly one node (see the completeness check in the
diagnostics report below, which aborts this script if it ever fails). New
Acts with a different numbering style are handled by adding a profile
override, not by changing code -- see ai_pipeline/profiles.py's docstring.

There used to be a second, model-backed engine here. It was removed rather
than fixed: it read the page as plain text, so it never saw the font
weight, size and x-position that the rules engine actually classifies on,
it had no completeness invariant of its own, and it couldn't consume a
profile at all. On this corpus that made it strictly worse at the one job,
with no compensating case it handled better.

Usage:
    python run_pipeline.py acts/crimes-act.pdf
    python run_pipeline.py acts/crimes-act.pdf --start-page 29
    python run_pipeline.py acts/crimes-act.pdf --profile my-other-act
    python run_pipeline.py acts/criminal-procedure-bill-2008.pdf \
        --document-type bill --profile criminal-procedure-act

--document-type bill parses a Bill instead of an enacted Act (rules
engine only): its own top-level numbered provisions come out typed
"clause" rather than "section" (same nesting rank -- see hierarchy.py --
just the pre-enactment name), and its front matter (title page + Table of
Provisions, which visually mimics real headings closely enough to
otherwise fool the heading classifiers) is skipped up to the fixed
enacting words every Bill's real text opens with (see rule_parser.py's
_skip_bill_front_matter). A Bill shares its originating Act's own
drafting conventions, so pass that Act's --profile too where one exists
(as above) rather than duplicating the same overrides under a new name --
nothing here ties a profile's filename to the PDF slug it's used with.

An Act's closing Endnotes are split off before the body parse and handed
to ai_pipeline/endnotes.py, which reads the Table of Amendments as a real
table (see its own docstring for why that needs geometry, not text). The
result rides along in data/ai_parsed/<act-slug>.json under "endnotes".

Writes:
    data/extracted/<act-slug>.json     -- cleaned per-page text
    data/ai_parsed/<act-slug>.json     -- the structured node list + endnotes
    data/diagnostics/<act-slug>.json   -- the anomaly report

Next step: python review.py <act-slug>
"""
import argparse
import json
import sys
from pathlib import Path

from ai_pipeline.diagnostics import run_diagnostics
from ai_pipeline.endnotes import detect_endnotes_start, parse_endnotes
from ai_pipeline.extract import extract_pages, pages_to_dicts, slugify
from ai_pipeline.hierarchy import group_into_units
from ai_pipeline.profiles import profile_exists
from ai_pipeline.reparse import apply_remap, describe_remap, parse_fingerprint
from ai_pipeline.versions import describe as describe_version
from ai_pipeline.versions import document_slug, read_front_matter, work_directory
from ai_pipeline.rule_parser import parse_act
from ai_pipeline.toc import detect_body_start
from ai_pipeline.tree import attach_history


def run_parser(pages, act_slug: str, profile_name: str | None, document_type: str = "act"):
    top_level_type = "clause" if document_type == "bill" else "section"
    result = parse_act(pages, profile_name=profile_name, top_level_type=top_level_type, skip_front_matter=(document_type == "bill"))
    print(f"[{act_slug}] parser -> {len(result.nodes)} nodes, {result.lines_consumed}/{result.lines_total} lines consumed")
    for w in result.warnings:
        print(f"  ! {w}")
    return result.nodes, result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf_path")
    ap.add_argument(
        "--document-type", choices=["act", "bill"], default="act",
        help="\"bill\" parses a Bill instead of an enacted Act (see the module docstring)",
    )
    ap.add_argument("--profile", default=None, help="pattern profile name (ai_pipeline/profiles/<name>.yaml)")
    ap.add_argument("--start-page", type=int, default=None, help="1-indexed; default: auto-detect end of Table of Provisions")
    ap.add_argument("--end-page", type=int, default=None)
    args = ap.parse_args()

    pdf_path = Path(args.pdf_path)
    # Which expression of the Act this is, read off its own front matter
    # (see ai_pipeline/versions.py). Needed before the slug, because a PDF
    # sitting in a work directory is stored under that work and its own
    # version number rather than under its filename.
    version = read_front_matter(pdf_path)
    work = work_directory(pdf_path)
    if work and version["version"] is None:
        raise SystemExit(
            f"{pdf_path} is in the work directory {work!r} but states no Authorised Version "
            "number, so there is no way to tell which version of that work it is. Move it out "
            "of the directory to parse it as a document in its own right."
        )
    act_slug = document_slug(work, version["version"]) if work else slugify(pdf_path.stem)
    if work:
        print(f"{describe_version(version)} -> {act_slug}")

    print(f"Extracting text from {pdf_path} ...")
    all_pages = extract_pages(str(pdf_path))

    if args.start_page is not None:
        start = args.start_page - 1
    else:
        detected = detect_body_start(all_pages)
        start = detected - 1
        print(f"Auto-detected body start at page {detected} (override with --start-page)")
    end = args.end_page if args.end_page is not None else len(all_pages)

    # The Endnotes are a different document with a different layout (a
    # two-column table), and their own section headings ("1 General
    # information", "2 Table of Amendments") are shaped exactly like the
    # Act's -- so left in the body parse they collide with the Act's real
    # sections 1 and 2 and swallow every remaining page into one node.
    # Split them out and hand them to their own parser instead.
    endnotes_start = detect_endnotes_start(all_pages)
    body_end = min(end, endnotes_start - 1) if endnotes_start else end
    pages = all_pages[start:body_end]
    endnote_pages = all_pages[endnotes_start - 1 : end] if endnotes_start else []
    print(f"Using pages {start + 1}-{body_end} ({len(pages)} pages)")
    if endnote_pages:
        print(f"Endnotes detected at page {endnotes_start} -- parsed separately ({len(endnote_pages)} pages)")

    extracted_dir = Path("data/extracted")
    extracted_dir.mkdir(parents=True, exist_ok=True)
    (extracted_dir / f"{act_slug}.json").write_text(
        json.dumps(pages_to_dicts(pages), indent=2), encoding="utf-8"
    )

    # A profile named after the Act applies to it without being asked for.
    # It only ever took effect with an explicit --profile before, so a
    # profile could sit in the repo doing nothing while the Act it was
    # written for kept parsing on the defaults -- which is what happened to
    # the Evidence Act's Parts. An explicit --profile still wins.
    #
    # Looked up under the *work* before the document: how an Act numbers
    # its Parts is a fact about the Act, not about one reprint of it, so
    # one criminal-procedure-act.yaml serves all five of its versions
    # rather than needing a copy per version.
    profile_name = args.profile or next(
        (name for name in ((work or act_slug), act_slug) if profile_exists(name)), None
    )
    if profile_name and not args.profile:
        print(f"Using profile ai_pipeline/profiles/{profile_name}.yaml (named after this Act)")
    nodes, parse_result = run_parser(pages, act_slug, profile_name, document_type=args.document_type)
    engine_meta = {"engine": "rules", "profile": profile_name, "document_type": args.document_type}
    hierarchy_order = parse_result.hierarchy

    endnotes = None
    if endnote_pages:
        endnotes_result = parse_endnotes(endnote_pages)
        endnotes = endnotes_result.to_dict()
        print(
            f"[{act_slug}] endnotes -> {len(endnotes_result.amending_acts)} amending Act(s) in the Table of "
            f"Amendments, {endnotes_result.lines_consumed}/{endnotes_result.lines_total} lines consumed"
        )
        for w in endnotes_result.warnings[:5]:
            print(f"  ! {w}")

    print("Attaching amendment-history margin notes ...")
    unattached_notes = attach_history(nodes, pages, hierarchy_order)
    # Provenance notes ("No. 6103 s. 15.", "cf. [1819] 60 George III ...")
    # name no provision of this Act, so they were never going to link to
    # one -- counting them as failures made correctly-handled notes look
    # like a parser problem. Reported separately, not as a shortfall.
    provenance = sum(1 for n in unattached_notes if n.get("kind") == "provenance")
    unlinked = len(unattached_notes) - provenance
    if unlinked:
        print(f"  {unlinked} amendment note(s) could not be auto-linked to a node (kept for manual review)")
    if provenance:
        print(f"  {provenance} provenance note(s) (where a provision came from, not how it changed) -- nothing to link")

    parsed_dir = Path("data/ai_parsed")
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out_path = parsed_dir / f"{act_slug}.json"
    out_path.write_text(
        json.dumps(
            {
                "act": act_slug, "source": str(pdf_path), **engine_meta,
                "version": version,
                "hierarchy": hierarchy_order,
                # A digest of this node list's own structure. Review rows
                # are keyed by position into it, so this is what lets
                # anything reading them tell "these positions still mean
                # what they meant" from "this parse has moved underneath
                # them" -- see ai_pipeline/reparse.py.
                "fingerprint": parse_fingerprint(nodes),
                "nodes": nodes, "unattached_notes": unattached_notes,
                "endnotes": endnotes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {len(nodes)} nodes to {out_path}")

    # Re-parsing an Act somebody has already reviewed used to quietly
    # invalidate their work: every stored row points at a node *position*,
    # and a parser change that adds or re-splits one node shifts every
    # position after it. Move the rows onto the provisions they actually
    # describe instead, before anything reads them again.
    remap = apply_remap(act_slug, nodes, group_into_units(nodes))
    if remap is not None:
        print(f"Review progress: {describe_remap(remap)}")
        for label in remap["changed"][:5]:
            print(f"  ~ wording changed, acceptance withdrawn: {label}")
        for label in remap["orphans"][:5]:
            print(f"  ! no longer in the parse, kept for you to re-file: {label}")

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
        # invariant of the parser (see rule_parser.py's module
        # docstring), never an expected outcome -- a mismatch here
        # means something regressed, so this must fail loudly (CI
        # included), not just print a warning nobody's watching.
        print(f"ERROR: {act_slug} -- completeness invariant violated, aborting.", file=sys.stderr)
        sys.exit(1)

    print(f"Next: python review.py {act_slug}")


if __name__ == "__main__":
    main()
