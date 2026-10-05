"""Searching legislation.vic.gov.au for an Act or a Bill to add, and the
PDFs a Bill is published with.

The same content API as corpus/amending/fetch.py and versions.py. Its
records filter by title, so "Bail" finds the Bail Act 1977 and every
Bail Bill in one call each. An Act's PDF is its newest reprint
(versions.available); a Bill's are its introduction print and, beside it,
its Explanatory Memorandum.
"""
import json
import re
import urllib.parse

from corpus.amending.fetch import CONTENT, SITE, _get


def _query(bundle: str, q: str, limit: int, fields: str, get) -> list[dict]:
    params = {
        "site": SITE,
        "filter[title][operator]": "CONTAINS", "filter[title][value]": q,
        "page[limit]": str(limit), f"fields[node--{bundle}]": fields,
    }
    data = json.loads(get(f"{CONTENT}/api/v1/node/{bundle}?" + urllib.parse.urlencode(params)))
    return data.get("data") or []


def _ranked(rows: list[dict], q: str, newest_first: bool = False) -> list[dict]:
    """Titles that begin with what was typed first -- "Bail" means the
    Bail Act before the Footscray (Bailey Reserve) Land Act. Then Acts
    alphabetically, and Bills newest first: a Bill is usually wanted as
    the one behind a recent change."""
    q = q.lower()
    if newest_first:
        return sorted(rows, key=lambda r: (not r["title"].lower().startswith(q), -(r.get("year") or 0), r["title"].lower()))
    return sorted(rows, key=lambda r: (not r["title"].lower().startswith(q), r["title"].lower()))


def search(q: str, limit: int = 15, get=_get) -> dict:
    """{"acts": [{title, act_no, year, path}], "bills": [{id, title, year,
    status, path}]} for titles containing `q`."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"acts": [], "bills": []}
    acts = [{
        "title": a["attributes"]["title"],
        "act_no": a["attributes"].get("field_act_sr_number"),
        "year": _year(a["attributes"].get("field_act_sr_year")),
        "path": (a["attributes"].get("path") or {}).get("alias"),
    } for a in _query("act_in_force", q, limit, "title,field_act_sr_number,field_act_sr_year,path", get)]
    bills = [{
        "id": b["id"],
        "title": b["attributes"]["title"].strip(),
        "year": _year(b["attributes"].get("field_legislation_year")),
        "status": b["attributes"].get("field_legislation_status"),
        "path": (b["attributes"].get("path") or {}).get("alias"),
    } for b in _query("bill", q, limit, "title,field_legislation_year,field_legislation_status,path", get)]
    return {"acts": _ranked(acts, q), "bills": _ranked(bills, q, newest_first=True)}


def _year(value) -> "int | None":
    m = re.match(r"\d{4}", str(value or ""))
    return int(m.group()) if m else None


_BILL_DOCUMENTS = ("field_bill_documents,field_bill_documents.field_bill_document_documents,"
                   "field_bill_documents.field_bill_document_documents.field_media_file")


def bill_documents(bill_id: str, get=_get) -> list[dict]:
    """[{"title", "kind": "bill" | "em", "pdf_url"}] -- the PDFs a Bill is
    published with. Each also comes as a .doc, which the pipeline cannot
    read and is left out."""
    if not re.fullmatch(r"[0-9a-f-]{36}", bill_id or ""):
        raise ValueError(f"Not a Bill's id: {bill_id!r}")
    data = json.loads(get(f"{CONTENT}/api/v1/node/bill/{bill_id}?"
                          + urllib.parse.urlencode({"site": SITE, "include": _BILL_DOCUMENTS})))
    by_id = {x["id"]: x for x in data.get("included") or []}
    out = []
    for doc in data.get("included") or []:
        if doc["type"] != "paragraph--bill_document":
            continue
        title = doc["attributes"].get("field_bill_document_title") or ""
        refs = (doc["relationships"].get("field_bill_document_documents") or {}).get("data") or []
        for ref in refs if isinstance(refs, list) else [refs]:
            media = by_id.get(ref["id"])
            if not media or media["type"] != "media--pdf":
                continue
            file = by_id.get(((media["relationships"].get("field_media_file") or {}).get("data") or {}).get("id"))
            if not file:
                continue
            attrs = file["attributes"]
            url = attrs.get("url") or urllib.parse.urljoin(CONTENT, (attrs.get("uri") or {}).get("url", ""))
            out.append({"title": title, "kind": "em" if "explanatory memorandum" in title.lower() else "bill",
                        "pdf_url": url})
    return out
