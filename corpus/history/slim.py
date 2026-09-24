"""Keeping a work's versions slim (corpus/history/delta.py).

One version -- the base, the one reviewed in full -- is held whole. Each
other keeps the pieces changed in its step toward the base (their words)
and in its step away from it (where they print), its notes, and those
pages of its PDF. Run after a version is added, and once to convert the
versions already held:

    python -m corpus.history.slim criminal-procedure-act [--base 114] [--dry-run]

A version added between two held ones only ever shrinks what its
neighbours keep: whatever changed between it and a neighbour is also a
change between that neighbour and the next one out. So nothing here
needs a PDF again, except a slim version asked for a piece it never
kept -- which is reported, to be fetched again.
"""
import argparse
import json
import sys
from pathlib import Path

from corpus import PROJECT_ROOT
from corpus.history import delta
from corpus.parsing.identity import annotate_ids
from corpus.parsing.versions import split_document_slug
from corpus.storage import db


def _base_dir(base_dir) -> Path:
    return Path(base_dir or PROJECT_ROOT)


def versions(work: str, base_dir=None) -> dict[int, str]:
    out = {}
    for path in (_base_dir(base_dir) / "data" / "parsed").glob(f"{work}-v*.json"):
        w, v = split_document_slug(path.stem)
        if w == work and v is not None:
            out[v] = path.stem
    return dict(sorted(out.items()))


def _base_path(work: str, base_dir=None) -> Path:
    return _base_dir(base_dir) / "data" / "versions" / f"{work}.json"


def base_version(work: str, base_dir=None) -> "int | None":
    """The version held whole: the one set for the work, else the one with
    the most review done (the newest, on a tie)."""
    held = versions(work, base_dir)
    try:
        chosen = json.loads(_base_path(work, base_dir).read_text(encoding="utf-8")).get("base")
        if chosen in held:
            return chosen
    except (OSError, ValueError):
        pass
    if not held:
        return None
    return max(held, key=lambda v: (len(db.load_verified(held[v], base_dir)), v))


