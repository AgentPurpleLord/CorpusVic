"""Finding an Act on legislation.vic.gov.au, and every reprint of it.

An Act's address can't be rebuilt from its title: of fifty Acts the
site's own search returned, nineteen weren't where the title said --
small words dropped ("control-weapons-act-1990"), apostrophes dropped,
long titles cut short, repealed Acts under /repealed-revoked/.
"""
import json
import urllib.parse

import pymupdf
import pytest

from corpus.amending.fetch import NotFound, slug, slugs
from corpus.history import versions


def _record(no="24", year="1990", versions_=()):
    included = []
    for n, multivolume, pdf, volumes in versions_:
        rel = {"field_in_force_authorized_ver": {"data": [{"type": "media--pdf", "id": f"m{n}"}] if pdf else []},
               "field_in_force_multivolume_ver": {"data": [{"type": "paragraph--act_sr_volume", "id": v}
                                                           for v in volumes]}}
        included.append({"type": "paragraph--in_force_act_version", "id": f"v{n}",
                         "attributes": {"field_in_force_version_number": str(n),
                                        "field_in_force_multivolume": "yes" if multivolume else "no"},
                         "relationships": rel})
        if pdf:
            included.append({"type": "media--pdf", "id": f"m{n}",
                             "relationships": {"field_media_file": {"data": {"id": f"f{n}"}}}})
            included.append({"type": "file--file", "id": f"f{n}", "attributes": {"url": f"https://x/{n}.pdf"}})
    return {"data": {"attributes": {"field_act_sr_number": no, "field_act_sr_year": year}}, "included": included}


def _site(pages: dict, by_number: "str | None" = None):
    """A fake content API answering only the addresses in `pages`."""
    asked = []

    def get(url):
        asked.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if "/api/v1/route" in url:
            if q["path"] not in pages:
                raise OSError("HTTP Error 404: Not Found")
            return json.dumps({"data": {"attributes": {"endpoint": "https://x/node/" + q["path"]}}}).encode()
        if "/node/act_in_force?" in url:
            rows = [{"attributes": {"path": {"alias": by_number}}}] if by_number else []
            return json.dumps({"data": rows}).encode()
        return json.dumps(pages[url.split("https://x/node/", 1)[1].split("?", 1)[0]]).encode()
    return get, asked


def test_the_address_the_search_gave_is_used_first():
    get, asked = _site({"/in-force/acts/control-weapons-act-1990": _record()})
    versions.locate("Control of Weapons Act 1990", "24", 1990, "/in-force/acts/control-weapons-act-1990", get)
    assert "control-of-weapons" not in asked[0]


def test_without_one_the_shortened_title_is_tried():
    get, _ = _site({"/in-force/acts/control-weapons-act-1990": _record()})
    assert versions.locate("Control of Weapons Act 1990", "24", 1990, get=get)


def test_failing_every_guess_the_act_is_found_by_its_number_and_year():
    """A repealed Act, and a title the site cut short, are where no title
    says."""
    path = "/repealed-revoked/acts/forestry-rights-act-1996"
    get, _ = _site({path: _record("29", "1996")}, by_number=path)
    assert versions.locate("Forestry Rights Act 1996", "29", 1996, get=get)


def test_an_address_leading_to_another_act_is_refused():
    get, _ = _site({"/in-force/acts/control-weapons-act-1990": _record("99", "2001")})
    with pytest.raises(NotFound) as e:
        versions.locate("Control of Weapons Act 1990", "24", 1990, get=get)
    assert "not 24/1990" in str(e.value)


def test_an_apostrophe_joins_its_word():
    assert slug("Forestry Pulp and Paper Company's Act 1949") == "forestry-pulp-and-paper-companys-act-1949"
    assert slugs("Control of Weapons Act 1990") == ["control-of-weapons-act-1990", "control-weapons-act-1990"]


def test_a_reprint_in_volumes_is_offered():
    """Its single authorised PDF where it has one; its volumes otherwise."""
    path = "/in-force/acts/road-safety-act-1986"
    get, _ = _site({path: _record("127", "1986", [(236, True, True, ["a", "b"]), (237, True, False, ["c", "d"]),
                                                    (238, True, False, [])])})
    found = {v["version"]: v for v in versions.available("Road Safety Act 1986", "127", 1986, get, path)}
    assert found[236]["pdf_url"] == "https://x/236.pdf" and found[236]["note"] == "in volumes"
    assert found[237]["pdf_url"] is None and [r["id"] for r in found[237]["volumes"]] == ["c", "d"]
    assert versions.fetchable(found[236]) and versions.fetchable(found[237]) and not versions.fetchable(found[238])


def _pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    return doc.tobytes()


def test_volumes_are_joined_into_one_pdf_in_order():
    files = {"https://x/vol1.pdf": _pdf(2), "https://x/vol2.pdf": _pdf(3)}

    def get(url):
        if url in files:
            return files[url]
        volume = url.split("/act_sr_volume/", 1)[1].split("?", 1)[0]
        return json.dumps({
            "data": {"relationships": {"field_volume_authorised_version": {"data": {"id": f"m-{volume}"}}}},
            "included": [{"type": "media--pdf", "id": f"m-{volume}",
                          "relationships": {"field_media_file": {"data": {"id": f"f-{volume}"}}}},
                         {"type": "file--file", "id": f"f-{volume}", "attributes": {"url": f"https://x/{volume}.pdf"}}],
        }).encode()

    entry = {"version": 237, "pdf_url": None, "volumes": [{"type": "paragraph--act_sr_volume", "id": "vol1"},
                                                          {"type": "paragraph--act_sr_volume", "id": "vol2"}]}
    joined = versions.download(entry, get)
    with pymupdf.open(stream=joined, filetype="pdf") as doc:
        assert doc.page_count == 5


def test_a_page_that_is_not_a_pdf_is_refused():
    with pytest.raises(ValueError):
        versions.download({"version": 1, "pdf_url": "https://x/1.pdf"}, lambda url: b"<html>Error</html>")
