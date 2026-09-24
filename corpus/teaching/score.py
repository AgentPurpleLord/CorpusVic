"""The parser, checked against every decision a reviewer has made.

Re-reads the Act's own PDF with the parser as it stands, in memory --
nothing saved, no review data touched -- and asks of each example whether
the parser now opens on its line what the reviewer said is there. The
last run is kept (gitignored), so a change that breaks what used to pass
is named as newly broken, not lost among the failures that were always
there.

    python -m corpus.teaching.score criminal-procedure-act-v114
"""
import argparse
import json
import sys
from pathlib import Path

from corpus import PROJECT_ROOT
from corpus.teaching import examples as teaching_examples


def body_pages(act: str, base_dir=None) -> list:
    """This Act's body pages, as run_pipeline reads them: the part of a
    re-parse worth doing once when the parser runs over it twice."""
    from corpus.parsing.endnotes import detect_endnotes_start
    from corpus.parsing.extract import extract_pages
    from corpus.parsing.toc import detect_body_start

    base = Path(base_dir or PROJECT_ROOT)
    parsed = json.loads((base / "data" / "parsed" / f"{act}.json").read_text(encoding="utf-8"))
    pages = extract_pages(str(base / parsed["source"]))
    start = detect_body_start(pages) - 1
    end = detect_endnotes_start(pages)
    return pages[start:(end - 1) if end else len(pages)]


def reparse(act: str, base_dir=None, learned: "list[dict] | None" = None, body: "list | None" = None) -> list[dict]:
    """This Act's PDF read by the current parser. `learned` stands in for
    the approved rules, to try the parser with a rule not yet approved."""
    from corpus.domain.profiles import profile_for
    from corpus.parsing.run_pipeline import run_parser

    base = Path(base_dir or PROJECT_ROOT)
    parsed = json.loads((base / "data" / "parsed" / f"{act}.json").read_text(encoding="utf-8"))
    if learned is None:
        from corpus.teaching.rules import for_act
        learned = for_act(act, base_dir)
    nodes, _result = run_parser(body if body is not None else body_pages(act, base_dir), act,
                                parsed.get("profile") or profile_for(act),
                                document_type=parsed.get("document_type") or "act", learned=learned)
    return nodes


def _opened_at(nodes: list[dict]) -> dict:
    at: dict = {}
    for node in nodes:
        seen = node.get("seen")
        if seen:
            at.setdefault((seen["page"], round(seen["y0"])), []).append(node)
    return at


def _split_inside(e: dict, by_page: dict) -> "dict | None":
    """A node the parse opens part-way down a piece the reviewer kept
    whole: a line that carries its text on, read as a new provision."""
    if e["expected"] is None:
        return None
    for r in e.get("rects") or []:
        for n in by_page.get(r["page"], ()):
            s = n["seen"]
            # The line's middle, not its top: under tight leading a box's
            # last line reaches below the top of the line after it (s 131's
            # heading over its (1)).
            middle = s["y0"] + (s.get("size") or 12.0) / 2
            if r["y0"] + 1 < s["y0"] and middle < r["y1"] and r["x0"] - 2 <= s["x0"] <= r["x1"] \
                    and not (s["page"] == e["seen"]["page"] and abs(s["y0"] - e["seen"]["y0"]) <= 1):
                return n
    return None


def check(examples: list[dict], nodes: list[dict]) -> list[dict]:
    """Each example with what the parse now opens on its line ("got") and
    whether that is what the reviewer said -- and nothing opened inside
    the piece, which is what a false split does ("split")."""
    at = _opened_at(nodes)
    by_page: dict = {}
    for n in nodes:
        if n.get("seen"):
            by_page.setdefault(n["seen"]["page"], []).append(n)
    out = []
    for e in examples:
        seen = e["seen"]
        found = [n for dy in (0, -1, 1) for n in at.get((seen["page"], round(seen["y0"]) + dy), [])
                 if n["seen"]["text"][:30] == seen["text"][:30]]
        got = teaching_examples.said(found[0]) if found else None
        split = _split_inside(e, by_page)
        out.append({**e, "got": got, "passed": teaching_examples.agree(got, e["expected"]) and split is None,
                    "split": None if split is None else {"seen": split["seen"], "got": teaching_examples.said(split)}})
    return out


def last_failures(base_dir=None) -> list[dict]:
    """Every Act's failures as its last check left them."""
    out = []
    for path in sorted((Path(base_dir or PROJECT_ROOT) / "data" / "teaching" / ".last").glob("*.json")):
        out.extend(json.loads(path.read_text(encoding="utf-8")).get("failures", []))
    return out


def _last_path(act: str, base_dir=None) -> Path:
    return Path(base_dir or PROJECT_ROOT) / "data" / "teaching" / ".last" / f"{act}.json"


def score(act: str, base_dir=None, nodes: "list[dict] | None" = None) -> dict:
    """Passed and failed, and which of the failures passed last time."""
    results = check(teaching_examples.load(act, base_dir), reparse(act, base_dir) if nodes is None else nodes)
    last_path = _last_path(act, base_dir)
    passed_before = set(json.loads(last_path.read_text(encoding="utf-8")).get("passed", [])) if last_path.exists() else set()
    passed = [r["id"] for r in results if r["passed"]]
    failures = [{k: r[k] for k in ("id", "kind", "parser", "expected", "got")}
                | {"page": r["seen"]["page"], "text": r["seen"]["text"],
                   "split": r["split"] and {"page": r["split"]["seen"]["page"], "text": r["split"]["seen"]["text"],
                                            "got": r["split"]["got"]}}
                for r in results if not r["passed"]]
    summary = {
        "act": act, "total": len(results), "passed": len(passed), "failed": len(failures),
        "newly_broken": [f for f in failures if f["id"] in passed_before],
        "failures": failures,
    }
    last_path.parent.mkdir(parents=True, exist_ok=True)
    # The failures in full, with what the parser saw, for the rule miner
    # (corpus/teaching/propose.py).
    last_path.write_text(json.dumps({"passed": passed, "failures": [r for r in results if not r["passed"]]}),
                         encoding="utf-8")
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act")
    s = score(ap.parse_args().act)
    print(f"{s['act']}: {s['passed']} of {s['total']} examples pass; {s['failed']} fail, "
          f"{len(s['newly_broken'])} newly broken")
    for f in s["newly_broken"] + [f for f in s["failures"] if f not in s["newly_broken"]][:40]:
        print(f"  p{f['page']} {f['text'][:60]!r}: you said {f['expected']}, the parser says {f['got']}")
    return 0 if not s["newly_broken"] else 1


if __name__ == "__main__":
    sys.exit(main())
