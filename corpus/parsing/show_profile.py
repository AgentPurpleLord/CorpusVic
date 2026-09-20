"""
Shows what a profile actually resolves to, and lets you check a pattern
against a single line of text without re-running the whole pipeline.

Usage:
    python show_profile.py crimes-act
        Lists every pattern key, its resolved regex, and whether it's the
        default or overridden by corpus/profiles/crimes-act.yaml.

    python show_profile.py criminal-procedure-act --test "Part 5.1—Introduction"
        Shows which pattern key(s) match that exact line and what they
        capture as (number, heading/rest) -- the fast edit-test loop for
        writing a profile override, see corpus/profiles/TEMPLATE.yaml.
"""
import argparse

from corpus.domain.hierarchy import HIERARCHY_ORDER
from corpus.profiles import ProfileError, describe_profile, load_hierarchy, load_profile


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile", help="profile name, e.g. crimes-act (matches corpus/profiles/<name>.yaml)")
    ap.add_argument("--test", metavar="LINE", help="check LINE against every pattern and show matches/captures")
    args = ap.parse_args()

    try:
        rows = describe_profile(args.profile)
        hierarchy = load_hierarchy(args.profile)
    except ProfileError as e:
        raise SystemExit(f"error: {e}")

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


if __name__ == "__main__":
    main()
