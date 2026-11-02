"""Fetches a Bill's second reading speeches and Statements of
Compatibility from Hansard, kept as their paragraphs.

    python -m corpus.hansard.fetch criminal-procedure-act
    python -m corpus.hansard.fetch criminal-procedure-bill-2008
    python -m corpus.hansard.fetch road-safety-act --title "Road Safety Act 1986"

Writes data/speeches/<bill-slug>.json, committed like data/bill_links/:
the public site reads it, and Hansard is not where a reader should have
to go to find out what an Act is for. A record that can't be fetched is
still written, with its Hansard link and the reason, so the site can
link out to it.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from corpus import PROJECT_ROOT
from corpus.domain import commentary
from corpus.hansard import bill_page, speech
from corpus.parsing.versions import split_document_slug

SPEECHES_DIR = Path("data") / "speeches"
_BILLS_YAML = Path("data") / "bill_links" / "bills.yaml"


def _bills(base: Path) -> dict:
    try:
        return yaml.safe_load((base / _BILLS_YAML).read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def bill_for(slug: str, base: Path = PROJECT_ROOT, title: "str | None" = None, get=None) -> "tuple[str, dict] | None":
    """(bill slug, {"title", "url"}) for a Bill slug, or for an Act's work
    slug through the Bill it was enacted from. With `title` ("Road Safety
    Act 1986"), an Act no Bill is recorded for is looked for on the site
    -- as "... Bill <year>", or the year before, when most Bills are
    introduced -- and recorded in bills.yaml when found."""
    bills = _bills(base)
    if slug in bills:
        return slug, bills[slug]
    work = split_document_slug(slug)[0]
    bill_docs, _em = commentary.load_link_docs(base)
    for doc in bill_docs:
        if doc.get("act_slug") and split_document_slug(doc["act_slug"])[0] == work and doc.get("bill_slug") in bills:
            return doc["bill_slug"], bills[doc["bill_slug"]]
    if not title:
        return None
    m = re.match(r"^(.*?)\s+Act\s+(\d{4})$", title.strip())
    if not m:
        return None
    name, year = m.group(1), int(m.group(2))
    for y in (year, year - 1):
        bill_title = f"{name} Bill {y}"
        path = "/bills/" + re.sub(r"[^a-z0-9]+", "-", bill_title.lower()).strip("-")
        try:
            bill_page.bill_node(path, **({"get": get} if get else {}))
        except bill_page.NotFound:
            continue
        bill_slug = path.rsplit("/", 1)[1]
        entry = {"title": bill_title, "url": f"https://www.legislation.vic.gov.au{path}"}
        bills[bill_slug] = entry
        _record_bill(base, bill_slug, entry)
        return bill_slug, entry
    return None


def _record_bill(base: Path, bill_slug: str, entry: dict) -> None:
    """Appended, so the hand-written comment at the head of bills.yaml stays."""
    path = base / _BILLS_YAML
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{bill_slug}:\n  title: {entry['title']}\n  url: {entry['url']}\n")


def fetch_bill(bill_slug: str, entry: dict, base: Path = PROJECT_ROOT, get_api=None, get_page=None,
               act_work: "str | None" = None) -> dict:
    """Reads the Bill's page, fetches each record it links to, and writes
    data/speeches/<bill_slug>.json. Returns what was written."""
    path = "/" + entry["url"].split("legislation.vic.gov.au/", 1)[1].strip("/")
    node = bill_page.bill_node(path, **({"get": get_api} if get_api else {}))
    records = []
    for reading in bill_page.readings(node):
        record = {k: v for k, v in reading.items() if k != "url"}
        record["source_url"] = reading["url"]
        previous = next((r for r in (load(bill_slug, base) or {}).get("speeches", [])
                         if (r.get("kind"), r.get("house")) == (record["kind"], record["house"]) and r.get("html")), None)
        try:
            record["html"] = speech.fetch(record["source_url"], get=get_page)
            if not speech.paragraph_count(record["html"]):
                record["error"] = "no paragraphs found on the Hansard page"
        except Exception as e:   # one unreachable record shouldn't lose the rest
            record["html"], record["error"] = "", f"{type(e).__name__}: {e}"
        if record.get("error") and previous:
            # Fetching again shouldn't lose what is held: a firewall's
            # refusal is not the speech being gone.
            record = {**previous, "source_url": record["source_url"]}
        records.append(record)
    # The Act it became, for a Bill no link record relates to one (found
    # by title, see bill_for): the site looks speeches up by Act.
    doc = {"bill_slug": bill_slug, "bill_title": entry.get("title"), "bill_page": entry["url"], "act_work": act_work,
           "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "speeches": records}
    out = base / SPEECHES_DIR / f"{bill_slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


def load(bill_slug: str, base: Path = PROJECT_ROOT) -> "dict | None":
    try:
        return json.loads((base / SPEECHES_DIR / f"{bill_slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def from_saved_page(bill_slug: str, index: int, page: str, base: Path = PROJECT_ROOT) -> dict:
    """Record `index` of a Bill's speeches, read from a Hansard page saved
    in a browser. Hansard's firewall refuses requests that don't come
    from a person's browser, so this is the way in that always works."""
    doc = load(bill_slug, base)
    if doc is None or not (0 <= index < len(doc.get("speeches") or [])):
        raise ValueError(f"no record {index} is held for {bill_slug}")
    try:
        kept = speech.main_text(page)
    except speech.Refused as e:
        raise ValueError("that is Hansard's firewall page, not the record -- open the link in a browser and save it again") from e
    if not speech.paragraph_count(kept):
        raise ValueError("no paragraphs were found in that page")
    record = doc["speeches"][index]
    record["html"] = kept
    record.pop("error", None)
    record["from_saved_page"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (base / SPEECHES_DIR / f"{bill_slug}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                                                          encoding="utf-8")
    return record


def held(base: Path = PROJECT_ROOT) -> list[dict]:
    """Every speeches document held, each {bill_slug, act_work, ...}."""
    out = []
    for path in sorted((base / SPEECHES_DIR).glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("slug", help="an Act's work slug, or a Bill's slug from data/bill_links/bills.yaml")
    ap.add_argument("--title", help='the Act\'s title, to look for a Bill not yet recorded ("Road Safety Act 1986")')
    args = ap.parse_args(argv)
    found = bill_for(args.slug, title=args.title)
    if found is None:
        print(f"No Bill is recorded for {args.slug}." + ("" if args.title else " Give --title to look for one."),
              file=sys.stderr)
        return 1
    bill_slug, entry = found
    doc = fetch_bill(bill_slug, entry, act_work=None if args.slug == bill_slug else split_document_slug(args.slug)[0])
    for r in doc["speeches"]:
        what = "error: " + r["error"] if r.get("error") else f"{speech.paragraph_count(r['html'])} paragraphs"
        print(f"{r['house']:22} {r['kind']:28} {r.get('date') or '':10}  {what}")
    print(f"wrote {SPEECHES_DIR / (bill_slug + '.json')}")
    return 0 if any(not r.get("error") for r in doc["speeches"]) else 1


if __name__ == "__main__":
    sys.exit(main())
