"""A work's instructions from the amending Acts fetched for it, as the
review tool and the public histories both read them: keyed by the
provision each changes, and with the reprints each Act first shows in,
since an instruction only says something about the change into those.
"""
import json
import re
from pathlib import Path

from corpus.amending.fetch import instructions_path
from corpus.amending.scope import acts_between


def key_of(ins: dict) -> tuple:
    """The provision key (diffing.provisions') an instruction changes. A
    whole new section is its own provision, not the one it follows."""
    if ins.get("schedule"):
        return ("schedule", None, ins["schedule"])
    return ("provision", None, (ins.get("section") or "").lower())


def work_title(slug: str, base_dir, pdf_title: "str | None" = None) -> "str | None":
    """The Act's title as amending Acts name it: the one its PDF gives, or,
    without the PDF, its own first entry in its Table of Amendments."""
    if pdf_title and re.search(r" Act \d{4}$", pdf_title):
        return pdf_title
    path = Path(base_dir) / "data" / "parsed" / f"{slug}.json"
    if path.exists():
        table = (json.loads(path.read_text(encoding="utf-8")).get("endnotes") or {}).get("amending_acts") or []
        if table:
            return table[0].get("title")
    return None


def work_instructions(slug: str, base_dir, title: "str | None") -> dict:
    """{"by_key": {provision key: [instruction]}, "first": {citation: the
    reprints its amendments first show in}} -- only Acts whose
    instructions have been read, and only instructions to this Act."""
    out: dict = {"by_key": {}, "first": {}}
    if not title:
        return out
    try:
        acts = acts_between(slug, Path(base_dir))
    except (OSError, ValueError, KeyError):
        return out
    for act in acts:
        path = instructions_path(act["citation"], Path(base_dir))
        if not path.exists():
            continue
        out["first"][act["citation"]] = set(act["versions"])
        for ins in json.loads(path.read_text(encoding="utf-8")):
            # One not recognised names no provision, so there is nothing
            # to check it against; its words stay in data/amending.
            if ins.get("target_act") == title and (ins.get("section") or ins.get("schedule")):
                out["by_key"].setdefault(key_of(ins), []).append(ins)
    return out


def fetched_for(work: dict, version: int) -> set:
    """The Acts fetched whose amendments first show in `version`."""
    return {citation for citation, versions in work["first"].items() if version in versions}
