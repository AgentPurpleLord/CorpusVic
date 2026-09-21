"""
Shows what a profile actually resolves to, and lets you check a pattern
against a single line of text without re-running the whole pipeline.

Usage:
    python show_profile.py crimes-act
        Lists every pattern key, its resolved regex, and whether it's the
        default or overridden by corpus/domain/rules/crimes-act.yaml.

    python show_profile.py criminal-procedure-act --test "Part 5.1—Introduction"
        Shows which pattern key(s) match that exact line and what they
        capture as (number, heading/rest) -- the fast edit-test loop for
        writing a profile override, see corpus/domain/rules/TEMPLATE.yaml.

    python show_profile.py criminal-procedure-act --explain "(c) the accused"
        Weighs that line against every recognition rule and prints each
        condition and whether it passed -- the rule that fired, and what
        stopped the others. The line's real typography is read out of
        data/extracted/<act>.json where the line is found there, so this
        shows what the parser actually saw, not a guess at it.
"""
import argparse
import json
from collections import Counter

from corpus import PROJECT_ROOT
from corpus.domain.hierarchy import HIERARCHY_ORDER
from corpus.domain.profiles import ProfileError, describe_profile, load_hierarchy, load_profile
from corpus.domain.ruleset import RulesetError, build_registry, nesting_gap
from corpus.parsing.extract import BodyLine
from corpus.parsing.recognise import Context, recognise


def _extracted_lines(act: str) -> list[BodyLine]:
    """The document's own lines, with the size, weight and position the
    extractor measured off the page."""
    extracted = PROJECT_ROOT / "data" / "extracted"
    path = extracted / f"{act}.json"
    if not path.exists():
        # An Act that has been re-parsed per reprint is stored as
        # <act>-v<N>.json, with no plain <act>.json. The latest reprint
        # is the one a rule is being written against.
        versions = sorted(extracted.glob(f"{act}-v*.json"))
        if not versions:
            return []
        path = versions[-1]
    return [
        BodyLine(**line)
        for page in json.loads(path.read_text(encoding="utf-8"))
        for line in page.get("body_lines", [])
    ]


def _body_size(lines: list[BodyLine]) -> float:
    """The most common non-bold size: this document's ordinary text, which
    every size rule is a ratio of."""
    sizes = Counter(round(l.size, 1) for l in lines if not l.bold and l.size)
    return sizes.most_common(1)[0][0] if sizes else 12.0


def _find(lines: list[BodyLine], text: str) -> "BodyLine | None":
    wanted = text.strip()
    return next((l for l in lines if l.text.strip() == wanted),
                next((l for l in lines if wanted in l.text), None))


def explain(act: str, text: str, open_above: list[str]) -> None:
    lines = _extracted_lines(act)
    body_size = _body_size(lines)
    found = _find(lines, text)
    if found is None:
        # Nothing measured to fall back on, so say so rather than
        # printing conditions judged against invented typography.
        print(f"  (line not found in data/extracted/{act}.json -- "
              f"judging it as plain body text at the left margin)\n")
        found = BodyLine(text=text, x0=71.0, x1=400.0, y0=0.0, y1=0.0, page_no=0, size=body_size)

    stack = [spec.rsplit(":", 1) for spec in open_above]
    ctx = Context(
        body_size=body_size,
        nesting_gap=nesting_gap(act),
        open_types=tuple(type_id for type_id, _ in stack),
        open_x0=tuple(float(x0) for _, x0 in stack),
    )

    print(f"  {found.text.strip()!r}")
    print(f"    page {found.page_no}  x0={found.x0:.1f}  size={found.size} (body {body_size})"
          f"  bold={'yes' if found.bold else 'no'}"
          f"  bold-italic={found.leading_bold_italic!r}")
    for type_id, x0 in zip(ctx.open_types, ctx.open_x0):
        print(f"    open above:  {type_id} at x0={x0:.1f}")
    enclosing, enclosing_x0 = ctx.enclosing(found.x0)
    if enclosing:
        print(f"    sits inside: {enclosing} at x0={enclosing_x0:.1f}")
    print()

    for verdict in recognise(found, ctx, build_registry(act)):
        if verdict.matched:
            print(f"  {verdict.type_id:18s} MATCH   number={verdict.number!r} heading={verdict.heading!r}")
            for cond in verdict.conditions:
                print(f"  {'':18s}   {cond.name:20s} ok    {cond.detail}")
        else:
            stopper = verdict.failed
            print(f"  {verdict.type_id:18s} no      {stopper.name}: {stopper.detail}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile", help="profile name, e.g. crimes-act (matches corpus/domain/rules/<name>.yaml)")
    ap.add_argument("--test", metavar="LINE", help="check LINE against every pattern and show matches/captures")
    ap.add_argument("--explain", metavar="LINE",
                    help="weigh LINE against every recognition rule, condition by condition")
    ap.add_argument("--open", metavar="TYPE:X0", action="append", default=[],
                    help="with --explain: a provision open above the line and where it starts, "
                         "e.g. --open section:163.7 --open paragraph:216.2 (outermost first)")
    args = ap.parse_args()

    # Recognition rules stand on their own base, so --explain works for
    # an Act that has never needed a pattern profile of its own.
    try:
        rows = describe_profile(args.profile)
        hierarchy = load_hierarchy(args.profile)
    except ProfileError as e:
        if not args.explain:
            raise SystemExit(f"error: {e}")
        rows, hierarchy = None, None

    if rows is None:
        print(f"Profile: {args.profile} (none of its own -- defaults throughout)")
    else:
        print(f"Profile: {args.profile}\n")
        tag = "override" if hierarchy != HIERARCHY_ORDER else "default "
        print(f"  [{tag}] {'hierarchy':14s} {' > '.join(hierarchy)}\n")
        for key, pattern, is_override in rows:
            tag = "override" if is_override else "default "
            print(f"  [{tag}] {key:14s} {pattern}")

    if args.test:
        print(f"\nTesting: {args.test!r}\n")
        patterns = load_profile(args.profile)
        matched_any = False
        for key, compiled in patterns.items():
            m = compiled.match(args.test)
            if m:
                matched_any = True
                groups = [g for g in m.groups() if g is not None]
                print(f"  MATCH  {key:14s} groups={groups}")
        if not matched_any:
            print("  (no pattern matched this line)")

    if args.explain:
        print(f"\nExplaining: {args.explain!r}\n")
        try:
            explain(args.profile, args.explain, args.open)
        except RulesetError as e:
            raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()
