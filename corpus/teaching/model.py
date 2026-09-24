"""A second opinion learned from your decisions: a shallow decision tree.

Trained on every example (corpus/teaching/examples.py) -- what a printed
line looked like, and what you said it is -- it reads each undecided
piece in review and flags the ones where it and the parser disagree. It
advises and never changes a parse. A tree, not anything cleverer, so each
flag can say which features decided it; and small enough to write here
rather than add a library to the server for.

Its accuracy is measured on Acts it wasn't trained on, so you can see
whether it is worth listening to before it says anything.

    python -m corpus.teaching.model
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from corpus import PROJECT_ROOT
from corpus.teaching import rules

CONTINUES = "(carries on the line above)"
MAX_DEPTH = 6
MIN_LEAF = 3
# How sure and how well-founded a leaf must be before review hears of it.
FLAG_PURITY = 0.85
FLAG_SUPPORT = 8
_NUMERIC = ("size_ratio", "indent")
_BOOLEAN = ("bold", "above_clean", "above_full", "lbi")
_CATEGORICAL = ("opening", "open_type", "marker_above")
# A Note or Example is headed by its marker word on the line above.
_MARKER_RE = re.compile(r"^(Notes?|Examples?)\s*[—–:]?$")

_OPENINGS = [
    ("(1)", r"\(\d+[A-Z]*\)"), ("(a)", r"\([a-z]+\)"), ("(A)", r"\([A-Z]+\)"),
    ("1.1", r"\d+\.\d+[A-Z]*"), ("1", r"\d+[A-Z]*"), ("Word—", r"[A-Z][a-z]+[—–:]"),
    ("Word", r"[A-Z][a-z]*"), ("word", r"[a-z]+"),
]


def opening(text: str) -> str:
    """The kind of thing a line opens with -- "(1)", "(a)", "1.1", a
    capitalised word -- the part of its text the layout alone can't say."""
    token = (text.split() or [""])[0]
    return next((name for name, pattern in _OPENINGS if re.fullmatch(pattern, token)), "other")


def vector(seen: dict) -> dict:
    f = rules.features(seen)
    return {"bold": f["bold"], "size_ratio": f["size_ratio"], "indent": f["indent"],
            "above_clean": f["above_clean"], "above_full": f["above_full"], "lbi": bool(seen.get("lbi")),
            "opening": opening(f["text"]), "open_type": f["parent_type"], "marker_above": _marker(seen.get("above"))}


def _marker(above: "dict | None") -> "str | None":
    m = _MARKER_RE.match((above or {}).get("text", "").strip())
    return m.group(1).rstrip("s").lower() if m else None


def label(example: dict) -> str:
    return example["expected"]["type"] if example["expected"] else CONTINUES


# -- The tree ---------------------------------------------------------------

def _gini(counts: Counter) -> float:
    n = sum(counts.values())
    return 1.0 - sum((c / n) ** 2 for c in counts.values()) if n else 0.0


def _holds(test: dict, x: dict) -> bool:
    v = x.get(test["feature"])
    if test["feature"] in _NUMERIC:
        return v is not None and v <= test["value"]
    return v == test["value"]


