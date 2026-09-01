"""Tests for dashboard.py's pure, non-interactive logic: slug validation
and per-Act status computation. The FastAPI endpoints themselves (upload/
export/bill-link/review-proxy, all thin wrappers around this logic plus
subprocess calls and a reverse proxy to a child review.py process) are
deliberately not covered here -- they were exercised end to end against a
live server instead (curl and Playwright), same approach test_review.py
takes for review.py's own endpoints."""
import json

import pytest

import dashboard
from conftest import make_node


def test_validate_slug_accepts_lowercase_hyphenated():
    assert dashboard._validate_slug("criminal-procedure-act") == "criminal-procedure-act"
    assert dashboard._validate_slug("act50") == "act50"


@pytest.mark.parametrize("bad", ["../etc/passwd", "Foo-Bar", "foo_bar", "foo/bar", "", "foo--bar".replace("--", "..")])
def test_validate_slug_rejects_anything_not_plain_lowercase_hyphenated(bad):
    with pytest.raises(Exception):
        dashboard._validate_slug(bad)


def test_discover_slugs_unions_uploaded_pdfs_and_already_parsed_acts(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    (tmp_path / "acts").mkdir()
    (tmp_path / "acts" / "crimes-act.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "data" / "ai_parsed").mkdir(parents=True)
    (tmp_path / "data" / "ai_parsed" / "evidence-act.json").write_text("{}", encoding="utf-8")

    assert dashboard.discover_slugs() == ["crimes-act", "evidence-act"]


def test_act_status_reports_not_parsed_when_no_ai_parsed_json_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    status = dashboard.act_status("crimes-act")
    assert status == {
        "slug": "crimes-act",
        "has_pdf": False,
        "parsed": False,
        "node_count": None,
        "unit_count": None,
        "reviewed_units": None,
        "review_status": "not-parsed",
        "akn_exported": False,
        "markdown_exported": False,
    }


def _write_parsed(tmp_path, slug: str, nodes: list[dict]) -> None:
    parsed_dir = tmp_path / "data" / "ai_parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    (parsed_dir / f"{slug}.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")


def test_act_status_counts_nodes_and_units_once_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    nodes = [
        make_node("section", "1", "Murder"),
        make_node("subsection", "1", None, "text"),
        make_node("section", "2", "Manslaughter"),
    ]
    _write_parsed(tmp_path, "crimes-act", nodes)

    status = dashboard.act_status("crimes-act")
    assert status["parsed"] is True
    assert status["node_count"] == 3
    assert status["unit_count"] == 2  # section 1 (+ its subsection), section 2
    assert status["review_status"] == "not-started"
    assert status["reviewed_units"] == 0


def test_act_status_is_reviewed_once_every_unit_is_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed(tmp_path, "crimes-act", nodes)

    verified_dir = tmp_path / "data" / "verified"
    verified_dir.mkdir(parents=True)
    committed = [dict(nodes[0], _unit_end_index=0), dict(nodes[1], _unit_end_index=1)]
    (verified_dir / "crimes-act.json").write_text(json.dumps(committed), encoding="utf-8")

    status = dashboard.act_status("crimes-act")
    assert status["review_status"] == "reviewed"
    assert status["reviewed_units"] == 2


def test_act_status_is_in_progress_when_only_some_units_are_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed(tmp_path, "crimes-act", nodes)

    verified_dir = tmp_path / "data" / "verified"
    verified_dir.mkdir(parents=True)
    committed = [dict(nodes[0], _unit_end_index=0)]
    (verified_dir / "crimes-act.json").write_text(json.dumps(committed), encoding="utf-8")

    status = dashboard.act_status("crimes-act")
    assert status["review_status"] == "in-progress"
    assert status["reviewed_units"] == 1


def test_act_status_flags_akn_and_markdown_exports(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    _write_parsed(tmp_path, "crimes-act", [make_node("section", "1", "Murder")])
    (tmp_path / "data" / "akn").mkdir(parents=True)
    (tmp_path / "data" / "akn" / "crimes-act.xml").write_text("<akn/>", encoding="utf-8")
    (tmp_path / "data" / "markdown" / "crimes-act").mkdir(parents=True)
    (tmp_path / "data" / "markdown" / "crimes-act" / "index.md").write_text("# Crimes Act", encoding="utf-8")

    status = dashboard.act_status("crimes-act")
    assert status["akn_exported"] is True
    assert status["markdown_exported"] is True
