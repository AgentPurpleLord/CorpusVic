#!/usr/bin/env python3
"""
Fetches the embedding model that corpus/embeddings.py uses.

A download rather than a commit. The model is around 110 MB quantised and
440 MB not, it is somebody else's artefact, and putting it in git would
make every clone of this repository carry it forever -- including the
ones that only want to parse a PDF.

Nothing else in the project needs this to have been run. Without a model,
search is the lexical search it has always been; with one, the semantic
half turns itself on. See corpus/embeddings.py.

    python3 download_search_model.py            # quantised, ~110 MB
    python3 download_search_model.py --full     # float32, ~440 MB
    python3 download_search_model.py --check    # say what is there, fetch nothing

On a 2 GB VPS use the quantised model: roughly 250 MB resident while it
builds, against about 700 MB for the float32 one, which is more than that
box has to spare beside a web server.
"""
import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Hugging Face's ONNX export of BAAI/bge-base-en-v1.5. Named in full
# rather than assembled from parts, so that what this fetches is
# greppable and reviewable rather than something you have to run the
# script to discover.
REPO = "Xenova/bge-base-en-v1.5"
BASE = f"https://huggingface.co/{REPO}/resolve/main"

# (url suffix, where it lands). tokenizer.json is the whole tokenizer --
# vocabulary and rules -- which is why `tokenizers` alone is enough here
# and the much larger `transformers` is not needed.
FILES = [
    ("tokenizer.json", "tokenizer.json"),
    ("tokenizer_config.json", "tokenizer_config.json"),
    ("special_tokens_map.json", "special_tokens_map.json"),
    ("config.json", "config.json"),
]
QUANTISED = ("onnx/model_quantized.onnx", "model.onnx")
FULL = ("onnx/model.onnx", "model.onnx")


def target_dir(base_dir) -> Path:
    from corpus import embeddings

    return embeddings.model_dir(base_dir)


def fetch(url: str, destination: Path) -> int:
    """One file, through a temporary name.

    A half-downloaded model.onnx that is named model.onnx is a model the
    rest of the code will try to load, and the error it produces will be
    about ONNX rather than about the network."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    scratch = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url) as response, open(scratch, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                out.write(chunk)
                print(f"\r  {destination.name}: {out.tell() / 1e6:.0f} MB", end="", flush=True)
    except (urllib.error.URLError, OSError) as e:
        scratch.unlink(missing_ok=True)
        raise SystemExit(
            f"\nCould not fetch {url}\n  {e}\n\n"
            "If this is a network policy rather than a typo, the model can be "
            "downloaded anywhere and copied into "
            f"{destination.parent} -- nothing here needs to be the thing that fetches it."
        ) from e
    size = scratch.stat().st_size
    scratch.replace(destination)
    print(f"\r  {destination.name}: {size / 1e6:.0f} MB  sha256 {digest.hexdigest()[:16]}…")
    return size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--full", action="store_true",
                    help="the float32 model (~440 MB) instead of the quantised one")
    ap.add_argument("--check", action="store_true", help="report what is present, fetch nothing")
    ap.add_argument("--force", action="store_true", help="fetch again over what is there")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus import embeddings

    directory = target_dir(args.base_dir)
    state = embeddings.status(args.base_dir)
    if args.check:
        print(f"model:   {'present' if state['model'] else 'absent'}  ({directory})")
        print(f"vectors: {'built' if state['vectors'] else 'not built'}"
              + (f"  ({len(state['sections'])} sections)" if state["sections"] else ""))
        if state["model"] and not state["vectors"]:
            print("\nNext: python3 -m corpus.embeddings --base-dir " + args.base_dir)
        return 0

    if state["model"] and not args.force:
        print(f"Already in {directory}. Pass --force to fetch it again.")
        return 0

    total = 0
    print(f"Fetching {REPO} into {directory}")
    for suffix, name in FILES + [FULL if args.full else QUANTISED]:
        total += fetch(f"{BASE}/{suffix}", directory / name)
    print(f"\n{total / 1e6:.0f} MB in {directory}")
    print("\nNext, build the vectors (a few minutes over the whole corpus):")
    print(f"  python3 -m corpus.embeddings --base-dir {args.base_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Each file is written under a .part name and renamed only when
        # it is whole, so there is nothing to clean up here.
        raise SystemExit("\nStopped.")
