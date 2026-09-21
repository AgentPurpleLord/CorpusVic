"""Every provision already parsed, re-judged by the recognition rules.

A rule file is edited by hand, and an edit that fixes one Act can quietly
change how another is read. This walks every document in data/parsed/,
finds the printed line each provision opened with, and asks the rules what
type that line is -- against what the parse recorded.

It is a comparison, not a verdict: where the two disagree, either can be
the one that is wrong, and the report prints enough of the line to tell.

    python -m corpus.parsing.check_rules
    python -m corpus.parsing.check_rules crimes-act --show subparagraph
"""
import argparse
import json
from collections import Counter
from dataclasses import dataclass

from corpus import PROJECT_ROOT
from corpus.domain.hierarchy import HIERARCHY_ORDER
from corpus.domain.ruleset import build_registry, nesting_gap
from corpus.parsing.extract import BodyLine
from corpus.parsing.recognise import Context, best

PARSED_DIR = PROJECT_ROOT / "data" / "parsed"
EXTRACTED_DIR = PROJECT_ROOT / "data" / "extracted"


@dataclass(frozen=True)
class Divergence:
    act: str
    page: int
    parsed_as: str
    rules_say: "str | None"
    line: str


@dataclass
class Report:
    checked: int = 0
    agreed: int = 0
    # Provisions whose opening line could not be found in the extracted
    # document -- a node the parse built from something other than one
    # line of body text, e.g. a table cell.
    unmatched: int = 0
    divergences: list = None

    def __post_init__(self):
        if self.divergences is None:
            self.divergences = []

    @property
    def agreement(self) -> float:
        return self.agreed / self.checked if self.checked else 0.0

    def classes(self) -> Counter:
        return Counter((d.parsed_as, d.rules_say) for d in self.divergences)


def _lines_by_position(act: str) -> "tuple[dict, float]":
    """Every printed line of a document, found by where it sits.

    Keyed on page and rounded top edge, which is how a parsed node's
    first rectangle points back at the line that opened it.
    """
    path = EXTRACTED_DIR / f"{act}.json"
    if not path.exists():
        return {}, 12.0
    index, sizes = {}, Counter()
    for page in json.loads(path.read_text(encoding="utf-8")):
        width = page.get("page_width", 0.0)
        for raw in page.get("body_lines", []):
            index[(raw["page_no"], round(raw["y0"]))] = (BodyLine(**raw), width)
            if not raw["bold"] and raw["size"]:
                sizes[round(raw["size"], 1)] += 1
    return index, (sizes.most_common(1)[0][0] if sizes else 12.0)


def _opening_line(index: dict, rect: dict):
    """The line a provision starts on. Rounding the top edge can land a
    point either side of the stored rectangle, so both are tried."""
    top = round(rect.get("y0", 0))
    for y in (top, top + 1, top - 1):
        found = index.get((rect.get("page"), y))
        if found:
            return found
    return None, None


def check(act: str, report: "Report | None" = None) -> Report:
    report = report or Report()
    doc = json.loads((PARSED_DIR / f"{act}.json").read_text(encoding="utf-8"))
    order = doc.get("hierarchy") or HIERARCHY_ORDER
    rank = {level: i for i, level in enumerate(order)}
    index, body_size = _lines_by_position(act)
    registry, gap = build_registry(act), nesting_gap(act)

    # The provisions enclosing the line being judged, rebuilt as the walk
    # goes: a node closes every node at its own level or deeper.
    stack: list[tuple[str, float]] = []
    for node in doc["nodes"]:
        node_type = node["type"]
        rects = node.get("rects") or []
        if node_type not in rank or not rects:
            continue
        line, page_width = _opening_line(index, rects[0])
        if line is None:
            report.unmatched += 1
            continue
        while stack and rank[stack[-1][0]] >= rank[node_type]:
            stack.pop()

        ctx = Context(body_size=body_size, page_width=page_width, nesting_gap=gap,
                      open_types=tuple(t for t, _ in stack),
                      open_x0=tuple(x for _, x in stack))
        verdict = best(line, ctx, registry)
        said = verdict.type_id if verdict else None

        report.checked += 1
        if said == node_type:
            report.agreed += 1
        else:
            report.divergences.append(
                Divergence(act, line.page_no, node_type, said, line.text.strip()))
        stack.append((node_type, line.x0))
    return report


def check_all(acts: "list[str] | None" = None) -> Report:
    acts = acts or sorted(p.stem for p in PARSED_DIR.glob("*.json"))
    report = Report()
    for act in acts:
        check(act, report)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("act", nargs="*", help="documents to check (default: every one in data/parsed/)")
    ap.add_argument("--show", metavar="TYPE", help="print every divergence the parse recorded as TYPE")
    args = ap.parse_args()

    report = check_all(args.act or None)
    if not report.checked:
        raise SystemExit(
            "nothing to check: no provision could be matched to a printed line.\n"
            f"data/extracted/ holds the lines and is not kept in git -- run\n"
            "  python -m corpus.parsing.run_pipeline acts/<act>.pdf\n"
            "to rebuild it.")

    print(f"{report.checked} provisions checked, {report.agreement:.1%} agreement "
          f"({len(report.divergences)} differ, {report.unmatched} not on a line of their own)\n")
    for (parsed_as, rules_say), count in report.classes().most_common():
        print(f"  {count:6d}  parsed as {parsed_as:18s} rules say {rules_say}")
    if args.show:
        print()
        for d in report.divergences:
            if d.parsed_as == args.show:
                print(f"  {d.act} p{d.page:<5} {d.rules_say or '(no type)':16s} {d.line[:70]}")


if __name__ == "__main__":
    main()