def _candidates(xs: list[dict]):
    for feature in _BOOLEAN:
        yield {"feature": feature, "value": True}
    for feature in _CATEGORICAL:
        # Sorted, so a tie between two splits falls the same way every run.
        for value in sorted({x[feature] for x in xs if x[feature] is not None}):
            yield {"feature": feature, "value": value}
    for feature in _NUMERIC:
        values = sorted({x[feature] for x in xs if x[feature] is not None})
        step = max(1, len(values) // 24)   # a few dozen thresholds is plenty, and keeps training quick
        for a, b in zip(values[::step], values[step::step]):
            yield {"feature": feature, "value": round((a + b) / 2, 2)}


def _grow(xs: list[dict], ys: list[str], depth: int) -> dict:
    counts = Counter(ys)
    leaf = {"label": counts.most_common(1)[0][0], "support": len(ys),
            "purity": round(counts.most_common(1)[0][1] / len(ys), 3)}
    if depth >= MAX_DEPTH or len(counts) == 1 or len(ys) < 2 * MIN_LEAF:
        return leaf
    best, best_score = None, _gini(counts)
    for test in _candidates(xs):
        yes = Counter(y for x, y in zip(xs, ys) if _holds(test, x))
        n_yes = sum(yes.values())
        if n_yes < MIN_LEAF or len(ys) - n_yes < MIN_LEAF:
            continue
        score = (n_yes * _gini(yes) + (len(ys) - n_yes) * _gini(counts - yes)) / len(ys)
        if score < best_score - 1e-9:
            best, best_score = test, score
    if best is None:
        return leaf
    yes = [i for i, x in enumerate(xs) if _holds(best, x)]
    no = [i for i in range(len(xs)) if not _holds(best, xs[i])]
    grown = {**best, "yes": _grow([xs[i] for i in yes], [ys[i] for i in yes], depth + 1),
             "no": _grow([xs[i] for i in no], [ys[i] for i in no], depth + 1)}
    if "label" in grown["yes"] and "label" in grown["no"] and grown["yes"]["label"] == grown["no"]["label"]:
        return leaf   # a split that decides nothing only lengthens the explanation
    return grown


def train(examples: list[dict]) -> "dict | None":
    if not examples:
        return None
    return _grow([vector(e["seen"]) for e in examples], [label(e) for e in examples], 0)


def _words(test: dict, held: bool) -> str:
    feature, value = test["feature"], test["value"]
    if feature == "size_ratio":
        return f"set {'at or under' if held else 'over'} {value:g} of body size"
    if feature == "indent":
        return f"indented {'at most' if held else 'more than'} {value:g}pt from what is open"
    if feature == "opening":
        return f"opens {'like' if held else 'unlike'} “{value}”"
    if feature == "open_type":
        return f"{'with' if held else 'without'} a {value} open"
    if feature == "marker_above":
        return f"{'under' if held else 'not under'} a “{value.capitalize()}” marker"
    return {"bold": "bold", "above_clean": "after a finished line", "above_full": "after a full line",
            "lbi": "with a bold-italic lead"}[feature] if held else {
            "bold": "not bold", "above_clean": "after an unfinished line", "above_full": "after a short line",
            "lbi": "no bold-italic lead"}[feature]


def predict(tree: dict, seen: dict) -> dict:
    """What the tree says the line is, how sure its leaf is, and why."""
    x, held_why, other_why = vector(seen), [], []
    node = tree
    while "label" not in node:
        held = _holds(node, x)
        (held_why if held else other_why).append(_words(node, held))
        node = node["yes" if held else "no"]
    # What the line is says more than a list of what it isn't.
    return {"label": node["label"], "purity": node["purity"], "support": node["support"],
            "why": held_why or other_why}


def disagrees(trained: dict, node: dict) -> "dict | None":
    """The tree's reading of a parsed node when it is sure of one the
    parser didn't give. Silent on a type it was never taught: having
    never seen a Schedule item, it would call every one a section."""
    if not node.get("seen") or node.get("type") not in trained.get("labels", {}):
        return None
    p = predict(trained["tree"], node["seen"])
    if p["purity"] < FLAG_PURITY or p["support"] < FLAG_SUPPORT or p["label"] == node.get("type"):
        return None
    return p


# -- Kept, and measured -----------------------------------------------------

def model_path(base_dir=None) -> Path:
    return Path(base_dir or PROJECT_ROOT) / "data" / "teaching" / ".model.json"


def all_examples(base_dir=None) -> list[dict]:
    from corpus.teaching import examples

    out = []
    for path in sorted((Path(base_dir or PROJECT_ROOT) / "data" / "teaching").glob("*.jsonl")):
        out.extend(examples.load(path.stem, base_dir))
    return out


def _accuracy(tree, test: list[dict]) -> dict:
    right = sum(predict(tree, e["seen"])["label"] == label(e) for e in test)
    parser = sum((e["parser"] or {}).get("type") == (e["expected"] or {}).get("type") for e in test)
    return {"lines": len(test), "model": round(right / len(test), 3), "parser": round(parser / len(test), 3)}


def held_out(examples: list[dict]) -> list[dict]:
    """Each Act scored by a tree trained on the others. With only one Act
    there are no others, so a fifth of its lines are held back instead --
    a weaker test, and said so."""
    acts = sorted({e["act"] for e in examples})
    if len(acts) > 1:
        out = []
        for act in acts:
            tree = train([e for e in examples if e["act"] != act])
            out.append({"held_out": act, **_accuracy(tree, [e for e in examples if e["act"] == act])})
        return out
    fifth = lambda e: int(hashlib.sha1(e["id"].encode()).hexdigest(), 16) % 5 == 0
    test = [e for e in examples if fifth(e)]
    if not test:
        return []
    return [{"held_out": f"a fifth of {acts[0]}'s lines", **_accuracy(train([e for e in examples if not fifth(e)]), test)}]


def build(base_dir=None) -> dict:
    """Trains on every example held, measures it, and keeps it for review
    (gitignored: it is rebuilt from the examples, which are committed)."""
    examples = all_examples(base_dir)
    if not examples:
        raise ValueError("No examples yet. Run a Teaching check on an Act you have reviewed.")
    model = {"tree": train(examples), "examples": len(examples), "acts": sorted({e["act"] for e in examples}),
             "held_out": held_out(examples), "labels": dict(Counter(label(e) for e in examples))}
    path = model_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model), encoding="utf-8")
    return {k: v for k, v in model.items() if k != "tree"}


def load(base_dir=None) -> "dict | None":
    path = model_path(base_dir)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    print(json.dumps(build(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
