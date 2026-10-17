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

from corpus.amending.fetch import CONTENT, SITE, NotFound, _get, slug, slugs

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


def _node(path: str, get) -> dict:
    route = json.loads(get(f"{CONTENT}/api/v1/route?" + urllib.parse.urlencode({"site": SITE, "path": path})))
    return json.loads(get(f"{route['data']['attributes']['endpoint']}?"
                          + urllib.parse.urlencode({"site": SITE, "include": _INCLUDE})))


def _path_by_number(act_no, year, get) -> "str | None":
    """The Act's address, asked for by its number and year -- which need
    no guessing at how the site spelled its title."""
    params = {"site": SITE, "filter[field_act_sr_number][value]": str(act_no),
              "filter[field_act_sr_year][value]": str(year), "fields[node--act_in_force]": "path", "page[limit]": "5"}
    data = json.loads(get(f"{CONTENT}/api/v1/node/act_in_force?" + urllib.parse.urlencode(params)))
    for row in data.get("data") or []:
        alias = (row["attributes"].get("path") or {}).get("alias")
        if alias:
            return alias
    return None


def locate(title: str, act_no, year, path: "str | None" = None, get=_get) -> dict:
    """The Act's record on the site, with its versions included.

    Its address can't be worked out from its title: the site drops small
    words from some ("control-weapons-act-1990"), drops apostrophes,
    cuts long titles short, and files a repealed Act under
    /repealed-revoked/. So the address the site's own search gave is
    tried first, then the guesses, then a lookup by number and year.
    `act_no` and `year` are checked against the record, so an address
    that leads to another Act is refused."""
    candidates = [path] if path else []
    candidates += [f"/in-force/acts/{s}" for s in slugs(title)]
    tried, seen = [], set()
    for candidate in list(dict.fromkeys(candidates)) + [None]:
        if candidate is None:
            if not act_no or not year:
                break
            try:
                candidate = _path_by_number(act_no, year, get)
            except Exception as e:
                tried.append(f"No. {act_no}/{year} ({e})")
                break
            if not candidate or candidate in seen:
                break
        seen.add(candidate)
        try:
            node = _node(candidate, get)
        except Exception as e:
            tried.append(f"{candidate} ({e})")
            continue
        attrs = node["data"]["attributes"]
        if act_no and (str(attrs.get("field_act_sr_number")), str(attrs.get("field_act_sr_year"))) != (str(act_no), str(year)):
            tried.append(f"{candidate} (No. {attrs.get('field_act_sr_number')}/{attrs.get('field_act_sr_year')}, "
                         f"not {act_no}/{year})")
            continue
        return node
    raise NotFound(f"{title} was not found on legislation.vic.gov.au: tried " + "; ".join(tried))


def available(title: str, act_no, year, get=_get, path: "str | None" = None) -> list[dict]:
    """[{"version", "effective", "pages", "pdf_url", "volume_urls"?}],
    oldest first. See locate for how the Act is found."""
    node = locate(title, act_no, year, path, get)
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
        # A reprint in volumes still has its whole text as one authorised
        # PDF (Road Safety Act v237: 978 pages, every line parsed). Where
        # it doesn't, its volumes are joined on download -- resolved then,
        # not here, since a long history would cost a request a volume.
        multivolume = a.get("field_in_force_multivolume") == "yes"
        entry = {"version": version, "effective": a.get("field_in_force_effective_date"),
                 "pages": a.get("field_in_force_pages"), "pdf_url": _pdf_url(x, by_id),
                 "note": "in volumes" if multivolume else None}
        if multivolume and not entry["pdf_url"]:
            entry["volumes"] = [ref for ref in (x["relationships"].get("field_in_force_multivolume_ver", {})
                                                .get("data") or [])]
            if not entry["volumes"]:
                del entry["volumes"]
        out.append(entry)
    return sorted(out, key=lambda v: (int(re.match(r"\d+", str(v["version"])).group()), str(v["version"])))


def fetchable(entry: dict) -> bool:
    return bool(entry.get("pdf_url") or entry.get("volumes"))


def _volume_url(ref: dict, get) -> "str | None":
    kind, bundle = ref["type"].split("--", 1)
    data = json.loads(get(f"{CONTENT}/api/v1/{kind}/{bundle}/{ref['id']}?" + urllib.parse.urlencode(
        {"site": SITE, "include": "field_volume_authorised_version.field_media_file"})))
    by_id = {x["id"]: x for x in data.get("included", [])}
    media_ref = (data["data"]["relationships"].get("field_volume_authorised_version") or {}).get("data")
    media = media_ref and by_id.get(media_ref["id"])
    file_ref = media and (media["relationships"].get("field_media_file") or {}).get("data")
    file = file_ref and by_id.get(file_ref["id"])
    if not file:
        return None
    attrs = file["attributes"]
    return attrs.get("url") or urllib.parse.urljoin(CONTENT, attrs["uri"]["url"])


def download(entry: dict, get=_get) -> bytes:
    """One reprint's PDF: its own file, or its volumes joined in order."""
    if entry.get("pdf_url"):
        content = get(entry["pdf_url"])
        if not content.startswith(b"%PDF"):
            raise ValueError(f"{entry['pdf_url']} is not a PDF")
        return content
    import pymupdf

    joined = pymupdf.open()
    for n, ref in enumerate(entry.get("volumes") or [], 1):
        url = _volume_url(ref, get)
        if not url:
            raise ValueError(f"volume {n} of version {entry['version']} has no PDF")
        content = get(url)
        if not content.startswith(b"%PDF"):
            raise ValueError(f"{url} (volume {n}) is not a PDF")
        with pymupdf.open(stream=content, filetype="pdf") as volume:
            joined.insert_pdf(volume)
    if not joined.page_count:
        raise ValueError(f"version {entry['version']} has no PDF")
    return joined.tobytes()
