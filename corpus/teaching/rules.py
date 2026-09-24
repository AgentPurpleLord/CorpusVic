"""Rules learned from review, which the parser applies once you approve them.

A rule says: a line that looks like this (its text, weight, size, indent
and the line above it -- the same `seen` record every node keeps) is
this. It is matched before the parser's own judgement, so an approved rule
overrides it, and nothing else.

Kept in data/teaching/rules.json -- decisions, like the review data, so
the dashboard's sync carries them -- as {"rules": [...], "rejected":
[signature, ...]}. A rule's scope is a work ("criminal-procedure-act",
every version of it) or "all".
"""
import hashlib
import json
import re
from pathlib import Path

from corpus import PROJECT_ROOT

# The line above finished a sentence or an item (rule_parser's own test).
_CLEAN_END_RE = re.compile(r"(?:[.;:—–*]|;\s*(?:or|and|and/or))\s*$")
# How near the margin a line has to reach to count as full.
_FULL_WITHIN = 15.0


def rules_path(base_dir=None) -> Path:
    return Path(base_dir or PROJECT_ROOT) / "data" / "teaching" / "rules.json"


def load_all(base_dir=None) -> dict:
    path = rules_path(base_dir)
    if not path.exists():
        return {"rules": [], "rejected": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_all(data: dict, base_dir=None) -> None:
    path = rules_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def for_act(act: "str | None", base_dir=None) -> list[dict]:
    """The approved rules that apply to this document."""
    from corpus.parsing.versions import split_document_slug

    work = split_document_slug(act)[0] if act else None
    return [r for r in load_all(base_dir)["rules"] if r.get("scope") in ("all", work, act)]


def signature(rule: dict) -> str:
    """What a rule says, not when or from what it was learned: two
    proposals that say the same thing are one, and a rejected one stays
    rejected."""
    core = json.dumps({"when": rule["when"], "then": rule["then"]}, sort_keys=True)
    return hashlib.sha1(core.encode("utf-8")).hexdigest()[:12]


def features(seen: dict) -> dict:
    """The measures a rule's conditions read, from a `seen` record: the
    line against what was open when it arrived, as the parser met it.
    ("parent_type" names that. A record made before `open` was kept has
    only where the node ended up.)"""
    above = seen.get("above")
    parent = seen["open"] if "open" in seen else seen.get("parent")
    body = seen.get("body") or 12.0
    margin = seen.get("margin")
    return {
        "text": seen.get("text") or "",
        "bold": bool(seen.get("bold")),
        "size_ratio": round((seen.get("size") or body) / body, 2),
        "indent": None if not parent or parent.get("x0") is None else round(seen["x0"] - parent["x0"], 1),
        "parent_type": parent["type"] if parent else None,
        "above_clean": None if above is None else bool(_CLEAN_END_RE.search(above["text"]) or above.get("bold")),
        "above_full": None if above is None or margin is None else above["x1"] >= margin - _FULL_WITHIN,
    }


def matches(rule: dict, seen: dict) -> "re.Match | bool":
    """Whether every condition of the rule holds for this line. Returns
    the text match, for the number it may carry."""
    when, f = rule["when"], features(seen)
    m = re.match(when["pattern"], f["text"]) if "pattern" in when else True
    if not m:
        return False
    for key in ("bold", "above_clean", "above_full"):
        if key in when and f[key] != when[key]:
            return False
    for key in ("size_ratio", "indent"):
        if key in when:
            lo, hi = when[key]
            if f[key] is None or not lo <= f[key] <= hi:
                return False
    if "parent_type" in when and f["parent_type"] not in when["parent_type"]:
        return False
    return m


def describe(rule: dict) -> str:
    """The rule in words, for the page you approve it on."""
    when, then = rule["when"], rule["then"]
    bits = []
    if "pattern" in when:
        bits.append(f"a line matching {when['pattern']}")
    if "bold" in when:
        bits.append("in bold" if when["bold"] else "not bold")
    if "size_ratio" in when:
        lo, hi = when["size_ratio"]
        bits.append(f"set at {lo:g}–{hi:g} of body size")
    if "indent" in when:
        lo, hi = when["indent"]
        bits.append(f"indented {lo:g} to {hi:g}pt from the provision open above it")
    if "parent_type" in when:
        bits.append(f"while a {' or '.join(when['parent_type'])} is open")
    if "above_clean" in when:
        bits.append("after a line that finished" if when["above_clean"] else "after a line that stopped mid-sentence")
    if "above_full" in when:
        bits.append("which ran to the margin" if when["above_full"] else "which stopped short of the margin")
    if "open" in then:
        does = f"starts a new {then['open']}" + (", numbered as printed" if then.get("number") else "")
    else:
        does = "carries on the text above it, not a new provision"
    said = ", ".join(bits) or "any line"
    return f"{said[0].upper()}{said[1:]}: {does}."


def approve(candidate: dict, scope: str, base_dir=None) -> dict:
    """Adds a proposal to the rules the parser applies."""
    from datetime import datetime, timezone

    data = load_all(base_dir)
    rule = {"id": candidate["id"], "when": candidate["when"], "then": candidate["then"], "scope": scope,
            "learned_from": candidate.get("learned_from", []),
            "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    data["rules"] = [r for r in data["rules"] if r["id"] != rule["id"]] + [rule]
    save_all(data, base_dir)
    return rule


def reject(rule_id: str, base_dir=None) -> None:
    """Remembered, so the same rule isn't proposed again. Withdrawing an
    approved rule is rejecting it."""
    data = load_all(base_dir)
    data["rules"] = [r for r in data["rules"] if r["id"] != rule_id]
    data["rejected"] = sorted(set(data["rejected"]) | {rule_id})
    save_all(data, base_dir)
