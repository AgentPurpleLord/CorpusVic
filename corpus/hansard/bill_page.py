"""Which Hansard records a Bill's page on legislation.vic.gov.au points to.

A Bill's page lists, for each House, its first and second readings. The
second reading links to the speech in which the Minister says what the
Bill is for. A Victorian first reading is formal and has no speech: the
document linked from it is the Statement of Compatibility under the
Charter of Human Rights, tabled with the Bill.

Read through the same content API as corpus/amending/fetch.py, not by
scraping the page, which is a client-side application with nothing in
its HTML.
"""
import html
import json
import re
import urllib.parse
from datetime import date

from corpus.amending.fetch import CONTENT, SITE, _get

_HOUSES = {"field_bill_assembly_section": "Legislative Assembly",
           "field_bill_council_section": "Legislative Council"}
_HREF_RE = re.compile(r'href="([^"]+)"')
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}


class NotFound(Exception):
    pass


def bill_node(path: str, get=_get) -> dict:
    """The content API's node for the Bill at `path` ("/bills/...")."""
    try:
        route = json.loads(get(f"{CONTENT}/api/v1/route?" + urllib.parse.urlencode({"site": SITE, "path": path})))
    except Exception as e:
        raise NotFound(f"no Bill page at {path} ({e})") from e
    return json.loads(get(f"{route['data']['attributes']['endpoint']}?" + urllib.parse.urlencode(
        {"site": SITE, "include": ",".join(_HOUSES)})))


def _links(field) -> list[str]:
    values = field if isinstance(field, list) else [field] if field else []
    out = []
    for value in values:
        for href in _HREF_RE.findall((value or {}).get("value") or ""):
            href = html.unescape(href)
            if href not in out:
                out.append(href)
    return out


def _date(url: str, fallback: "str | None") -> "str | None":
    """The sitting day, as Hansard records it. The old links carry it
    (date1/date2/date3); the field's own date can be another day -- a
    Statement of Compatibility's is the first reading's, while the
    statement is tabled with the second."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    try:
        return date(int(q["date3"][0]), _MONTHS[q["date2"][0].lower()], int(q["date1"][0])).isoformat()
    except (KeyError, ValueError, IndexError):
        return (fallback or "")[:10] or None


_SEARCH = "https://hansard.parliament.vic.gov.au/search/"
_ACTIVITY_PHRASE = {"Second Reading": "be now read a second time"}


def current_link(url: str) -> str:
    """The link as Hansard answers it now. Bills before about 2013 link to
    tex.parliament.vic.gov.au, which no longer exists; the same record is
    found on hansard.parliament.vic.gov.au by the search the recent Bill
    pages link to, built from what the old link names -- title, House,
    activity and sitting day."""
    parts = urllib.parse.urlparse(url)
    if parts.hostname != "tex.parliament.vic.gov.au":
        return url
    q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
    activity = q.get("activity", "")
    search = {"LDMS": "Y", "IW_DATABASE": "*"}
    if activity in _ACTIVITY_PHRASE:
        search["IW_FIELD_ADVANCE_PHRASE"] = _ACTIVITY_PHRASE[activity]
    search.update({"IW_FIELD_IN_SpeechTitle": q.get("title", "").title(),
                   "IW_FIELD_IN_HOUSENAME": q.get("house", ""),
                   "IW_FIELD_IN_ACTIVITYTYPE": activity.capitalize(),
                   "IW_FIELD_IN_SittingYear": q.get("date3", ""),
                   "IW_FIELD_IN_SittingMonth": q.get("date2", ""),
                   "IW_FIELD_IN_SittingDay": q.get("date1", "")})
    return _SEARCH + "?" + urllib.parse.urlencode({k: v for k, v in search.items() if v})


def readings(node: dict) -> list[dict]:
    """[{kind, house, date, member, url}] for each Statement of
    Compatibility and second reading speech the page links to, in the
    order the Houses took them."""
    by_id = {x["id"]: x for x in node.get("included", [])}
    rel = node["data"].get("relationships") or {}
    out = []
    for field, house in _HOUSES.items():
        member = None
        for ref in (rel.get(field) or {}).get("data") or []:
            paragraph = by_id.get(ref["id"])
            if paragraph is None:
                continue
            attrs = paragraph["attributes"]
            if paragraph["type"] == "paragraph--first_reading":
                member = ((attrs.get("field_first_reading_introduced") or {}).get("string_value") or "").strip() or None
                outcome = attrs.get("field_first_reading_outcome") or {}
                for url in _links(outcome):
                    out.append({"kind": "statement_of_compatibility", "house": house, "member": member,
                                "date": _date(url, outcome.get("date_value")), "url": current_link(url)})
            elif paragraph["type"] == "paragraph--second_reading":
                for moved in attrs.get("field_second_reading_moved") or []:
                    for url in _links(moved):
                        out.append({"kind": "second_reading", "house": house, "member": member,
                                    "date": _date(url, moved.get("date_value")), "url": current_link(url)})
    return out