def set_base(work: str, version: int, base_dir=None) -> None:
    path = _base_path(work, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base": version}) + "\n", encoding="utf-8")


def _read(slug: str, base_dir=None) -> dict:
    data = json.loads((_base_dir(base_dir) / "data" / "parsed" / f"{slug}.json").read_text(encoding="utf-8"))
    if "nodes" in data:
        annotate_ids(data["nodes"], data.get("hierarchy") or None)
    return data


def _index(parse: dict) -> dict:
    return parse["slim"]["notes"] if "slim" in parse else delta.notes_index(parse["nodes"], parse.get("unattached_notes"))


def plan(work: str, base_dir=None, base: "int | None" = None) -> dict:
    """{version: {"toward", "text", "pages_only"}} for every version but
    the base."""
    held = versions(work, base_dir)
    base = base if base in held else base_version(work, base_dir)
    if base is None:
        return {}
    order = list(held)
    idx = {v: _index(_read(held[v], base_dir)) for v in order}
    out = {}
    for k, v in enumerate(order):
        if v == base:
            continue
        before = order[k - 1] if k else None
        after = order[k + 1] if k + 1 < len(order) else None
        if v < base:   # built backward from the version after it
            toward = after
            text = delta.changed_pieces(idx[v], idx[after])
            pages = delta.changed_pieces(idx[before], idx[v]) if before is not None else {}
        else:          # built forward from the version before it
            toward = before
            text = delta.changed_pieces(idx[before], idx[v])
            pages = delta.changed_pieces(idx[v], idx[after]) if after is not None else {}
        out[v] = {"toward": held[toward], "text": sorted(text), "pages_only": sorted(set(pages) - set(text))}
    return out


def _reslim(parse: dict, toward: str, text: list, pages_only: list) -> tuple[dict, list]:
    """A slim version cut down further, from what it kept. Names it never
    kept come back as missing -- that version must be fetched again."""
    slim = parse["slim"]
    pieces = {p["name"]: p for p in slim["pieces"]}
    wanted = [*text, *pages_only]
    missing = [n for n in wanted
               if not any(n == m or n.startswith(m + "/") for m in pieces) and n not in slim.get("removed", [])]
    kept = [{**p, "words": any(p["name"] == t or p["name"].startswith(t + "/") or t.startswith(p["name"] + "/")
                               for t in text)}
            for p in slim["pieces"]
            if any(p["name"] == n or p["name"].startswith(n + "/") or n.startswith(p["name"] + "/") for n in wanted)]
    pages = sorted({r["page"] for p in kept for n in p["nodes"] for r in n.get("rects") or []}
                   | {note.get("page") for note in parse.get("unattached_notes") or [] if note.get("page")})
    return ({**parse, "slim": {**slim, "toward": toward, "pieces": kept,
                               "removed": [t for t in text if t in slim.get("removed", [])], "kept_pages": pages}},
            missing)


def _own_rows(slug: str, text: list, base_dir=None) -> int:
    """Drops this version's review rows for anything not its own words --
    those are the neighbour's now, and its review is lent by name."""
    rows = db.load_verified(slug, base_dir)
    keep = [r for r in rows if any((r.get("_node_id") or "") == t or (r.get("_node_id") or "").startswith(t + "/")
                                   for t in text)]
    if len(keep) != len(rows):
        db.save_verified(slug, keep, base_dir)
    return len(rows) - len(keep)


def apply(work: str, base_dir=None, pdf_for=None, dry_run: bool = False, base: "int | None" = None) -> dict:
    """Slims every version but the base. `pdf_for(slug)` is where a
    version's PDF is, to blank its other pages; None leaves PDFs alone."""
    from corpus.storage import parsed

    held = versions(work, base_dir)
    base = base if base in held else base_version(work, base_dir)
    if base is not None and not dry_run and not _base_path(work, base_dir).exists():
        # Remembered, so adding a version can never move it: a version
        # slimmed toward one base cannot be read from another without
        # fetching it again.
        set_base(work, base, base_dir)
    report = {"work": work, "base": base, "versions": {}}
    for v, step in plan(work, base_dir, base).items():
        slug = held[v]
        path = _base_dir(base_dir) / "data" / "parsed" / f"{slug}.json"
        parse = _read(slug, base_dir)
        before = path.stat().st_size
        if "slim" in parse:
            slimmed, missing = _reslim(parse, step["toward"], step["text"], step["pages_only"])
        else:
            slimmed, missing = delta.slim(parse, step["toward"], step["text"], step["pages_only"]), []
        entry = {"text": len(step["text"]), "pages_only": len(step["pages_only"]), "missing": missing,
                 "pages": len(slimmed["slim"]["kept_pages"]), "bytes_before": before}
        if not dry_run:
            path.write_text(json.dumps(slimmed, ensure_ascii=False), encoding="utf-8")
            entry["bytes_after"] = path.stat().st_size
            entry["rows_dropped"] = _own_rows(slug, step["text"], base_dir)
            # Its rows are read by name against the text as put back
            # together, so that is the parse they belong to now.
            db.save_parse_fingerprint(slug, parsed.load(path)["fingerprint"], base_dir)
            pdf = pdf_for(slug) if pdf_for else None
            if pdf is not None and Path(pdf).exists():
                tmp = Path(pdf).with_suffix(".slim.pdf")
                size = Path(pdf).stat().st_size
                delta.blank_pages(Path(pdf), slimmed["slim"]["kept_pages"], tmp)
                tmp.replace(pdf)
                entry["pdf_bytes"] = (size, Path(pdf).stat().st_size)
        report["versions"][v] = entry
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("--base", type=int, help="the version held whole (remembered for the work)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reparse", action="store_true",
                    help="parse each version not yet slim again from its PDF first, with the current parser -- "
                         "an older parse attaches notes and names pieces differently from the base's")
    args = ap.parse_args()
    if args.base is not None and not args.dry_run:
        set_base(args.work, args.base)
    from corpus.web import dashboard

    def pdf_for(slug):
        return dashboard._find_source_pdf(slug)

    if args.reparse and not args.dry_run:
        base = args.base or base_version(args.work)
        for v, slug in versions(args.work).items():
            pdf = pdf_for(slug)
            if v == base or pdf is None or "slim" in _read(slug):
                continue
            profile = dashboard._parse_field(slug, "profile") or ""
            ok, _code, log = dashboard._run_parse_subprocess(
                dashboard._build_parse_command(pdf, "act", profile, "", "", keep_accepted=True))
            print(f"v{v}: {'parsed again' if ok else 'the parse failed'}", file=sys.stderr)

    print(json.dumps(apply(args.work, pdf_for=pdf_for, dry_run=args.dry_run, base=args.base), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
