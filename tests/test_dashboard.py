"""Tests for dashboard.py's pure, non-interactive logic: slug validation
and per-Act status computation. The FastAPI endpoints themselves (upload/
export/bill-link/review-proxy, all thin wrappers around this logic plus
subprocess calls and a reverse proxy to a child review.py process) are
deliberately not covered here -- they were exercised end to end against a
live server instead (curl and Playwright), same approach test_review.py
takes for review.py's own endpoints."""
import json
import time

import pytest

import dashboard
from conftest import make_node


@pytest.fixture(autouse=True)
def _reset_auth_state():
    """Auth state lives in module-level dicts so it survives server
    restarts within one process run -- reset it around each test so tests
    can't see each other's sessions/lockouts."""
    dashboard._SESSIONS.clear()
    dashboard._FAILED_ATTEMPTS.clear()
    yield
    dashboard._SESSIONS.clear()
    dashboard._FAILED_ATTEMPTS.clear()


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


def test_check_credentials_accepts_only_the_configured_username_and_password():
    dashboard._configure_auth("alice", "s3cret")
    assert dashboard._check_credentials("alice", "s3cret") is True
    assert dashboard._check_credentials("alice", "wrong") is False
    assert dashboard._check_credentials("bob", "s3cret") is False


def test_configure_auth_never_keeps_the_password_in_plain_text():
    dashboard._configure_auth("alice", "s3cret")
    assert dashboard._PASSWORD_HASH != b"s3cret"
    assert b"s3cret" not in dashboard._PASSWORD_HASH


def test_new_session_is_valid_until_removed():
    token = dashboard._new_session()
    assert dashboard._session_is_valid(token) is True
    assert dashboard._session_is_valid("some-other-token") is False
    assert dashboard._session_is_valid(None) is False


def test_session_is_invalid_and_pruned_once_past_its_expiry():
    token = dashboard._new_session()
    dashboard._SESSIONS[token] = time.time() - 1  # force it into the past
    assert dashboard._session_is_valid(token) is False
    assert token not in dashboard._SESSIONS


def test_lockout_kicks_in_once_failures_reach_the_threshold():
    ip = "10.0.0.1"
    for _ in range(dashboard._LOCKOUT_THRESHOLD):
        assert dashboard._is_locked_out(ip) is False
        dashboard._record_failed_login(ip)
    assert dashboard._is_locked_out(ip) is True


def test_lockout_is_scoped_to_the_offending_client_only():
    for _ in range(dashboard._LOCKOUT_THRESHOLD):
        dashboard._record_failed_login("10.0.0.1")
    assert dashboard._is_locked_out("10.0.0.1") is True
    assert dashboard._is_locked_out("10.0.0.2") is False


def test_lockout_clears_once_its_failures_age_out_of_the_window():
    ip = "10.0.0.3"
    stale = time.time() - dashboard._LOCKOUT_WINDOW_SECONDS - 1
    dashboard._FAILED_ATTEMPTS[ip] = [stale] * dashboard._LOCKOUT_THRESHOLD
    assert dashboard._is_locked_out(ip) is False


def test_resolve_auth_forces_a_password_change_on_a_brand_new_install(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "_AUTH_STORE_PATH", tmp_path / ".dashboard_auth.json")

    using_placeholder = dashboard._resolve_auth(None, None)

    assert using_placeholder is True
    assert dashboard._DASHBOARD_USERNAME == dashboard._DEFAULT_USERNAME
    assert dashboard._MUST_CHANGE_PASSWORD is True
    assert dashboard._check_credentials(dashboard._DEFAULT_USERNAME, dashboard._DEFAULT_PASSWORD) is True
    assert dashboard._AUTH_STORE_PATH.exists()


def test_resolve_auth_reuses_the_persisted_store_on_a_later_run(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "_AUTH_STORE_PATH", tmp_path / ".dashboard_auth.json")
    dashboard._resolve_auth(None, None)  # first run: writes the placeholder store
    first_hash, first_salt = dashboard._PASSWORD_HASH, dashboard._PASSWORD_SALT

    # Simulate the password having since been changed via /api/change-password.
    dashboard._configure_auth(dashboard._DEFAULT_USERNAME, "a-real-password", must_change=False)
    dashboard._save_auth_store()

    using_placeholder = dashboard._resolve_auth(None, None)  # a later run, e.g. after a restart

    assert using_placeholder is False
    assert dashboard._MUST_CHANGE_PASSWORD is False
    assert (dashboard._PASSWORD_HASH, dashboard._PASSWORD_SALT) != (first_hash, first_salt)
    assert dashboard._check_credentials(dashboard._DEFAULT_USERNAME, "a-real-password") is True


def test_resolve_auth_prefers_explicit_credentials_over_the_persisted_store(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "_AUTH_STORE_PATH", tmp_path / ".dashboard_auth.json")
    dashboard._resolve_auth(None, None)  # establishes a placeholder store on disk
    stored_before = dashboard._AUTH_STORE_PATH.read_text(encoding="utf-8")

    using_placeholder = dashboard._resolve_auth("bob", "explicit-password")

    assert using_placeholder is False
    assert dashboard._DASHBOARD_USERNAME == "bob"
    assert dashboard._MUST_CHANGE_PASSWORD is False
    assert dashboard._check_credentials("bob", "explicit-password") is True
    # Explicit creds are this run's source of truth -- they don't overwrite the store.
    assert dashboard._AUTH_STORE_PATH.read_text(encoding="utf-8") == stored_before
