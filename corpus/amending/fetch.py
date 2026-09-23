"""Downloads the amending Acts a work's held versions need (see scope.py)
from legislation.vic.gov.au.

Through the site's own content API rather than its pages: the pages are
rendered in the browser from that API, so scraping them would mean
scraping what the API already says plainly. Three calls per Act:

  1. /api/v1/route?site=6&path=/as-made/acts/<slug> -- the Act's record,
     from the address its title gives it;
  2. that record, with its authorised PDF included;
  3. the PDF itself.

The record's own Act number and year are checked against the citation
wanted, so a title that happens to lead somewhere else is refused rather
than read as the wrong Act.

    python -m corpus.amending.fetch criminal-procedure-act-v114
"""
import argparse
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from corpus import PROJECT_ROOT
from corpus.amending.instructions import read_act
from corpus.amending.pdf import read_pdf
from corpus.amending.scope import acts_between

CONTENT = "https://content.legislation.vic.gov.au"
SITE = "6"   # legislation.vic.gov.au, in the content API's own list of sites
_AGENT = "CorpusVic amending-Act fetcher (+https://corpusvic.au)"


def slug(title: str) -> str:
    """The as-made address the site gives an Act: its title, lower-case,
    punctuation dropped, words hyphenated."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


class NotFound(Exception):
    pass


def locate(act: dict, get=_get) -> dict:
    """{"page", "pdf_url"} for one Act from acts_between."""
    path = f"/as-made/acts/{slug(act['title'])}"
    try:
        route = json.loads(get(f"{CONTENT}/api/v1/route?" + urllib.parse.urlencode({"site": SITE, "path": path})))
    except Exception as e:
        raise NotFound(f"no as-made page at {path} ({e})") from e
    endpoint = route["data"]["attributes"]["endpoint"]
    node = json.loads(get(f"{endpoint}?" + urllib.parse.urlencode(
        {"site": SITE, "include": "field_as_made_authorized_version.field_media_file"})))
    attrs = node["data"]["attributes"]
    record = act["record"]
    if (str(attrs.get("field_act_sr_number")), str(attrs.get("field_act_sr_year"))) != (
            str(record.get("act_no")), str(record.get("year"))):
        raise NotFound(f"{path} is No. {attrs.get('field_act_sr_number')}/{attrs.get('field_act_sr_year')}, "
                       f"not {act['citation']}")
    files = [x for x in node.get("included", []) if x["type"] == "file--file"]
    if not files:
        raise NotFound(f"{path} has no authorised PDF")
    file = files[0]["attributes"]
    url = file.get("url") or urllib.parse.urljoin(CONTENT, file["uri"]["url"])
    return {"page": f"https://www.legislation.vic.gov.au{path}", "pdf_url": url}


# Under acts/, where the Act PDFs are, but not one of them: the dashboard
# lists every other folder there as a work (see dashboard.discover_slugs).
FOLDER = "amending"


def pdf_path(citation: str, base_dir=None):
    act_no, year = citation.split("/")
    return (base_dir or PROJECT_ROOT) / "acts" / FOLDER / f"{year}-{act_no}.pdf"


def instructions_path(citation: str, base_dir=None):
    act_no, year = citation.split("/")
    return (base_dir or PROJECT_ROOT) / "data" / "amending" / f"{year}-{act_no}.json"


def write_instructions(citation: str, base_dir=None) -> list[dict]:
    """Reads a fetched Act's instructions and keeps them beside the
    manifest -- small, and committed, so a server that has the review data
    has these without fetching the PDFs again."""
    out = [{"act": citation, **i} for i in read_act(read_pdf(pdf_path(citation, base_dir)))]
    path = instructions_path(citation, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One instruction a line, so a re-read that changes one shows as one.
    path.write_text("[\n" + ",\n".join(json.dumps(i, ensure_ascii=False) for i in out) + "\n]\n", encoding="utf-8")
    return out


def manifest_path(base_dir=None):
    return (base_dir or PROJECT_ROOT) / "data" / "amending" / "manifest.json"


def fetch(work_slug: str, base_dir=None, get=_get, log=print) -> dict:
    """Fetches what is missing, one Act at a time, and returns the
    manifest. One Act failing is reported and the rest carry on."""
    base = base_dir or PROJECT_ROOT
    manifest_file = manifest_path(base)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    for act in acts_between(work_slug, base):
        citation = act["citation"]
        dest = pdf_path(citation, base)
        known = manifest.get(citation)
        if known and dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == known.get("sha256"):
            read = write_instructions(citation, base)
            log(f"{citation}: already have it; {len(read)} instructions read")
            continue
        try:
            where = locate(act, get)
            pdf = get(where["pdf_url"])
        except Exception as e:
            log(f"{citation}: NOT FETCHED -- {e}")
            continue
        if not pdf.startswith(b"%PDF"):
            log(f"{citation}: NOT FETCHED -- {where['pdf_url']} is not a PDF")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdf)
        manifest[citation] = {
            "citation": citation, "title": act["title"], **where,
            "sha256": hashlib.sha256(pdf).hexdigest(),
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        read = write_instructions(citation, base)
        log(f"{citation}: fetched {act['title']} ({len(pdf) // 1024} KB); {len(read)} instructions read, "
            f"{sum(i['action'] == 'unparsed' for i in read)} not recognised")
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work", help="any version's slug, e.g. criminal-procedure-act-v114")
    args = ap.parse_args()
    fetch(args.work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
