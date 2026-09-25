"""Checks every instruction a work's fetched amending Acts give it against
the reprints its Act first shows in, and says how each fared: made where
the Act says, found under another piece (the parse's misplacement),
already in force before, not found, or not checked.

Run on its own, apart from parsing, since the Acts are fetched and read
apart from it:

    python -m corpus.amending.verify criminal-procedure-act-v114
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from corpus import PROJECT_ROOT
from corpus.amending import load
from corpus.amending.fetch import instructions_path, manifest_path
from corpus.amending.match import match
from corpus.amending.scope import acts_between
from corpus.domain import diffing
from corpus.parsing.identity import annotate_ids
from corpus.review.inheritance import sibling_slugs
from corpus.storage import parsed as parsed_files

# Best first: across the reprints an Act shows in, an instruction is
# reported by the best it did in any of them.
_RANK = {"matched": 4, "elsewhere": 3, "earlier": 2, "unchecked": 1, "not found": 0}


def status(work_slug: str, base_dir=None) -> list[dict]:
    """The Acts this work's held reprints need, and how far each has got:
    fetched, and how many of its instructions were read. No network."""
    base = Path(base_dir or PROJECT_ROOT)
    manifest_file = manifest_path(base)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    out = []
    for act in acts_between(work_slug, base):
        path = instructions_path(act["citation"], base)
        read = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        out.append({
            "citation": act["citation"], "title": act["title"], "versions": act["versions"],
            "fetched": act["citation"] in manifest,
            "read": None if read is None else len(read),
            "unparsed": None if read is None else sum(i["action"] == "unparsed" for i in read),
        })
    return out


def _parsed_nodes(base: Path):
    def nodes_of(slug: str):
        parsed = parsed_files.load(base / "data" / "parsed" / f"{slug}.json")   # a slim one put back together
        hierarchy = parsed.get("hierarchy")
        annotate_ids(parsed["nodes"], hierarchy)
        return parsed["nodes"], hierarchy
    return nodes_of


def verify(work_slug: str, base_dir=None, nodes_of=None, title=None) -> dict:
    """{"acts": [{"citation", "title", "counts", "items"}], "counts"}.

    `nodes_of(slug)` gives a version's nodes and hierarchy -- the
    dashboard passes the reviewed text; run on its own, it is the parse."""
    from corpus.publishing.html_view import HIERARCHY_ORDER, _wording_units

    base = Path(base_dir or PROJECT_ROOT)
    nodes_of = nodes_of or _parsed_nodes(base)
    slugs = sibling_slugs(work_slug, base)
    versions = sorted(slugs)
    work = load.work_instructions(work_slug, base, title or load.work_title(slugs[versions[-1]], base))
    cache: dict = {}

    def units(version: int, key: tuple) -> list[dict]:
        if version not in cache:
            nodes, hierarchy = nodes_of(slugs[version])
            cache[version] = (nodes, hierarchy or HIERARCHY_ORDER, diffing.provisions(nodes))
        nodes, hierarchy, provisions = cache[version]
        p = provisions.get(key)
        return _wording_units(nodes[p["node_index"]:diffing.unit_end(nodes, p["node_index"])], hierarchy) if p else []

    report = []
    for act in acts_between(work_slug, base):
        mine = [(key, i) for key, group in work["by_key"].items() for i in group if i["act"] == act["citation"]]
        if act["citation"] not in work["first"]:
            continue
        best: dict = {}
        for version in act["versions"]:
            older = versions[versions.index(version) - 1] if versions.index(version) else None
            if older is None:
                continue
            for key in dict.fromkeys(k for k, _ in mine):
                group = [i for k, i in mine if k == key]
                for result in match(group, units(older, key), units(version, key))["instructions"]:
                    name = (result["provision"], tuple(result["path"]), result.get("section"), result.get("schedule"))
                    if name not in best or _RANK[result["status"]] > _RANK[best[name]["status"]]:
                        best[name] = {"provision": result["provision"], "target": _target(result),
                                      "status": result["status"], "at": result["at"],
                                      "between": f"v{older}→v{version}"}
        items = sorted(best.values(), key=lambda r: (-_RANK[r["status"]], r["provision"]))
        report.append({"citation": act["citation"], "title": act["title"],
                       "counts": dict(Counter(r["status"] for r in items)), "items": items})
    return {"acts": report, "counts": dict(sum((Counter(a["counts"]) for a in report), Counter()))}


def _target(ins: dict) -> str:
    if ins.get("schedule"):
        return f"Sch {ins['schedule']} item " + "".join([ins["path"][0]] + [f"({p})" for p in ins["path"][1:]])
    return f"s {ins.get('section')}" + "".join(f"({p})" for p in ins.get("path") or []) + (
        " heading" if ins.get("heading") else "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work", help="any version's slug, e.g. criminal-procedure-act-v114")
    args = ap.parse_args()
    report = verify(args.work)
    for act in report["acts"]:
        print(f"{act['citation']} {act['title']}: {act['counts']}")
        for item in act["items"]:
            where = f" at {item['at']}" if item["at"] else ""
            print(f"    {item['provision']:26} {item['target']:22} {item['between']:12} {item['status']}{where}")
    print("total:", report["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
