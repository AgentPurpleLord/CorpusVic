"""
Sets up the local model the AI-assist feature uses (see
ai_pipeline/ai_assist.py) -- an optional second opinion a reviewer can
ask for on a piece diagnostics has already flagged as uncertain. This
feature is entirely optional: nothing in the parsing pipeline needs it,
and skipping this script just means that one button in review.py stays
unavailable.

Everything here runs locally. Ollama (https://ollama.com) serves an
open-source model file from disk on this machine, with no network
access needed once the model is pulled and no data leaving it -- see
ai_pipeline/llm_backend.py's own module docstring for why Ollama is
the only backend this project uses.

Usage:
    python install_ai_model.py                     # pull the default model
    python install_ai_model.py --model llama3.1:8b-instruct

This checks, in order, for the three things ai_pipeline.llm_backend.
OllamaBackend.ensure_ready checks at review time -- Ollama installed,
Ollama running, the model pulled -- and only ever does the third one
for you. The first two are left to you to fix and re-run this for:
installing or starting a background service you'll keep running is a
bigger decision than this script should make on its own, and the exact
right way to do either depends on your OS in a way a model download
doesn't.
"""
import argparse
import sys

from ai_pipeline.llm_backend import DEFAULT_MODEL, OLLAMA_HOST, OllamaBackend, pull_model


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to pull (default: {DEFAULT_MODEL})")
    ap.add_argument("--host", default=OLLAMA_HOST, help=f"Ollama server address (default: {OLLAMA_HOST})")
    ap.add_argument("--yes", "-y", action="store_true", help="Don't ask for confirmation before downloading")
    args = ap.parse_args()

    backend = OllamaBackend(model=args.model, host=args.host)
    status = backend.status()

    if not status["installed"]:
        print(
            "Ollama isn't installed.\n\n"
            "Install it from https://ollama.com (macOS/Windows: download the app; "
            "Linux: curl -fsSL https://ollama.com/install.sh | sh), then run this "
            "script again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not status["running"]:
        print(
            f"Ollama is installed but not reachable at {args.host}.\n\n"
            "Start it with `ollama serve` (or open the Ollama app), then run this "
            "script again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if status["model_present"]:
        print(f"Already set up: {args.model!r} is pulled and Ollama is running at {args.host}.")
        print("The AI-assist button in review.py is ready to use.")
        return

    print(f"Ollama is running. The model {args.model!r} hasn't been pulled yet.")
    if not args.yes:
        # A model pull can be several gigabytes -- worth a human's
        # explicit yes rather than a script quietly filling their disk
        # the first time they run it out of curiosity.
        reply = input(f"Download {args.model!r} now? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Not downloading. Re-run with --yes to skip this prompt.")
            sys.exit(1)

    print(f"Pulling {args.model!r} -- this can take a while the first time ...")
    ok, log = pull_model(args.model, host=args.host)
    print(log)
    if not ok:
        print(f"\nERROR: failed to pull {args.model!r} -- see the output above.", file=sys.stderr)
        sys.exit(1)

    print(f"\nDone. {args.model!r} is ready -- the AI-assist button in review.py will now work.")


if __name__ == "__main__":
    main()
