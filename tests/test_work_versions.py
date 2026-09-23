"""The dashboard's "Add a version": a PDF placed among a work's other
versions by the number it states, and refused when it is not one."""
import json

import pytest

import corpus.web.dashboard as dashboard


@pytest.fixture
def held(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.chdir(tmp_path)
    dashboard._DASHBOARD_USERNAME = None
    (tmp_path / "acts" / "cpa").mkdir(parents=True)
    (tmp_path / "acts" / "cpa" / "v114.pdf").write_bytes(b"%PDF")
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "cpa-v114.json").write_text(json.dumps(
        {"nodes": [], "version": {"version": 114, "act_no": "7", "year": 2009}}))
    monkeypatch.setattr(dashboard, "_pdf_version", lambda pdf: 114)
    ran = []
    monkeypatch.setattr(dashboard, "_run_parse_subprocess", lambda cmd: ran.append(cmd) or (True, 0, ""))
    return TestClient(dashboard.app), ran


def _upload(client, front_matter, monkeypatch):
    monkeypatch.setattr(dashboard, "read_front_matter", lambda path: front_matter)
    return client.post("/api/works/cpa/versions", files={"pdf": ("new.pdf", b"%PDF", "application/pdf")})


def test_a_version_of_the_same_act_is_added_under_its_own_number(held, monkeypatch):
    client, ran = held

    res = _upload(client, {"version": 113, "act_no": "7", "year": 2009}, monkeypatch)

    assert res.status_code == 200 and res.json()["slug"] == "cpa-v113"
    assert "acts/cpa/new.pdf" in " ".join(ran[0])


def test_a_different_act_is_refused(held, monkeypatch):
    client, ran = held

    res = _upload(client, {"version": 3, "act_no": "6231", "year": 1958}, monkeypatch)

    assert res.status_code == 400 and "a different Act" in res.json()["detail"]
    assert not ran


def test_a_version_already_held_is_refused(held, monkeypatch):
    client, ran = held

    res = _upload(client, {"version": 114, "act_no": "7", "year": 2009}, monkeypatch)

    assert res.status_code == 400 and "already held" in res.json()["detail"]


def test_a_pdf_stating_no_version_is_refused(held, monkeypatch):
    client, ran = held

    res = _upload(client, {"act_no": "7", "year": 2009}, monkeypatch)

    assert res.status_code == 400 and "no Authorised Version" in res.json()["detail"]
    assert not list((dashboard.BASE_DIR / "acts").glob(".incoming-*"))


# ---------------------------------------------------------------------
# Every version legislation.vic.gov.au holds, fetched from History review
# ---------------------------------------------------------------------

def _site(pdfs: dict, act_no="7", multivolume=()):
    """The content API's in-force record, as fetch.locate's fake does it:
    {version: (authorised url | None, plain url | None)}."""
    included = []
    for version, (authorised, plain) in pdfs.items():
        rel = {}
        for field, url in (("field_in_force_authorized_ver", authorised), ("field_in_force_version", plain)):
            rel[field] = {"data": []}
            if url:
                included += [{"type": "media--pdf", "id": f"m{url}", "attributes": {},
                              "relationships": {"field_media_file": {"data": {"type": "file--file", "id": f"f{url}"}}}},
                             {"type": "file--file", "id": f"f{url}", "attributes": {"uri": {"url": url}}}]
                rel[field]["data"].append({"type": "media--pdf", "id": f"m{url}"})
        included.append({"type": "paragraph--in_force_act_version", "id": f"p{version}", "relationships": rel,
                         "attributes": {"field_in_force_version_number": f"{version:03d}",
                                        "field_in_force_effective_date": "2026-07-01",
                                        "field_in_force_multivolume": "yes" if version in multivolume else "no"}})
    included.append({"type": "paragraph--in_force_act_version", "id": "p59A", "relationships": {},
                     "attributes": {"field_in_force_version_number": "059A"}})
    node = {"data": {"attributes": {"field_act_sr_number": act_no, "field_act_sr_year": "2009"}}, "included": included}

    def get(url):
        if "/route?" in url:
            assert "in-force%2Facts%2Fcriminal-procedure-act-2009" in url
            return json.dumps({"data": {"attributes": {"endpoint": "https://c/node/x"}}}).encode()
        return json.dumps(node).encode()
    return get


def test_the_sites_versions_are_listed_with_a_pdf_each():
    from corpus.history.versions import available

    got = available("Criminal Procedure Act 2009", "7", 2009, _site(
        {114: ("/a114.pdf", None), 1: (None, "/p001.pdf"), 50: ("/a050.pdf", "/p050.pdf"), 60: ("/a060.pdf", None)},
        multivolume={60}))

    assert [(v["version"], v["pdf_url"]) for v in got] == [
        (1, "https://content.legislation.vic.gov.au/p001.pdf"), (50, "https://content.legislation.vic.gov.au/a050.pdf"),
        ("059A", None), (60, None), (114, "https://content.legislation.vic.gov.au/a114.pdf")], \
        "the authorised PDF where there is one; none offered for a lettered reprint or one in volumes"


def test_a_title_leading_to_another_act_is_refused():
    from corpus.amending.fetch import NotFound
    from corpus.history.versions import available

    with pytest.raises(NotFound):
        available("Criminal Procedure Act 2009", "7", 2009, _site({114: ("/a.pdf", None)}, act_no="8"))


def test_a_fetched_version_is_added_as_an_uploaded_one_is(held, monkeypatch):
    client, ran = held
    monkeypatch.setattr(dashboard, "_site_versions", lambda work: [
        {"version": 1, "pdf_url": "https://c/p001.pdf"}, {"version": 2, "pdf_url": None}])
    monkeypatch.setattr(dashboard.amending_fetch, "_get", lambda url: b"%PDF v1")
    monkeypatch.setattr(dashboard, "read_front_matter", lambda path: {"version": 1, "act_no": "7", "year": 2009})

    res = client.post("/api/works/cpa/versions/fetch/1")
    assert res.status_code == 200 and res.json()["slug"] == "cpa-v1"
    assert (dashboard.BASE_DIR / "acts" / "cpa" / "cpa-v001.pdf").read_bytes() == b"%PDF v1"
    assert client.post("/api/works/cpa/versions/fetch/2").status_code == 404, "no single PDF to fetch"

    monkeypatch.setattr(dashboard, "read_front_matter", lambda path: {"version": 3, "act_no": "7", "year": 2009})
    res = client.post("/api/works/cpa/versions/fetch/1")
    assert res.status_code == 400 and "the PDF says 3" in res.json()["detail"]
