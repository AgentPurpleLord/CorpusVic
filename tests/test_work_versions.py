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
