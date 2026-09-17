"""
Scoring search against a set of queries whose answers are known.

Relevance work is the kind you cannot feel your way through. Every change
to ranking helps some queries and hurts others, and the only evidence
that usually gets collected is whichever query somebody happened to try
last. So the queries live in data/search_eval.yaml, the answers live
beside them, and the result is a number.

Mean reciprocal rank, because it asks the question a search box is
actually judged by: how far down did I have to look? Rank 1 scores 1.0,
rank 2 scores 0.5, rank 10 scores 0.1, and anything past the cut scores
nothing at all -- which is right, since nobody reads the third page.
"""
from pathlib import Path

DEFAULT_CUTOFF = 20


def load_eval(path) -> list[dict]:
    """The evaluation set, as written.

    An entry may name a `group`. Queries in different groups are not
    averaged together for the purpose of the floor, because they are not
    the same question: one group is what search already answers and must
    go on answering, the other is what it cannot answer yet. Averaging
    the two produces a number that falls when you write down a problem
    and rises when you stop looking at it."""
    import yaml

    rows = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    for row in rows:
        missing = {"query", "doc", "page"} - set(row)
        if missing:
            raise ValueError(f"eval entry {row.get('query')!r} is missing {sorted(missing)}")
    return rows


def rank_of(results: list[dict], doc: str, page: str) -> "int | None":
    """Where the expected provision came, 1-based, or None if it did not.

    Matched on the document and the page rather than on the heading: a
    heading can change when an Act is re-parsed, and a relevance score
    should not move because somebody corrected a typo in the text."""
    for position, hit in enumerate(results, start=1):
        if hit.get("site_slug") == doc and hit.get("page") == page:
            return position
    return None


def score(search, rows: list[dict], cutoff: int = DEFAULT_CUTOFF) -> dict:
    """Runs every query and reports how well they did.

    `search` is called with one query and returns the result list --
    passed in so this module needs no opinion about which search it is
    scoring, which is what lets it compare two of them."""
    scored = []
    for row in rows:
        results = search(row["query"])[:cutoff]
        rank = rank_of(results, row["doc"], row["page"])
        scored.append({
            "query": row["query"],
            "group": row.get("group", "core"),
            "expected": f"{row['doc']}/{row['page']}",
            "rank": rank,
            "reciprocal": 1.0 / rank if rank else 0.0,
            "found": results[0].get("label") if results else None,
        })
    total = len(scored) or 1
    return {
        "mrr": sum(s["reciprocal"] for s in scored) / total,
        "at_1": sum(1 for s in scored if s["rank"] == 1),
        "at_5": sum(1 for s in scored if s["rank"] and s["rank"] <= 5),
        "missed": [s["query"] for s in scored if s["rank"] is None],
        "queries": scored,
    }


def by_group(result: dict) -> dict:
    """The same figures, per group."""
    groups: dict = {}
    for s in result["queries"]:
        groups.setdefault(s["group"], []).append(s)
    return {
        name: {
            "mrr": sum(s["reciprocal"] for s in rows) / (len(rows) or 1),
            "at_1": sum(1 for s in rows if s["rank"] == 1),
            "at_5": sum(1 for s in rows if s["rank"] and s["rank"] <= 5),
            "queries": rows,
        }
        for name, rows in groups.items()
    }


def report(result: dict) -> str:
    """The scoreboard, for a terminal."""
    lines = [f"MRR {result['mrr']:.3f}   rank 1: {result['at_1']}/{len(result['queries'])}"
             f"   top 5: {result['at_5']}/{len(result['queries'])}"]
    for name, figures in sorted(by_group(result).items()):
        lines.append(f"    {name:16} MRR {figures['mrr']:.3f}   rank 1: "
                     f"{figures['at_1']}/{len(figures['queries'])}")
    lines.append("")
    for s in result["queries"]:
        where = f"rank {s['rank']}" if s["rank"] else "NOT FOUND"
        lines.append(f"  {where:>10}  {s['query'][:44]:46} -> {s['expected']}")
        if s["rank"] != 1:
            lines.append(f"              {'':46}    top was: {(s['found'] or '-')[:52]}")
    return "\n".join(lines)


def main():
    """The scoreboard, from a terminal.

        python3 -m corpus.relevance

    Here so that measuring is one command on whichever machine has the
    index -- including a server that has the semantic half installed and
    this one does not. Two runs of this, before and after, is what a
    claim about relevance has to be made of."""
    import argparse
    import sys
    import time

    ap = argparse.ArgumentParser(description="Score search against data/search_eval.yaml.")
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--limit", type=int, default=DEFAULT_CUTOFF)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from corpus import search

    base = Path(args.base_dir)
    index = search.Index(base)
    if not index.available():
        raise SystemExit("No search index. Build it from the dashboard, "
                         "or: python3 -m corpus.search --build")

    rows = load_eval(base / "data" / "search_eval.yaml")
    started = time.time()
    scored = score(lambda q: index.search(q, limit=args.limit)["results"], rows, args.limit)
    print(report(scored))
    print(f"\n{len(rows)} queries in {time.time() - started:.1f}s")

    try:
        from corpus import embeddings

        state = embeddings.status(base)
    except ImportError:
        state = {"model": False, "vectors": False}
    print("semantic half: " + ("on" if (state["model"] and state["vectors"]) else "off")
          + (f" ({len(state['sections'])} sections)" if state.get("sections") else ""))


if __name__ == "__main__":
    main()
