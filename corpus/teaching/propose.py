"""Rules proposed from where the parser and your decisions disagree.

Failures that went wrong the same way (the parser said X, you said Y) on
lines that look alike (their opening, weight, size, indent, the line
above) are one lesson. Three make a proposal. Nothing is applied until
you approve it on the Lessons page, and a preview first shows every line
across the held Acts it would change.

    python -m corpus.teaching.propose
"""
import argparse
import json
import re
import sys
from pathlib import Path

from corpus import PROJECT_ROOT
from corpus.teaching import examples as teaching_examples, rules

MIN_EXAMPLES = 3
# Types a rule can't state: a definition needs its term, a repealed
# marker comes from a run of asterisks, not a line.
_UNSTATABLE = {"definition", "repealed"}
_TAIL = r"(?:\s|$)"


def _number_class(number: str) -> "str | None":
    if re.fullmatch(r"\d+(?:\.\d+)*[A-Z]*", number):
        return r"\d+(?:\.\d+)*[A-Z]*"
    if re.fullmatch(r"[a-z]+", number):
        return "[a-z]+"
    if re.fullmatch(r"[A-Z]+", number):
        return "[A-Z]+"
    return None


def shape(text: str, number: "str | None") -> "str | None":
    """The line's opening word as a pattern: its brackets and punctuation
    as printed, the number you gave it captured as any number like it,
    other digits as any digits."""
    words = text.split()
    if not words:
        return None
    token = words[0]
    if number:
        i, cls = token.find(number), _number_class(number)
        prefix, suffix = token[:i], token[i + len(number):]
        if i < 0 or cls is None or any(c.isalnum() for c in prefix + suffix):
            return None   # the number isn't the line's opening, so a rule couldn't read it
        return f"^{re.escape(prefix)}({cls}){re.escape(suffix)}{_TAIL}"
    return "^" + "".join(r"\d+" if part.isdigit() else re.escape(part)
                         for part in re.split(r"(\d+)", token) if part) + _TAIL


def _conditions(group: list[dict]) -> dict:
    """What every example in the group has in common."""
    fs = [rules.features(f["seen"]) for f in group]
    when = {}
    for key in ("bold", "above_clean", "above_full"):
        values = {f[key] for f in fs}
        if len(values) == 1 and None not in values:
            when[key] = values.pop()
    for key, pad, spread in (("size_ratio", 0.03, 0.2), ("indent", 3.0, 15.0)):
        values = [f[key] for f in fs]
        if None not in values and max(values) - min(values) <= spread:
            when[key] = [round(min(values) - pad, 2), round(max(values) + pad, 2)]
    parents = {f["parent_type"] for f in fs}
    if None not in parents and len(parents) <= 3:
        when["parent_type"] = sorted(parents)
    return when


def _brief(f: dict) -> dict:
    return {"id": f["id"], "act": f["act"], "page": f["seen"]["page"], "text": f["seen"]["text"],
            "expected": f["expected"], "got": f["got"]}


def propose(failures: list[dict], decided: "set[str]" = frozenset()) -> list[dict]:
    """Candidate rules, most-supported first, leaving out any already
    approved or rejected (`decided`, by signature)."""
    from corpus.parsing.versions import split_document_slug

    groups: dict = {}
    for f in failures:
        expected = f["expected"]
        if expected and expected["type"] in _UNSTATABLE:
            continue
        number = expected.get("number") if expected else None
        pattern = shape(f["seen"]["text"], number)
        if pattern is None:
            continue
        got = f["got"]["type"] if f.get("got") else None
        key = (got, expected["type"] if expected else None, bool(number), pattern)
        groups.setdefault(key, []).append(f)

    out = []
    for (_got, want, numbered, pattern), group in groups.items():
        if len(group) < MIN_EXAMPLES:
            continue
        rule = {
            "when": {"pattern": pattern, **_conditions(group)},
            "then": {"continue": True} if want is None else {"open": want, "number": numbered},
        }
        rule["id"] = rules.signature(rule)
        if rule["id"] in decided:
            continue
        works = {split_document_slug(f["act"])[0] for f in group}
        rule["scope"] = works.pop() if len(works) == 1 else "all"
        rule["learned_from"] = sorted(f["id"] for f in group)
        rule["examples"] = [_brief(f) for f in group[:12]]
        rule["count"] = len(group)
        rule["description"] = rules.describe(rule)
        out.append(rule)
    return sorted(out, key=lambda r: -r["count"])


def candidates(base_dir=None) -> list[dict]:
    """The proposals the last check of every Act gives."""
    from corpus.teaching.score import last_failures

    data = rules.load_all(base_dir)
    decided = set(data["rejected"]) | {r["id"] for r in data["rules"]}
    return propose(last_failures(base_dir), decided)


def acts_in_scope(scope: str, base_dir=None) -> list[str]:
    """The held Acts a rule of this scope reads: those parsed whose PDF is
    still here."""
    from corpus.parsing.versions import split_document_slug

    base = Path(base_dir or PROJECT_ROOT)
    out = []
    for path in sorted((base / "data" / "parsed").glob("*.json")):
        act = path.stem
        if scope != "all" and scope not in (act, split_document_slug(act)[0]):
            continue
        source = json.loads(path.read_text(encoding="utf-8")).get("source")
        if source and (base / source).exists():
            out.append(act)
    return out


def _by_line(nodes: list[dict]) -> dict:
    out = {}
    for n in nodes:
        seen = n.get("seen")
        if seen:
            out.setdefault((seen["page"], round(seen["y0"]), seen["text"][:30]), (seen["text"], []))[1].append(
                teaching_examples.said(n))
    return out


def preview(rule: dict, base_dir=None, acts: "list[str] | None" = None, progress=None) -> dict:
    """The parser run over each held Act in the rule's scope with and
    without it: every line whose reading changes, and which of your
    examples it fixes or breaks."""
    from corpus.teaching import score

    changes, fixed, broken = [], [], []
    acts = acts_in_scope(rule["scope"], base_dir) if acts is None else acts
    for i, act in enumerate(acts):
        if progress:
            progress(act, i, len(acts))
        body = score.body_pages(act, base_dir)
        current = rules.for_act(act, base_dir)
        before = _by_line(nodes_before := score.reparse(act, base_dir, learned=current, body=body))
        after = _by_line(nodes_after := score.reparse(act, base_dir, learned=current + [rule], body=body))
        for key in sorted(set(before) | set(after)):
            was, now = before.get(key, (None, [])), after.get(key, (None, []))
            if was[1] != now[1]:
                changes.append({"act": act, "page": key[0], "text": was[0] or now[0],
                                "before": was[1][0] if was[1] else None, "after": now[1][0] if now[1] else None})
        stored = teaching_examples.load(act, base_dir)
        passed_before = {r["id"] for r in score.check(stored, nodes_before) if r["passed"]}
        passed_after = {r["id"] for r in score.check(stored, nodes_after) if r["passed"]}
        fixed += [{"act": act, "id": x} for x in sorted(passed_after - passed_before)]
        broken += [{"act": act, "id": x} for x in sorted(passed_before - passed_after)]
    return {"acts": acts, "changes": changes, "fixed": fixed, "broken": broken}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    for c in candidates():
        print(f"{c['id']}  ({c['count']} examples, {c['scope']})  {c['description']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
