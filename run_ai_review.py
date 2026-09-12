"""
Runs a whole-document AI audit pass over an already-parsed Act, Bill or
Explanatory Memorandum: the local model looks at *every* unit (not just
the ones diagnostics.py already flagged) and says whether its own
type/number/heading/text classification looks right -- see
ai_pipeline/ai_scan.py's own docstring for the question it asks and why.

Usage:
    python run_ai_review.py crimes-act
    python run_ai_review.py crimes-act --batch-size 20 --model llama3.1:8b-instruct
    python run_ai_review.py crimes-act --restart

This is a separate, offline script rather than something review.py
kicks off itself, because scanning everything is genuinely slow: a
~700-unit Act like the Crimes Act is dozens of batched model calls even
at the default batch size, and a local model's own generation speed
depends entirely on the machine it's running on -- there's no good way
to make that fit inside one HTTP request/response.

It's also resumable and safe to interrupt (Ctrl-C, a crash, the machine
sleeping): every unit's result -- including a "clean" row for a unit
with nothing wrong with it -- is saved to data/legislation.db as soon as
its batch comes back (see db.save_ai_scan_finding), and a re-run skips
anything already scanned. Pass --restart to throw that away and scan
everything again (after a big profile change, say, or to pick up a
newer/better model's opinion).

Findings land in the same place diagnostics.py's own do: review.py loads
them into _findings_by_node at startup (tagged "ai-scan" rather than
whatever category diagnostics.py's own checks use), so they gate
blind-review and stay hidden from a reviewer until their own independent
judgement is already recorded, exactly like every other finding --
running this script never shortcuts that protection, it just adds more
candidates for it to guard.
"""
import argparse

from ai_pipeline import db
from ai_pipeline.ai_scan import BATCH_SIZE, iter_unit_batches, scan_batch, unit_root
from ai_pipeline.llm_backend import DEFAULT_MODEL, OLLAMA_HOST, OllamaBackend, OllamaUnavailable
from review import build_effective_nodes_indexed, positions_are_trustworthy


def pending_units(nodes: list, units: list[list[int]], already_scanned: set[int]) -> list[list[int]]:
    """Which units are actually worth a model call: not one whose own
    root node is gone (merged away by a reviewer since the parse this
    scan is reading -- see build_effective_nodes_indexed), and not one
    already scanned (see db.load_ai_scan_findings -- a "clean" result
    counts as scanned too, since the point of storing it is to never ask
    about that unit again without --restart).

    A unit's other, non-root nodes can still be individually merged away
    without the whole unit being skipped -- those positions are simply
    left out of the text the model is shown (see the list comprehension
    below), the same way a merged-away node just isn't there any more
    for a human reviewer either."""
    result = []
    for unit in units:
        if nodes[unit[0]] is None or unit[0] in already_scanned:
            continue
        result.append([i for i in unit if nodes[i] is not None])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"units per model call (default: {BATCH_SIZE})")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"model to use (default: {DEFAULT_MODEL})")
    ap.add_argument("--host", default=OLLAMA_HOST, help=f"Ollama server address (default: {OLLAMA_HOST})")
    ap.add_argument("--restart", action="store_true", help="ignore already-scanned units and start over")
    args = ap.parse_args()

    nodes, units, fingerprint = build_effective_nodes_indexed(args.act)
    if not positions_are_trustworthy(args.act, fingerprint):
        raise SystemExit(
            f"{args.act!r}'s stored review rows don't match its current parse (see "
            "review.positions_are_trustworthy) -- a scan run now couldn't be trusted to land "
            "back on the right node later. Re-parse and let run_pipeline.py re-anchor the "
            "existing rows first."
        )

    if args.restart:
        cleared = db.clear_ai_scan_findings(args.act)
        if cleared:
            print(f"Cleared {cleared} previously-scanned unit(s) -- starting over.")

    already_scanned = {row["node_index"] for row in db.load_ai_scan_findings(args.act)}
    todo = pending_units(nodes, units, already_scanned)
    if not todo:
        print(f"Nothing to scan -- every unit of {args.act!r} has already been looked at. Pass --restart to redo it.")
        return

    backend = OllamaBackend(model=args.model, host=args.host)
    try:
        backend.ensure_ready()
    except OllamaUnavailable as e:
        raise SystemExit(str(e)) from e

    total = len(todo)
    print(f"Scanning {total} unit(s) of {args.act!r} in batches of {args.batch_size}, using {backend.model!r} ...")

    scanned = 0
    concerns = 0
    for _start, batch in iter_unit_batches(todo, args.batch_size):
        try:
            flagged = scan_batch(nodes, batch, backend=backend)
        except OllamaUnavailable as e:
            raise SystemExit(
                f"{e}\n\n{scanned}/{total} unit(s) were saved before this failure -- re-run this "
                "script once Ollama is available again and it will pick up from here."
            ) from e
        for i, unit in enumerate(batch):
            root = unit_root(unit)
            result = flagged.get(i)
            if result is None:
                db.save_ai_scan_finding(args.act, root, severity="clean", message="", model=backend.model)
            else:
                db.save_ai_scan_finding(
                    args.act, root, severity=result["severity"], message=result["concern"], model=backend.model,
                )
                concerns += 1
        scanned += len(batch)
        print(f"  {scanned}/{total} scanned, {concerns} concern(s) found so far")

    print(
        f"Done. {scanned} unit(s) scanned, {concerns} concern(s) flagged for review -- "
        "they'll show up in review.py the next time it starts (or restarts) for this Act."
    )


if __name__ == "__main__":
    main()
