"""Lines that look like one you just corrected, to correct the same way.

"Alike" is the rule one correction would teach (corpus/teaching/propose):
the same opening shape, weight, size and indent, met under the same kind
of open provision. A retype doesn't look at how the line above ended; a
merge does, since a false split is a line the parser took for a new
provision because of how the line above it ended.
"""
import re

from corpus.teaching import propose, rules


def rule_for(seen: dict, number: "str | None", merging: bool) -> dict:
    when = {"pattern": propose.shape(seen["text"], number) or propose.shape(seen["text"], None),
            **propose.conditions([{"seen": seen}])}
    if not merging:
        when.pop("above_clean", None)
        when.pop("above_full", None)
    return {"when": when, "then": {"continue": True} if merging else {}}


def number_in(rule: dict, text: str) -> "str | None":
    """The number a look-alike carries where the corrected line carried
    the one you gave it."""
    m = re.match(rule["when"]["pattern"], text)
    return m.group(1) if m and m.groups() else None


def describe(rule: dict, fix: str) -> str:
    return rules.describe({**rule, "then": {"continue": True}}).rsplit(":", 1)[0] + f": {fix}."
