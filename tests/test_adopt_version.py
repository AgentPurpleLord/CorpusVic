"""corpus/review/adopt_version.py: a document held under its plain name
becomes version N of its work, carrying its review work with it."""
import json

import pytest

from corpus.review import adopt_version, review_sync
from corpus.storage import db


def _setup(base, version=42):
    (base / "acts").mkdir()
    (base / "acts" / "evidence-act.pdf").write_bytes(b"%PDF-1.4 stand-in")
    parsed = base / "data" / "parsed"
    parsed.mkdir(parents=True)
    (parsed / "evidence-act.json").write_text(json.dumps({
        "act": "evidence-act", "source": "acts/evidence-act.pdf", "nodes": [],
        "version": {"version": version} if version is not None else {},
    }))
    review = base / "data" / "review" / "evidence-act"
    review.mkdir(parents=True)
    (review / "parse_state.jsonl").write_text(json.dumps(
        {"act": "evidence-act", "fingerprint": "fp", "updated_at": "2026-01-01"}, sort_keys=True) + "\n")
    (base / "data" / "diagnostics").mkdir()
    (base / "data" / "diagnostics" / "evidence-act.json").write_text("[]")
    (base / "data" / "diagnostics" / "evidence-act-50.json").write_text("[]")
    (base / "data" / "bill_links").mkdir()
    (base / "data" / "bill_links" / "evidence-bill-to-evidence-act.json").write_text(
        json.dumps({"bill_slug": "evidence-bill", "act_slug": "evidence-act", "links": []}))
    review_sync.import_(base, backup=False)


def test_adopting_moves_everything_keyed_by_the_documents_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)

    adopt_version.adopt("evidence-act", tmp_path)

    assert (tmp_path / "acts" / "evidence-act" / "evidence-act.pdf").is_file()
    parse = json.loads((tmp_path / "data" / "parsed" / "evidence-act-v42.json").read_text())
    assert parse["source"] == "acts/evidence-act/evidence-act.pdf" and parse["act"] == "evidence-act-v42"
    assert db.load_parse_fingerprint("evidence-act-v42", tmp_path) == "fp"
    assert db.load_parse_fingerprint("evidence-act", tmp_path) is None
    assert (tmp_path / "data" / "diagnostics" / "evidence-act-v42.json").is_file()
    assert (tmp_path / "data" / "diagnostics" / "evidence-act-50.json").is_file(), "another document's file"
    link = json.loads((tmp_path / "data" / "bill_links" / "evidence-bill-to-evidence-act.json").read_text())
    assert link["act_slug"] == "evidence-act-v42"


def test_a_dry_run_moves_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)

    steps = adopt_version.plan("evidence-act", tmp_path)

    assert steps["new_slug"] == "evidence-act-v42"
    assert (tmp_path / "data" / "parsed" / "evidence-act.json").is_file()


def test_a_document_stating_no_version_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path, version=None)
    (tmp_path / "acts" / "evidence-act.pdf").unlink()

    with pytest.raises(adopt_version.Refused, match="no Authorised Version"):
        adopt_version.plan("evidence-act", tmp_path)


def test_a_version_is_refused(tmp_path):
    with pytest.raises(adopt_version.Refused, match="already version"):
        adopt_version.plan("evidence-act-v42", tmp_path)


def test_an_existing_target_is_refused_rather_than_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _setup(tmp_path)
    (tmp_path / "data" / "parsed" / "evidence-act-v42.json").write_text("{}")

    with pytest.raises(adopt_version.Refused, match="already exists"):
        adopt_version.plan("evidence-act", tmp_path)
