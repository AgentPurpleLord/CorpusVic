"""Every Authorised Version of an Act that legislation.vic.gov.au holds,
from the same content API the amending Acts come from (see
corpus/amending/fetch.py): the Act's in-force record lists each version
with its PDF.

A version's PDF is its authorised one where the site has it; the
earliest reprints have only a plain PDF, which reads the same.
"""
import json
import re
import urllib.parse

from corpus.amending.fetch import CONTENT, SITE, NotFound, _get, slug

_INCLUDE = ("field_in_force_version.field_in_force_authorized_ver.field_media_file,"
            "field_in_force_version.field_in_force_version.field_media_file")


def _pdf_url(paragraph: dict, by_id: dict) -> "str | None":
    for field in ("field_in_force_authorized_ver", "field_in_force_version"):
        refs = paragraph["relationships"].get(field, {}).get("data") or []
        for ref in refs if isinstance(refs, list) else [refs]:
            media = by_id.get(ref["id"])
            if not media or media["type"] != "media--pdf":
                continue
            file_ref = media["relationships"].get("field_media_file", {}).get("data")
            file = file_ref and by_id.get(file_ref["id"])
            if file:
                attrs = file["attributes"]
                return attrs.get("url") or urllib.parse.urljoin(CONTENT, attrs["uri"]["url"])
    return None


def available(title: str, act_no, year, get=_get) -> list[dict]:
    """[{"version", "effective", "pages", "pdf_url"}], oldest first.

    `act_no` and `year` are checked against the record, as fetch.locate
    does, so a title that leads to another Act is refused."""
    path = f"/in-force/acts/{slug(title)}"
    try:
        route = json.loads(get(f"{CONTENT}/api/v1/route?" + urllib.parse.urlencode({"site": SITE, "path": path})))
    except Exception as e:
        raise NotFound(f"no in-force page at {path} ({e})") from e
    node = json.loads(get(f"{route['data']['attributes']['endpoint']}?"
                          + urllib.parse.urlencode({"site": SITE, "include": _INCLUDE})))
    attrs = node["data"]["attributes"]
    if act_no and (str(attrs.get("field_act_sr_number")), str(attrs.get("field_act_sr_year"))) != (str(act_no), str(year)):
        raise NotFound(f"{path} is No. {attrs.get('field_act_sr_number')}/{attrs.get('field_act_sr_year')}, "
                       f"not {act_no}/{year}")
    by_id = {x["id"]: x for x in node.get("included", [])}
    out = []
    for x in node.get("included", []):
        if x["type"] != "paragraph--in_force_act_version":
            continue
        a = x["attributes"]
        number = str(a.get("field_in_force_version_number") or "")
        if not number[:1].isdigit():
            continue
        if not number.isdigit():
            # "059A": a reprint between two numbered ones. Versions here
            # are named by number alone, so it is listed but not offered.
            out.append({"version": number, "effective": a.get("field_in_force_effective_date"),
                        "pages": a.get("field_in_force_pages"), "pdf_url": None, "note": "lettered reprint"})
            continue
        version = int(number)
        # A reprint in volumes is several PDFs, one Act: the pipeline reads
        # one PDF as one document, so it is listed but not offered.
        multivolume = a.get("field_in_force_multivolume") == "yes"
        out.append({"version": version, "effective": a.get("field_in_force_effective_date"),
                    "pages": a.get("field_in_force_pages"),
                    "pdf_url": None if multivolume else _pdf_url(x, by_id),
                    "note": "in volumes" if multivolume else None})
    return sorted(out, key=lambda v: (int(re.match(r"\d+", str(v["version"])).group()), str(v["version"])))
