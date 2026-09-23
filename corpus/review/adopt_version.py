"""
Turns a document held under its plain name into version N of a work, so
that another version of it can be added beside it.

    python -m corpus.review.adopt_version evidence-act --dry-run
    python -m corpus.review.adopt_version evidence-act

Everything keyed by the document's name moves to the versioned one:
its PDF into acts/<work>/, its parse, its review files with the `act` on
every row, its Bill links, and the derived files beside them. The
database is exported first and rebuilt from the moved files after, so
nothing reviewed exists only in a database whose rows still carry the
old name.

What does not move is what is keyed by the *work*: publication, and the
public address, which the newest version holds whatever its slug is.

It refuses rather than half-moves: when the database and the review
files disagree, when the target already exists, or when neither the
parse nor the PDF states a version number.
"""
import argparse
import json
from pathlib import Path

from corpus import PROJECT_ROOT
from corpus.parsing.versions import document_slug, read_front_matter, split_document_slug
from corpus.review import review_sync
from corpus.storage import db

# Derived and gitignored, but read by slug: the review tool reads a
# document's diagnostics by its name, so leaving them behind would
# silently drop them.
_DERIVED_DIRS = ("extracted", "diagnostics", "markdown", "akn")


class Refused(Exception):
    pass


def _version_of(base: Path, slug: str, parse: dict, pdf: "Path | None") -> int:
    stated = (parse.get("version") or {}).get("version")
    if stated is None and pdf is not None:
        stated = read_front_matter(pdf).get("version")
    if stated is None:
        raise Refused(f"{slug} states no Authorised Version number, so it has no version to become.")
    return int(stated)


def _source_pdf(base: Path, slug: str, parse: dict) -> "Path | None":
    recorded = parse.get("source")
    if recorded and (base / recorded).is_file():
        return base / recorded
    matches = sorted(p for p in (base / "acts").glob(f"{slug}.*") if p.suffix.lower() == ".pdf")
    return matches[0] if matches else None


def plan(slug: str, base_dir=None) -> dict:
    """What adopting `slug` would move, as {"slug", "new_slug", "moves":
    [(from, to)], "bill_links": [paths]} -- or Refused."""
    base = Path(base_dir or PROJECT_ROOT)
    work, version = split_document_slug(slug)
    if version is not None:
        raise Refused(f"{slug} is already version {version} of {work}.")
    parse_path = base / "data" / "parsed" / f"{slug}.json"
    if not parse_path.is_file():
        raise Refused(f"There is no parse of {slug} to adopt.")
    parse = json.loads(parse_path.read_text(encoding="utf-8"))
    pdf = _source_pdf(base, slug, parse)
    new_slug = document_slug(slug, _version_of(base, slug, parse, pdf))

    moves = [(parse_path, parse_path.with_name(f"{new_slug}.json"))]
    if pdf is not None and pdf.parent == base / "acts":
        moves.append((pdf, base / "acts" / slug / pdf.name))
    review = base / "data" / review_sync.REVIEW_DIRNAME / slug
    if review.is_dir():
        moves.append((review, review.with_name(new_slug)))
    for name in _DERIVED_DIRS:
        folder = base / "data" / name
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            # Exactly this document's: "crimes-act.json" or "crimes-act/",
            # never "crimes-act-50.json", which is another extraction.
            if path.name == slug or path.name.startswith(f"{slug}."):
                moves.append((path, path.with_name(new_slug + path.name[len(slug):])))
    for _old, new in moves:
        if new.exists():
            raise Refused(f"{new} already exists; adopting {slug} would overwrite it.")

    bill_links = []
    links_dir = base / "data" / "bill_links"
    if links_dir.is_dir():
        for path in sorted(links_dir.glob("*.json")):
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("act_slug") == slug:
                    bill_links.append(path)
            except (OSError, ValueError):
                continue
    return {"slug": slug, "new_slug": new_slug, "moves": moves, "bill_links": bill_links}


def _rewrite_rows(folder: Path, old: str, new: str) -> None:
    for path in folder.glob("*.jsonl"):
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("act") == old:
                row["act"] = new
            lines.append(json.dumps(row, sort_keys=True, ensure_ascii=False))
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def adopt(slug: str, base_dir=None) -> dict:
    """Does what plan() describes. Returns the plan."""
    base = Path(base_dir or PROJECT_ROOT)
    steps = plan(slug, base)
    new_slug = steps["new_slug"]
    if db.db_path(base).exists():
        # Anything reviewed but not yet exported would otherwise be left
        # in the database under the old name and then replaced by the
        # import below. export refuses if the files are the newer side.
        review_sync.export(base)

    for old, new in steps["moves"]:
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
    parse_path = base / "data" / "parsed" / f"{new_slug}.json"
    parse = json.loads(parse_path.read_text(encoding="utf-8"))
    for old, new in steps["moves"]:
        if old.suffix.lower() == ".pdf" and parse.get("source"):
            parse["source"] = str(new.relative_to(base))
    if parse.get("act") == slug:
        parse["act"] = new_slug
    parse_path.write_text(json.dumps(parse, indent=2), encoding="utf-8")
    review = base / "data" / review_sync.REVIEW_DIRNAME / new_slug
    if review.is_dir():
        _rewrite_rows(review, slug, new_slug)
    for path in steps["bill_links"]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["act_slug"] = new_slug
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    if (base / "data" / review_sync.REVIEW_DIRNAME).is_dir():
        review_sync.import_(base)
    return steps


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slug")
    ap.add_argument("--dry-run", action="store_true", help="say what would move, and move nothing")
    args = ap.parse_args()
    try:
        steps = plan(args.slug) if args.dry_run else adopt(args.slug)
    except (Refused, review_sync.Unloaded) as e:
        raise SystemExit(f"Refused: {e}")
    base = Path(PROJECT_ROOT)
    for old, new in steps["moves"]:
        print(f"{old.relative_to(base)} -> {new.relative_to(base)}")
    for path in steps["bill_links"]:
        print(f"{path.relative_to(base)}: act_slug -> {steps['new_slug']}")
    print(("Would adopt " if args.dry_run else "Adopted ") + f"{args.slug} as {steps['new_slug']}.")


if __name__ == "__main__":
    main()
