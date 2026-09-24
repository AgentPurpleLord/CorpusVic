"""Tests for dashboard.py's pure, non-interactive logic: slug validation
and per-Act status computation. The FastAPI endpoints themselves (upload/
export/bill-link/review-proxy, all thin wrappers around this logic plus
subprocess calls and a reverse proxy to a child review.py process) are
deliberately not covered here -- they were exercised end to end against a
live server instead (curl and Playwright), same approach test_review.py
takes for review.py's own endpoints."""
import corpus.storage.db
import corpus.publishing.reader
import corpus.review.sync
import corpus.search.search
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from corpus.web import dashboard
from corpus.domain import diffing
from corpus.storage import db
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
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "evidence-act.json").write_text("{}", encoding="utf-8")

    assert dashboard.discover_slugs() == ["crimes-act", "evidence-act"]


def test_act_status_reports_not_parsed_when_no_parsed_json_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    status = dashboard.act_status("crimes-act")
    assert status == {
        "slug": "crimes-act",
        "has_pdf": False,
        # Nothing reaches the public site because it happened to get
        # parsed -- see corpus/db.py's publication table.
        "published": False,
        "work": "crimes-act",
        "version": None,
        "version_as_at": None,
        "profile": None,
        "parsed": False,
        "kind": "act",
        "node_count": None,
        "unit_count": None,
        "reviewed_units": None,
        "review_status": "not-parsed",
        "akn_exported": False,
        "markdown_exported": False,
    }


def _write_parsed(tmp_path, slug: str, nodes: list[dict]) -> None:
    parsed_dir = tmp_path / "data" / "parsed"
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

    committed = [
        dict(nodes[0], _node_id="s0", _source_node_index=0, _unit_end_index=0),
        dict(nodes[1], _node_id="s1", _source_node_index=1, _unit_end_index=1),
    ]
    db.save_verified("crimes-act", committed, base_dir=tmp_path)

    status = dashboard.act_status("crimes-act")
    assert status["review_status"] == "reviewed"
    assert status["reviewed_units"] == 2


def test_act_status_is_in_progress_when_only_some_units_are_committed(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    nodes = [make_node("section", "1", "Murder"), make_node("section", "2", "Manslaughter")]
    _write_parsed(tmp_path, "crimes-act", nodes)

    committed = [dict(nodes[0], _node_id="s0", _source_node_index=0, _unit_end_index=0)]
    db.save_verified("crimes-act", committed, base_dir=tmp_path)

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


# --- parsing/reparsing: pure param validation + command building --------


def test_validate_parse_params_accepts_defaults():
    dashboard._validate_parse_params("act", "", "", "")  # no exception


@pytest.mark.parametrize(
    "kind,profile,start_page,end_page",
    [
        ("play", "", "", ""),
        ("act", "Not A Slug", "", ""),
        ("act", "", "not-a-number", ""),
        ("act", "", "", "not-a-number"),
    ],
)
def test_validate_parse_params_rejects_bad_input(kind, profile, start_page, end_page):
    with pytest.raises(Exception):
        dashboard._validate_parse_params(kind, profile, start_page, end_page)


def test_build_parse_command_for_an_em_ignores_every_other_option():
    cmd = dashboard._build_parse_command(Path("acts/some-bill-em.pdf"), "em", "profile", "5", "10")
    assert cmd == [sys.executable, "-m", "corpus.parsing.run_em_pipeline", "acts/some-bill-em.pdf"]


def test_build_parse_command_for_a_bill_sets_document_type():
    cmd = dashboard._build_parse_command(Path("acts/some-bill.pdf"), "bill", "", "", "")
    assert cmd == [sys.executable, "-m", "corpus.parsing.run_pipeline", "acts/some-bill.pdf", "--document-type", "bill"]


def test_build_parse_command_includes_profile_and_page_range_when_given():
    cmd = dashboard._build_parse_command(Path("acts/x.pdf"), "act", "my-profile", "5", "20")
    assert cmd == [
        sys.executable, "-m", "corpus.parsing.run_pipeline", "acts/x.pdf", "--document-type", "act",
        "--profile", "my-profile", "--start-page", "5", "--end-page", "20",
    ]


# Re-parsing only what nobody approved: the mode has to reach the parser,
# because the whole difference it makes is decided there (see
# corpus/parsing/reparse.py). A mode the dashboard swallows would look
# exactly like a mode that did nothing.

def test_keeping_approved_work_is_asked_for_on_the_command_line():
    cmd = dashboard._build_parse_command(Path("acts/x.pdf"), "act", "", "", "", True)
    assert cmd[-1] == "--keep-accepted"


def test_and_is_not_asked_for_otherwise():
    """The default re-parse is unchanged -- it still withdraws acceptance
    from wording nobody has read."""
    cmd = dashboard._build_parse_command(Path("acts/x.pdf"), "act", "", "", "")
    assert "--keep-accepted" not in cmd


def test_an_em_takes_no_notice_of_it():
    """run_em_pipeline has no such flag; handing it one would fail the
    parse rather than ignore it."""
    cmd = dashboard._build_parse_command(Path("acts/x-em.pdf"), "em", "", "", "", True)
    assert "--keep-accepted" not in cmd


def test_find_source_pdf_matches_by_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    (tmp_path / "acts").mkdir()
    (tmp_path / "acts" / "crimes-act.pdf").write_bytes(b"%PDF-1.4")
    assert dashboard._find_source_pdf("crimes-act") == tmp_path / "acts" / "crimes-act.pdf"


def test_find_source_pdf_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    assert dashboard._find_source_pdf("no-such-act") is None


def test_repo_relative_strips_the_checkout_path(tmp_path, monkeypatch):
    """The path handed to run_pipeline ends up verbatim in the
    committed data/parsed/<slug>.json -- it has to stay repo-relative
    so it doesn't bake in one machine's checkout location."""
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    assert dashboard._repo_relative(tmp_path / "acts" / "crimes-act.pdf") == "acts/crimes-act.pdf"


def test_repo_relative_leaves_a_path_outside_the_repo_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    outside = tmp_path / "elsewhere" / "x.pdf"
    assert dashboard._repo_relative(outside) == str(outside)


def test_act_status_names_the_profile_the_act_should_be_parsed_with(tmp_path, monkeypatch):
    """By name, not merely whether one exists: the re-parse dialog
    pre-fills this field, and it used to fill it with the slug -- which
    for a versioned Act names no profile at all."""
    from corpus.domain import profiles

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    profiles_dir = tmp_path / "corpus" / "profiles"
    profiles_dir.mkdir(parents=True)
    monkeypatch.setattr(profiles, "PROFILES_DIR", profiles_dir)
    (profiles_dir / "crimes-act.yaml").write_text("part: 'x'\n", encoding="utf-8")

    assert dashboard.act_status("crimes-act")["profile"] == "crimes-act"
    assert dashboard.act_status("evidence-act")["profile"] is None


# ---------------------------------------------------------------------
# Versions of a work
#
# An Act opts into version tracking by having its PDFs put in a directory
# named after it; each is then addressed by the work's name and its own
# Authorised Version number rather than by its filename.
# ---------------------------------------------------------------------

def _versioned_pdf(path, version):
    """A minimal PDF whose first page carries an Authorised Version block,
    since that is what the slug is actually derived from."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Authorised Version No. {version}")
    page.insert_text((72, 92), "Criminal Procedure Act 2009")
    page.insert_text((72, 112), "No. 7 of 2009")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def test_discover_slugs_reads_a_work_directory_as_that_works_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    _versioned_pdf(tmp_path / "acts" / "criminal-procedure-act" / "cpa-113.pdf", 113)
    _versioned_pdf(tmp_path / "acts" / "criminal-procedure-act" / "anything.pdf", 114)
    (tmp_path / "acts" / "sentencing-act.pdf").write_bytes(b"%PDF-1.4")

    # Named by the work and the version each PDF states, not by its file.
    assert dashboard.discover_slugs() == [
        "criminal-procedure-act-v113", "criminal-procedure-act-v114", "sentencing-act",
    ]


def test_act_status_splits_a_versioned_slug_into_its_work_and_version(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    _versioned_pdf(tmp_path / "acts" / "criminal-procedure-act" / "cpa-114.pdf", 114)

    status = dashboard.act_status("criminal-procedure-act-v114")

    assert status["work"] == "criminal-procedure-act"
    assert status["version"] == 114
    assert status["has_pdf"] is True  # found by version, not by filename


def test_a_profile_is_found_under_the_work_not_each_version(tmp_path, monkeypatch):
    # How an Act numbers its Parts is a fact about the Act, not about one
    # reprint of it -- one profile serves all its versions.
    from corpus.domain import profiles

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    profiles_dir = tmp_path / "corpus" / "profiles"
    profiles_dir.mkdir(parents=True)
    monkeypatch.setattr(profiles, "PROFILES_DIR", profiles_dir)
    (profiles_dir / "criminal-procedure-act.yaml").write_text("part: x", encoding="utf-8")

    assert dashboard.act_status("criminal-procedure-act-v114")["profile"] == "criminal-procedure-act"


def _write_bill_link(tmp_path, name: str, doc: dict) -> None:
    links_dir = tmp_path / "data" / "bill_links"
    links_dir.mkdir(parents=True, exist_ok=True)
    (links_dir / name).write_text(json.dumps({**doc, "links": []}), encoding="utf-8")


def test_bill_link_groups_merges_the_act_and_em_files_for_one_bill(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    _write_bill_link(tmp_path, "bill-to-act.json",
                     {"bill_slug": "criminal-procedure-bill-2008", "act_slug": "criminal-procedure-act-v114"})
    _write_bill_link(tmp_path, "bill-em-links.json",
                     {"bill_slug": "criminal-procedure-bill-2008", "em_slug": "criminal-procedure-bill-2008-em"})

    groups = dashboard._bill_link_groups()

    assert groups == [{
        "bill_slug": "criminal-procedure-bill-2008",
        "act_work": "criminal-procedure-act",
        "em_slug": "criminal-procedure-bill-2008-em",
    }]


def test_bill_link_groups_leaves_the_em_slot_null_without_an_em_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    _write_bill_link(tmp_path, "bill-to-act.json",
                     {"bill_slug": "some-bill-2020", "act_slug": "some-act-v1"})

    groups = dashboard._bill_link_groups()

    assert groups == [{"bill_slug": "some-bill-2020", "act_work": "some-act", "em_slug": None}]


def test_bill_link_groups_is_empty_without_a_bill_links_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)

    assert dashboard._bill_link_groups() == []


# ---------------------------------------------------------------------------
# The standing /legislation/<citation> resolver
# ---------------------------------------------------------------------------

def test_resolve_legislation_citation_finds_a_parsed_act_by_its_number(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "load_act_registry",
                        lambda: {"Crimes Act 1958": {"act_no": "6231", "year": "1958", "in_force": True}})
    monkeypatch.setattr(dashboard, "load_known_acts", lambda: {"crimes-act": "Crimes Act 1958"})
    _write_parsed(tmp_path, "crimes-act", [make_node("section", "1", "Purposes")])

    info = dashboard._resolve_legislation_citation("6231-1958")

    assert info == {"act_no": "6231", "year": 1958, "title": "Crimes Act 1958", "in_force": True, "slug": "crimes-act"}


def test_resolve_legislation_citation_names_a_known_act_that_isnt_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "load_act_registry",
                        lambda: {"Some Other Act 2004": {"act_no": "42", "year": "2004", "in_force": True}})
    monkeypatch.setattr(dashboard, "load_known_acts", lambda: {})

    info = dashboard._resolve_legislation_citation("42-2004")

    assert info["title"] == "Some Other Act 2004"
    assert info["slug"] is None


def test_resolve_legislation_citation_reports_nothing_for_an_unrecognised_number(monkeypatch):
    monkeypatch.setattr(dashboard, "load_act_registry", lambda: {})
    monkeypatch.setattr(dashboard, "load_known_acts", lambda: {})

    info = dashboard._resolve_legislation_citation("999999-1900")

    assert info == {"act_no": "999999", "year": 1900, "title": None, "in_force": None, "slug": None}


def test_resolve_legislation_citation_rejects_a_malformed_citation():
    assert dashboard._resolve_legislation_citation("not-a-number") == {
        "act_no": None, "year": None, "title": None, "in_force": None, "slug": None,
    }


def test_resolve_legislation_citation_accepts_a_year_less_old_style_number(monkeypatch):
    monkeypatch.setattr(dashboard, "load_act_registry",
                        lambda: {"Old Act": {"act_no": "8679", "year": "1962", "in_force": False}})
    monkeypatch.setattr(dashboard, "load_known_acts", lambda: {})

    info = dashboard._resolve_legislation_citation("8679")

    assert info["title"] == "Old Act" and info["year"] == 1962 and info["in_force"] is False


# ---------------------------------------------------------------------------
# Linking a timeline entry to its own page across versions
# ---------------------------------------------------------------------------

def test_provision_page_url_finds_an_ordinary_provisions_page():
    page_index = {"by_key": {diffing.provision_identity("section", None, "366"): "s366"}}
    entry = {"type": "section", "schedule": None, "number": "366"}

    assert dashboard._provision_page_url("cpa-v112", page_index, entry) == "/browse/cpa-v112/section/s366"


def test_provision_page_url_finds_a_pageable_schedules_own_page():
    # The whole point of the kind-aware key: a Schedule entry must not
    # borrow a same-numbered section's page, or come back with none at all.
    page_index = {"by_key": {
        diffing.provision_identity("section", None, "3"): "s3",
        diffing.provision_identity("schedule", None, "3"): "s3_2",
    }}
    entry = {"type": "schedule", "schedule": None, "number": "3"}

    assert dashboard._provision_page_url("cpa-v114", page_index, entry) == "/browse/cpa-v114/section/s3_2"


def test_provision_page_url_is_none_where_that_version_has_no_page_for_it():
    page_index = {"by_key": {}}
    entry = {"type": "schedule", "schedule": None, "number": "9"}

    assert dashboard._provision_page_url("cpa-v110", page_index, entry) is None


# ---------------------------------------------------------------------
# Serving under a base path (corpusvic.au/admin)
# ---------------------------------------------------------------------
# The app is mounted under the prefix, so its routes never mention it.
# What does need it is the two directions traffic crosses the boundary:
# a path coming in (which the auth gate compares against its own route
# names) and a URL going out (a redirect, a link in a rendered page).
# Both got this wrong first time, and both ways were silent-ish: the
# login page redirected to itself for ever, and logging in landed on the
# domain root instead of the admin tool.

@pytest.fixture
def _at_admin():
    """dashboard mounted at /admin for the duration of one test."""
    previous = dashboard._BASE_PATH
    dashboard._configure_base_path("/admin")
    yield
    dashboard._configure_base_path(previous)


@pytest.mark.parametrize("given, expected", [
    ("/admin", "/admin"), ("admin", "/admin"), ("/admin/", "/admin"), ("admin/", "/admin"),
    ("", ""), ("/", ""), ("   ", ""),
])
def test_a_base_path_is_normalised(given, expected):
    previous = dashboard._BASE_PATH
    try:
        assert dashboard._configure_base_path(given) == expected
    finally:
        dashboard._configure_base_path(previous)


def test_an_emitted_url_carries_the_base_path(_at_admin):
    """Every address handed to a browser has to be the one the browser
    will see. A bare "/login" would leave the mount and land on whatever
    is served at the domain root -- the public site."""
    assert dashboard._url("/login") == "/admin/login"
    assert dashboard._url("/browse/crimes-act") == "/admin/browse/crimes-act"


def test_an_emitted_url_at_the_root_is_unchanged():
    assert dashboard._url("/login") == "/login"


def test_an_incoming_path_is_read_without_the_base_path(_at_admin):
    """The other direction: the auth gate names its own routes, and the
    browser asks for them with the prefix on. Comparing the two without
    stripping it made /admin/login look protected, so it redirected to
    /admin/login -- for ever."""
    class _Req:
        def __init__(self, path):
            self.url = type("U", (), {"path": path})()

    assert dashboard._app_path(_Req("/admin/login")) == "/login"
    assert dashboard._app_path(_Req("/admin/")) == "/"
    assert dashboard._app_path(_Req("/admin")) == "/"
    assert dashboard._app_path(_Req("/admin/api/acts")) == "/api/acts"


def test_a_path_that_only_looks_like_the_base_path_is_left_alone(_at_admin):
    """"/administration" starts with "/admin" as a string and is not
    inside it as a path."""
    class _Req:
        def __init__(self, path):
            self.url = type("U", (), {"path": path})()

    assert dashboard._app_path(_Req("/administration")) == "/administration"


def test_the_session_cookie_is_confined_to_the_admin_path(_at_admin):
    """On a domain shared with the public site, the admin session simply
    is not sent with a request for a published page."""
    assert dashboard._cookie_path() == "/admin"


def test_the_session_cookie_covers_the_site_when_there_is_no_base_path():
    assert dashboard._cookie_path() == "/"


@pytest.mark.parametrize("headers, scheme, expected", [
    ({"x-forwarded-proto": "https"}, "http", True),
    ({"x-forwarded-proto": "https, http"}, "http", True),
    ({"x-forwarded-proto": "http"}, "http", False),
    ({}, "https", True),
    ({}, "http", False),
])
def test_https_is_read_from_the_proxy_not_the_socket(headers, scheme, expected):
    """Caddy terminates TLS and forwards plain HTTP to loopback, so this
    process sees "http" on a site that is HTTPS-only. Trusting the socket
    would leave the session cookie without Secure on exactly the
    deployment that needs it."""
    class _Req:
        def __init__(self):
            self.headers = headers
            self.url = type("U", (), {"scheme": scheme})()

    assert dashboard._served_over_https(_Req()) is expected


def test_the_served_app_is_the_app_itself_at_the_root():
    assert dashboard.serving_app() is dashboard.app


def test_the_served_app_is_mounted_under_a_base_path(_at_admin):
    served = dashboard.serving_app()

    assert served is not dashboard.app
    assert [r.path for r in served.routes] == ["/admin"]


def test_the_login_page_under_a_base_path_does_not_redirect_to_itself(_at_admin):
    """The whole flow through the real middleware, because the bug this
    pins down was invisible to every unit above: each piece was right and
    the two were compared in different terms."""
    from fastapi.testclient import TestClient

    dashboard._configure_auth("admin", "a-real-admin-password", must_change=False)
    client = TestClient(dashboard.serving_app(), follow_redirects=False)

    assert client.get("/admin").status_code in (307, 308)
    assert client.get("/admin").headers["location"].endswith("/admin/")
    # Unauthenticated, the app's own root sends you to its own login page...
    assert client.get("/admin/").headers["location"] == "/admin/login"
    # ...which serves, rather than sending you back to itself.
    assert client.get("/admin/login").status_code == 200
    # And nothing answers outside the mount: that is the public site's.
    assert client.get("/login").status_code == 404
    assert client.get("/").status_code == 404


def test_logging_in_under_a_base_path_sets_a_cookie_scoped_to_it(_at_admin):
    from fastapi.testclient import TestClient

    dashboard._configure_auth("admin", "a-real-admin-password", must_change=False)
    client = TestClient(dashboard.serving_app(), follow_redirects=False)

    bad = client.post("/admin/api/login", json={"username": "admin", "password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/admin/api/login",
                     json={"username": "admin", "password": "a-real-admin-password"})
    assert ok.status_code == 200
    assert 'Path=/admin' in ok.headers["set-cookie"]
    assert "HttpOnly" in ok.headers["set-cookie"]
    # The session now opens the app's own pages.
    assert client.get("/admin/").status_code == 200


# ---------------------------------------------------------------------
# Pushing the review work from the admin tool
# ---------------------------------------------------------------------

def test_the_push_endpoint_reports_a_refusal_as_a_conflict(monkeypatch):
    """A remote that has moved on is an ordinary thing to run into, not a
    server fault: the page has to be able to say what happened and stay
    usable, which a 500 does not."""
    from fastapi.testclient import TestClient
    from corpus.review import sync

    dashboard._DASHBOARD_USERNAME = None
    monkeypatch.setattr(corpus.review.sync, "push",
                        lambda *a, **k: (_ for _ in ()).throw(sync.SyncError("the remote has 2 commits")))
    client = TestClient(dashboard.app)

    res = client.post("/api/sync/push", json={"message": None})

    assert res.status_code == 409
    assert "2 commits" in res.json()["detail"]


def test_the_push_endpoint_uses_a_dated_message_when_given_none(monkeypatch):
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    seen = {}
    monkeypatch.setattr(corpus.review.sync, "push",
                        lambda repo, message: seen.setdefault("message", message) and None
                        or {"pushed": True, "committed": True, "message": "ok", "status": {}})
    client = TestClient(dashboard.app)

    client.post("/api/sync/push", json={"message": "   "})

    assert seen["message"].startswith("Review progress, ")


def test_the_push_endpoint_keeps_a_message_the_reviewer_typed(monkeypatch):
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    seen = {}
    monkeypatch.setattr(corpus.review.sync, "push",
                        lambda repo, message: seen.setdefault("message", message) and None
                        or {"pushed": True, "committed": True, "message": "ok", "status": {}})
    client = TestClient(dashboard.app)

    client.post("/api/sync/push", json={"message": "Reviewed CPA Chapter 2"})

    assert seen["message"] == "Reviewed CPA Chapter 2"


def test_the_sync_endpoints_are_behind_the_login(_at_admin):
    """They commit, push, pull, discard and restart. Anyone who can reach
    them without a session can write to the repository."""
    from fastapi.testclient import TestClient

    dashboard._configure_auth("admin", "a-real-admin-password", must_change=False)
    client = TestClient(dashboard.serving_app(), follow_redirects=False)

    assert client.get("/admin/api/sync/status").status_code == 401
    for path in ("push", "pull", "commit", "discard", "restart"):
        assert client.post(f"/admin/api/sync/{path}", json={}).status_code == 401, path
    assert client.post("/admin/api/site/rebuild").status_code == 401
    assert client.get("/admin/api/site/progress").status_code == 401


def test_the_pull_endpoint_reports_a_refusal_as_a_conflict(monkeypatch):
    from fastapi.testclient import TestClient
    from corpus.review import sync

    dashboard._DASHBOARD_USERNAME = None
    monkeypatch.setattr(corpus.review.sync, "pull",
                        lambda *a, **k: (_ for _ in ()).throw(sync.SyncError("3 uncommitted change(s)")))
    client = TestClient(dashboard.app)

    res = client.post("/api/sync/pull", json={})

    assert res.status_code == 409
    assert "uncommitted" in res.json()["detail"]


def test_discarding_needs_the_word_typed_out(monkeypatch):
    """The one button on the page that destroys work. A click in the
    wrong place must not be able to reach it."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    called = []
    monkeypatch.setattr(corpus.review.sync, "discard", lambda *a, **k: called.append(True) or {})
    client = TestClient(dashboard.app)

    for body in ({}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": "Discard it"}):
        assert client.post("/api/sync/discard", json=body).status_code == 400, body
    assert called == []

    assert client.post("/api/sync/discard", json={"confirm": "  Discard "}).status_code == 200
    assert called == [True]


def test_the_status_says_when_the_running_code_is_stale(monkeypatch):
    """The failure a pull button introduces: git moves the checkout on
    and this process keeps serving what it imported at startup."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    monkeypatch.setattr(corpus.review.sync, "status", lambda repo: {"branch": "main", "error": None})
    monkeypatch.setattr(corpus.review.sync, "head", lambda repo: "b" * 40)
    monkeypatch.setattr(corpus.review.sync, "code_version", lambda repo: "code-b")
    monkeypatch.setattr(dashboard, "_RUNNING_HEAD", "a" * 40)
    monkeypatch.setattr(dashboard, "_RUNNING_CODE", "code-a")
    client = TestClient(dashboard.app)

    body = client.get("/api/sync/status").json()

    assert body["code_stale"] is True
    assert body["running_head"] == "a" * 40
    assert body["checkout_head"] == "b" * 40


def test_an_unreadable_head_is_not_reported_as_stale(monkeypatch):
    """Not knowing is not the same as knowing they differ, and a banner
    that cannot be dismissed is worse than no banner."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    monkeypatch.setattr(corpus.review.sync, "status", lambda repo: {"branch": "main", "error": None})
    monkeypatch.setattr(corpus.review.sync, "head", lambda repo: None)
    monkeypatch.setattr(corpus.review.sync, "code_version", lambda repo: None)
    monkeypatch.setattr(dashboard, "_RUNNING_HEAD", "a" * 40)
    monkeypatch.setattr(dashboard, "_RUNNING_CODE", "code-a")
    client = TestClient(dashboard.app)

    assert client.get("/api/sync/status").json()["code_stale"] is False


def test_a_commit_of_review_data_alone_asks_for_no_restart(tmp_path):
    """Push commits data/, and pulling brings other people's: neither
    changes the code, and a restart prompt after each was noise."""
    import subprocess
    from corpus.review import sync

    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "corpus").mkdir(); (tmp_path / "corpus" / "x.py").write_text("a = 1\n")
    (tmp_path / "data").mkdir(); (tmp_path / "data" / "review.jsonl").write_text("{}\n")
    git("add", "-A"); git("commit", "-qm", "one")
    before = sync.code_version(tmp_path)

    (tmp_path / "data" / "review.jsonl").write_text('{"more": 1}\n')
    git("commit", "-qam", "review progress")
    assert sync.code_version(tmp_path) == before

    (tmp_path / "corpus" / "x.py").write_text("a = 2\n")
    git("commit", "-qam", "a fix")
    assert sync.code_version(tmp_path) != before


def test_restarting_is_refused_where_nothing_would_start_it_again(monkeypatch):
    """Exiting a service systemd will not restart leaves the dashboard
    down, which is worse than asking for a command to be typed."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    client = TestClient(dashboard.app)

    res = client.post("/api/sync/restart")

    assert res.status_code == 409
    assert "systemctl restart dashboard" in res.json()["detail"]


def test_a_unit_that_would_not_come_back_is_read_rather_than_assumed(monkeypatch, tmp_path):
    """Measured off the unit file, because INVOCATION_ID only says
    systemd started this -- not that it would start it again."""
    unit = tmp_path / "dashboard.service"
    unit.write_text("[Service]\nRestart=no\n")
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    monkeypatch.setattr(dashboard, "_own_unit_name", lambda: "dashboard.service")
    monkeypatch.setattr(dashboard, "_RESTART_UNIT_DIRS", (str(tmp_path),))

    can, why = dashboard._restart_capability()

    assert can is False
    assert "Restart=no" in why


def test_a_restarting_unit_is_allowed(monkeypatch, tmp_path):
    unit = tmp_path / "dashboard.service"
    unit.write_text("[Service]\nRestart=on-failure\n")
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    monkeypatch.setattr(dashboard, "_own_unit_name", lambda: "dashboard.service")
    monkeypatch.setattr(dashboard, "_RESTART_UNIT_DIRS", (str(tmp_path),))

    assert dashboard._restart_capability() == (True, None)


def test_a_drop_in_overriding_restart_is_read_last(monkeypatch, tmp_path):
    """systemd lets a drop-in override the unit file, and reads them in
    that order. Reading only the unit file would offer a button that
    stops the service for good."""
    (tmp_path / "dashboard.service").write_text("[Service]\nRestart=always\n")
    dropin = tmp_path / "dashboard.service.d"
    dropin.mkdir()
    (dropin / "override.conf").write_text("[Service]\nRestart=no\n")
    monkeypatch.setenv("INVOCATION_ID", "abc123")
    monkeypatch.setattr(dashboard, "_own_unit_name", lambda: "dashboard.service")
    monkeypatch.setattr(dashboard, "_RESTART_UNIT_DIRS", (str(tmp_path),))

    can, _why = dashboard._restart_capability()

    assert can is False


# ---------------------------------------------------------------------
# Uploading and associating documents from the dashboard
# ---------------------------------------------------------------------

def test_the_offered_profiles_leave_out_the_template():
    """TEMPLATE.yaml is the documented blank to copy, not something any
    document is parsed with -- offering it invites a parse against an
    empty profile, which is worse than no profile because it looks
    deliberate."""
    from corpus.domain.profiles import available_profiles

    names = available_profiles()

    assert "TEMPLATE" not in names
    assert "criminal-procedure-act" in names


def test_the_profiles_endpoint_answers_with_them(monkeypatch):
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    client = TestClient(dashboard.app)

    assert "criminal-procedure-act" in client.get("/api/profiles").json()["profiles"]


def _link_file(dir_path, name, **fields):
    (dir_path / name).write_text(json.dumps({**fields, "links": []}), encoding="utf-8")


def test_removing_an_association_takes_both_of_its_files(tmp_path, monkeypatch):
    """A Bill's link to its Act and its link to its EM are written as
    separate documents, so forgetting one and keeping the other leaves a
    half-association -- which reads as a real one everywhere that looks."""
    from fastapi.testclient import TestClient

    links = tmp_path / "data" / "bill_links"
    links.mkdir(parents=True)
    _link_file(links, "bill-to-act.json", bill_slug="a-bill", act_slug="an-act")
    _link_file(links, "bill-em.json", bill_slug="a-bill", em_slug="an-em")
    _link_file(links, "other.json", bill_slug="other-bill", act_slug="other-act")
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    dashboard._DASHBOARD_USERNAME = None
    client = TestClient(dashboard.app)

    res = client.post("/api/bill-link/remove", data={"bill_slug": "a-bill"})

    assert res.status_code == 200
    assert sorted(res.json()["removed"]) == ["bill-em.json", "bill-to-act.json"]
    assert [p.name for p in links.glob("*.json")] == ["other.json"], "another Bill's is untouched"


def test_removing_an_association_that_is_not_there_says_so(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    (tmp_path / "data" / "bill_links").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    dashboard._DASHBOARD_USERNAME = None
    client = TestClient(dashboard.app)

    assert client.post("/api/bill-link/remove", data={"bill_slug": "never-linked"}).status_code == 404


def test_removing_an_association_validates_the_slug(tmp_path, monkeypatch):
    """It reaches the filesystem, so the name has to be a slug and not a
    path."""
    from fastapi.testclient import TestClient

    (tmp_path / "data" / "bill_links").mkdir(parents=True)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    dashboard._DASHBOARD_USERNAME = None
    client = TestClient(dashboard.app)

    assert client.post("/api/bill-link/remove", data={"bill_slug": "../../etc"}).status_code == 400


# ---------------------------------------------------------------------
# An Explanatory Memorandum's own name
# ---------------------------------------------------------------------
# An EM's front matter names the Bill it is about, so the title read off
# it is the Bill's. Two links on an Act's contents page both read
# "Criminal Procedure Bill 2008", and only a sentence after each one said
# which was which.

def test_an_em_is_named_as_one():
    assert dashboard._named_as_an_em.__doc__, "the reason is worth keeping"


@pytest.mark.parametrize("kind, title, expected", [
    ("em", "Criminal Procedure Bill 2008",
     "Criminal Procedure Bill 2008 — Explanatory Memorandum"),
    # A Bill and an Act are already named for what they are.
    ("bill", "Criminal Procedure Bill 2008", "Criminal Procedure Bill 2008"),
    ("act", "Criminal Procedure Act 2009", "Criminal Procedure Act 2009"),
    # Said once, however many times the title is asked for -- this is
    # cached and rendered into pages, so appending twice would stick.
    ("em", "Criminal Procedure Bill 2008 — Explanatory Memorandum",
     "Criminal Procedure Bill 2008 — Explanatory Memorandum"),
])
def test_only_an_em_is_told_it_is_one(monkeypatch, kind, title, expected):
    monkeypatch.setattr(dashboard, "_document_kind", lambda slug: kind)

    assert dashboard._named_as_an_em("a-slug", title) == expected


def test_a_title_built_from_the_slug_says_it_too(monkeypatch):
    """The fallback always did this; it was the path that only ran when a
    parse recorded no title at all, which is to say almost never."""
    assert dashboard._title_from_slug("criminal-procedure-bill-2008-em") == (
        "Criminal Procedure Bill 2008 — Explanatory Memorandum"
    )
    assert dashboard._title_from_slug("criminal-procedure-bill-2008") == "Criminal Procedure Bill 2008"


def test_the_real_em_carries_it_end_to_end():
    """Against the corpus, since the point is what the parse actually
    recorded rather than what a fixture says it did."""
    assert dashboard._act_title("criminal-procedure-bill-2008-em") == (
        "Criminal Procedure Bill 2008 — Explanatory Memorandum"
    )
    assert dashboard._act_title("criminal-procedure-bill-2008") == "Criminal Procedure Bill 2008"


# ---------------------------------------------------------------------
# Rebuilding the published site
# ---------------------------------------------------------------------

class _FakeProc:
    def __init__(self, code=None):
        self._code = code
        self.returncode = code

    def poll(self):
        return self._code


def test_rebuilding_runs_the_export_script(monkeypatch):
    """The whole point of the button: nothing about adding an Act reaches
    the public site until this script runs over the current data."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    dashboard._site_build.clear()
    seen = {}
    monkeypatch.setattr(dashboard.subprocess, "Popen",
                        lambda cmd, **kw: seen.setdefault("cmd", cmd) and None or _FakeProc())
    client = TestClient(dashboard.app)

    assert client.post("/api/site/rebuild").status_code == 200

    assert seen["cmd"][1:] == ["-m", "corpus.exporters.export_static_site", "--out", "_site"]
    # No --password and no --no-password: the script takes the passphrase
    # from deploy/site.env and refuses to replace a gated build with an
    # open one, so this button cannot be the thing that unpublishes the
    # gate.
    assert "--no-password" not in seen["cmd"]
    dashboard._site_build.clear()


def test_a_second_rebuild_is_refused_while_one_is_running(monkeypatch):
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    dashboard._site_build.clear()
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    client = TestClient(dashboard.app)

    assert client.post("/api/site/rebuild").status_code == 200
    assert client.post("/api/site/rebuild").status_code == 409
    dashboard._site_build.clear()


def test_the_progress_endpoint_hands_back_what_the_build_said(monkeypatch, tmp_path):
    """A build that refuses to run says why on its own stdout and nowhere
    else -- most likely that it will not replace a gated site with an
    open one."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    log = tmp_path / "site_build.log"
    log.write_text("Refusing to rebuild _site/ without a passphrase")
    monkeypatch.setattr(dashboard, "_SITE_BUILD_LOG", log)
    dashboard._site_build.clear()
    dashboard._site_build.update({"proc": _FakeProc(1), "started": "2026-09-16T00:00:00Z"})
    client = TestClient(dashboard.app)

    body = client.get("/api/site/progress").json()

    assert body["running"] is False
    assert body["exit_code"] == 1
    assert "without a passphrase" in body["log"]
    dashboard._site_build.clear()


# ---------------------------------------------------------------------
# Who a request is from, for the lockout to count
# ---------------------------------------------------------------------


def _request_from(peer, forwarded=None):
    """A stand-in for the one thing _client_ip reads off a request."""
    from starlette.datastructures import Headers

    class _Client:
        host = peer

    class _Request:
        client = _Client() if peer else None
        headers = Headers({"x-forwarded-for": forwarded} if forwarded else {})

    return _Request()


def test_the_forwarded_address_is_read_when_the_peer_is_the_proxy():
    """Caddy terminates TLS and proxies over loopback, so the peer is
    always 127.0.0.1. Counting that made the login lockout global: five
    wrong guesses from anyone locked out everyone."""
    assert dashboard._client_ip(_request_from("127.0.0.1", "203.0.113.7")) == "203.0.113.7"
    assert dashboard._client_ip(_request_from("::1", "203.0.113.7")) == "203.0.113.7"


def test_the_last_forwarded_hop_is_the_one_believed():
    """A client can seed X-Forwarded-For with anything. Each proxy
    appends the peer it actually saw, so the last entry is the one the
    proxy in front of us added and the only one not under the client's
    control."""
    forged = "1.2.3.4, 5.6.7.8"  # what a client sent, plus what Caddy appended
    assert dashboard._client_ip(_request_from("127.0.0.1", forged)) == "5.6.7.8"


def test_a_forwarded_header_from_a_stranger_is_ignored():
    """Read only when the peer really is the proxy. Anywhere else the
    header is just something somebody sent, and believing it would let
    anyone pick which address their failures are counted against."""
    assert dashboard._client_ip(_request_from("198.51.100.9", "127.0.0.1")) == "198.51.100.9"


def test_a_peerless_request_still_has_an_answer():
    assert dashboard._client_ip(_request_from(None)) == "unknown"


def test_two_callers_behind_the_proxy_do_not_lock_each_other_out():
    """The whole point of the fix: one person guessing wrong must not be
    able to lock the door on everybody else."""
    dashboard._FAILED_ATTEMPTS.clear()
    guesser = _request_from("127.0.0.1", "203.0.113.7")
    for _ in range(dashboard._LOCKOUT_THRESHOLD):
        dashboard._record_failed_login(dashboard._client_ip(guesser))

    assert dashboard._is_locked_out(dashboard._client_ip(guesser)) is True
    everyone_else = _request_from("127.0.0.1", "198.51.100.4")
    assert dashboard._is_locked_out(dashboard._client_ip(everyone_else)) is False
    dashboard._FAILED_ATTEMPTS.clear()


# ---------------------------------------------------------------------
# Choosing what is on the public site
# ---------------------------------------------------------------------


def _dashboard_at(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    dashboard._DASHBOARD_USERNAME = None
    return TestClient(dashboard.app)


def test_publishing_a_work_from_the_dashboard(tmp_path, monkeypatch):
    client = _dashboard_at(tmp_path, monkeypatch)

    res = client.post("/api/publication", json={"work": "crimes-act", "published": True})

    assert res.status_code == 200
    assert res.json()["published_works"] == ["crimes-act"]
    assert corpus.storage.db.published_works(tmp_path) == {"crimes-act"}


def test_taking_a_work_off_the_public_site(tmp_path, monkeypatch):
    client = _dashboard_at(tmp_path, monkeypatch)
    client.post("/api/publication", json={"work": "crimes-act", "published": True})

    res = client.post("/api/publication", json={"work": "crimes-act", "published": False})

    assert res.json()["published_works"] == []
    assert corpus.storage.db.published_works(tmp_path) == set()


def test_a_publication_request_has_to_name_a_work(tmp_path, monkeypatch):
    client = _dashboard_at(tmp_path, monkeypatch)
    assert client.post("/api/publication", json={"work": "   ", "published": True}).status_code == 400


def test_the_decision_covers_every_reprint_of_a_work(tmp_path, monkeypatch):
    """Published per work, so all five Criminal Procedure Act reprints
    answer the same -- there is no state in which the newest is down and
    an older one is still up."""
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    for version in (110, 114):
        (tmp_path / "data" / "parsed" / f"criminal-procedure-act-v{version}.json").write_text(
            "{}", encoding="utf-8")
    corpus.storage.db.set_publication("criminal-procedure-act", True, tmp_path)

    publication = corpus.storage.db.load_publication(tmp_path)
    for version in (110, 114):
        assert dashboard.act_status(f"criminal-procedure-act-v{version}", publication)["published"] is True


def test_seeding_records_what_was_already_being_served(tmp_path, monkeypatch):
    """Everything parsed used to be on the site. The table arriving must
    not take all of it down -- that is not a decision anybody made."""
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "crimes-act.json").write_text("{}", encoding="utf-8")
    (tmp_path / "data" / "parsed" / "criminal-procedure-act-v114.json").write_text("{}", encoding="utf-8")

    dashboard._seed_publication_if_new()

    assert corpus.storage.db.published_works(tmp_path) == {"crimes-act", "criminal-procedure-act"}


def test_seeding_leaves_a_later_document_off(tmp_path, monkeypatch):
    """Once the table has been written, a newly parsed work starts off
    the site until somebody says otherwise -- which is the whole point of
    asking for the control."""
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "crimes-act.json").write_text("{}", encoding="utf-8")
    dashboard._seed_publication_if_new()

    (tmp_path / "data" / "parsed" / "evidence-act.json").write_text("{}", encoding="utf-8")
    dashboard._seed_publication_if_new()

    assert corpus.storage.db.published_works(tmp_path) == {"crimes-act"}
    assert dashboard.act_status("evidence-act")["published"] is False


# ---------------------------------------------------------------------
# The search index
# ---------------------------------------------------------------------


def test_the_index_status_says_there_is_none_yet(tmp_path, monkeypatch):
    client = _dashboard_at(tmp_path, monkeypatch)

    body = client.get("/api/search/status").json()

    assert body["built"] is False
    assert body["running"] is False


def test_publishing_a_work_starts_a_rebuild(tmp_path, monkeypatch):
    """The index holds what the site serves, so a publication change has
    just changed it. Left alone, search would answer about a corpus that
    no longer matches the site."""
    client = _dashboard_at(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(dashboard, "_rebuild_search_index_soon", lambda: started.append(True))

    client.post("/api/publication", json={"work": "crimes-act", "published": True})

    assert started == [True]


def test_a_failed_rebuild_is_reported_rather_than_swallowed(tmp_path, monkeypatch):
    client = _dashboard_at(tmp_path, monkeypatch)
    monkeypatch.setattr(corpus.search.search, "rebuild",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")))
    dashboard._search_state.update({"running": False, "error": None, "stats": None})

    res = client.post("/api/search/rebuild")

    assert res.status_code == 500
    assert "no space left" in res.json()["detail"]
    dashboard._search_state.update({"error": None})


def test_an_index_built_by_an_older_builder_counts_as_stale(tmp_path, monkeypatch):
    """Even when the data has not moved a byte.

    The index gained a table the query layer reads; an older one still
    answers queries, just without typo correction. A feature that is
    quietly absent is worse than one that is visibly broken, so the
    schema stamp is compared alongside the data signature."""
    client = _dashboard_at(tmp_path, monkeypatch)
    corpus.search.search.rebuild(tmp_path, source=dashboard)
    assert client.get("/api/search/status").json()["stale"] is False

    conn = sqlite3.connect(str(corpus.search.search.index_path(tmp_path)))
    with conn:
        conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    conn.close()

    assert client.get("/api/search/status").json()["stale"] is True


def test_the_index_endpoints_are_behind_the_login(_at_admin):
    from fastapi.testclient import TestClient

    dashboard._configure_auth("admin", "a-real-admin-password", must_change=False)
    client = TestClient(dashboard.serving_app(), follow_redirects=False)

    assert client.get("/admin/api/search/status").status_code == 401
    assert client.post("/admin/api/search/rebuild").status_code == 401
    assert client.post("/admin/api/publication", json={}).status_code == 401


# ---------------------------------------------------------------------
# Restarting the public site
# ---------------------------------------------------------------------
#
# A second process on the same box. The dashboard restarts it by sending
# it SIGTERM -- both units run as the same user, so that needs no
# privilege -- and letting systemd's Restart= bring it back. Which is
# what the first version of this got wrong: it went through sudo, and
# deploy/dashboard.service sets NoNewPrivileges=yes, under which sudo
# cannot become root no matter what sudoers says.
#
# None of these states exist on the machine the tests run on, so
# systemd's answers are stood in for. What is being tested is what the
# dashboard makes of them, which is the part that can be wrong.


def _unit(monkeypatch, booted=True, **properties):
    """Stands in for systemd's answers about the public unit."""
    monkeypatch.setattr(dashboard, "_systemd_is_running", lambda: booted)
    values = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running",
              "ActiveEnterTimestamp": "Tue 2026-09-16 09:00:00 AEST",
              "MainPID": "4242", "Restart": "always", "User": "dashboard"}
    values.update({k: str(v) for k, v in properties.items()})
    monkeypatch.setattr(dashboard, "_unit_properties", lambda *a, **k: dict(values))


def test_no_systemd_means_nothing_to_say(tmp_path, monkeypatch):
    """A laptop running this to review documents should not be told that
    a service it never installed is in trouble."""
    client = _dashboard_at(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "_systemd_is_running", lambda: False)

    body = client.get("/api/service/public").json()

    assert body["systemd"] is False
    assert body["can_restart"] is False


def test_a_running_public_site_is_reported_as_running(tmp_path, monkeypatch):
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch)
    monkeypatch.setattr(dashboard, "_process_is_ours", lambda pid: None)

    body = client.get("/api/service/public").json()

    assert body["installed"] is True and body["active"] is True
    assert body["since"].startswith("Tue")
    assert body["can_restart"] is True


def test_a_unit_that_was_never_installed_is_not_a_failure(tmp_path, monkeypatch):
    """Different from stopped, and the page hides the strip on it rather
    than reporting a problem nobody has."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch, LoadState="not-found", ActiveState="inactive")

    body = client.get("/api/service/public").json()

    assert body["known"] is True and body["installed"] is False


# --- the guards on signalling, which is where this could do damage -----


def test_a_process_belonging_to_somebody_else_is_not_signalled(monkeypatch):
    """Both services usually run as the same user. Where they do not,
    this must not try -- and must say which uid it saw."""
    monkeypatch.setattr(dashboard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(dashboard.os, "stat", lambda path: type("S", (), {"st_uid": 0})())

    why = dashboard._process_is_ours(4242)

    assert why and "uid 0" in why and "uid 1000" in why


def test_a_process_that_is_not_the_public_site_is_not_signalled(monkeypatch, tmp_path):
    """pids are reused, and systemd's MainPID is read over a socket a
    moment before it is acted on. Signalling the wrong process is the one
    way this can do real damage, so the command line is checked too."""
    monkeypatch.setattr(dashboard.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(dashboard.os, "stat", lambda path: type("S", (), {"st_uid": 1000})())
    monkeypatch.setattr(dashboard.Path, "read_bytes", lambda self: b"/usr/bin/postgres\x00")

    why = dashboard._process_is_ours(4242)

    assert why and "does not look like the public site" in why


def test_a_process_that_is_gone_is_not_signalled(monkeypatch):
    monkeypatch.setattr(dashboard.os, "stat",
                        lambda path: (_ for _ in ()).throw(FileNotFoundError()))

    assert "no process" in dashboard._process_is_ours(4242)


def test_no_main_process_is_not_signalled():
    assert "no main process" in dashboard._process_is_ours(0)


def test_a_unit_systemd_would_not_bring_back_is_not_signalled(tmp_path, monkeypatch):
    """Sending SIGTERM to a unit with Restart=no is how you take the
    public site down and leave it down."""
    _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch, Restart="no")

    method, why = dashboard._restart_method()

    assert method == "systemctl"
    assert "Restart=no" in why


# --- restarting ---------------------------------------------------------


def test_a_restart_signals_the_process_and_waits_for_a_new_one(tmp_path, monkeypatch):
    """No sudo, no systemctl: same user, so a signal is enough, and
    systemd's Restart= does the rest."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch)
    monkeypatch.setattr(dashboard, "_process_is_ours", lambda pid: None)
    signalled = []
    monkeypatch.setattr(dashboard.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(dashboard, "_wait_for_restart",
                        lambda was, seconds=8.0: {"known": True, "active": True,
                                                  "main_pid": 5555, "state": "active"})
    used_systemctl = []
    monkeypatch.setattr(dashboard, "_systemctl", lambda *a, **k: used_systemctl.append(a))

    res = client.post("/api/service/public/restart")

    assert res.status_code == 200
    assert signalled == [(4242, dashboard.signal.SIGTERM)]
    assert used_systemctl == []
    assert "5555" in res.json()["message"]


def test_a_restart_where_the_process_did_not_change_is_not_a_success(tmp_path, monkeypatch):
    """Having sent a signal is not evidence that anything came back. A
    page saying "restarted" over a site that is down is the kind of
    reassurance that costs an hour to see through."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch)
    monkeypatch.setattr(dashboard, "_process_is_ours", lambda pid: None)
    monkeypatch.setattr(dashboard.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(dashboard, "_wait_for_restart",
                        lambda was, seconds=8.0: {"known": True, "active": False,
                                                  "main_pid": 4242, "state": "failed",
                                                  "sub_state": "failed"})

    class _Refused:
        returncode = 1
        stdout = ""
        stderr = "Failed to restart: Access denied"

    monkeypatch.setattr(dashboard, "_systemctl", lambda *a, **k: _Refused())

    res = client.post("/api/service/public/restart")

    assert res.status_code == 500
    assert "journalctl" in res.json()["detail"]


def test_what_systemctl_actually_said_reaches_the_page(tmp_path, monkeypatch):
    """Verbatim. That string names the real cause -- including the
    NoNewPrivileges one -- and summarising it is how the real cause got
    lost the first time."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch, ActiveState="failed", SubState="failed", MainPID="0")

    class _Refused:
        returncode = 1
        stdout = ""
        stderr = ("sudo: The \"no new privileges\" flag is set, which prevents sudo "
                  "from running as root.")

    monkeypatch.setattr(dashboard, "_systemctl", lambda *a, **k: _Refused())

    res = client.post("/api/service/public/restart")

    assert res.status_code == 500
    assert "no new privileges" in res.json()["detail"]


def test_a_restart_is_attempted_even_when_it_looked_impossible(tmp_path, monkeypatch):
    """The defect this replaces: the old version decided in advance that
    it could not, on a `sudo -n -l` probe that exits non-zero for several
    unrelated reasons, and refused with a 409 naming a sudoers line. For
    somebody who had already written that line it was the one message
    that could not help. So the attempt is the measurement."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch)
    monkeypatch.setattr(dashboard, "_restart_method", lambda: (None, "Looks impossible."))
    tried = []

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(dashboard, "_systemctl", lambda *a, **k: tried.append(a) or _Ok())
    monkeypatch.setattr(dashboard, "_wait_for_restart",
                        lambda was, seconds=8.0: {"known": True, "active": True,
                                                  "main_pid": 5555, "state": "active"})

    res = client.post("/api/service/public/restart")

    assert res.status_code == 200
    assert tried, "it should have tried anyway rather than refusing on a guess"


def test_the_two_refusals_that_are_actually_knowable(tmp_path, monkeypatch):
    """No systemd, and a unit that is not installed. Everything else is
    found out by trying."""
    client = _dashboard_at(tmp_path, monkeypatch)

    monkeypatch.setattr(dashboard, "_systemd_is_running", lambda: False)
    assert client.post("/api/service/public/restart").status_code == 409

    _unit(monkeypatch, LoadState="not-found")
    assert client.post("/api/service/public/restart").status_code == 409


# --- the evidence ------------------------------------------------------


def test_the_diagnosis_carries_evidence_rather_than_an_assertion(tmp_path, monkeypatch):
    """A message that reads the same whether or not you have done the
    thing it asks for is a message nobody can act on."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch, User="www-data")
    monkeypatch.setattr(dashboard, "_process_is_ours", lambda pid: "belongs to uid 33")
    monkeypatch.setattr(dashboard, "_sudo_diagnosis",
                        lambda: {"tried": True, "available": False, "exit_code": 1,
                                 "detail": "sudo: a password is required",
                                 "command": "sudo -n -l ..."})

    d = client.get("/api/service/public").json()["diagnosis"]

    assert d["unit_user"] == "www-data"
    assert d["unit_main_pid"] == 4242
    assert d["unit_restart_policy"] == "always"
    assert d["running_uid"] == dashboard.os.geteuid()
    assert d["sudo"]["detail"] == "sudo: a password is required"


def test_no_new_privileges_is_read_from_the_kernel_not_guessed(monkeypatch, tmp_path):
    """deploy/dashboard.service sets it, and under it no sudoers line can
    work. Saying so is the difference between a fixable problem and an
    hour of editing a file that was already right."""
    status = tmp_path / "status"
    status.write_text("Name:\tpython3\nNoNewPrivs:\t1\n", encoding="utf-8")
    real_read = dashboard.Path.read_text

    def read_text(self, *a, **k):
        if str(self) == "/proc/self/status":
            return status.read_text(encoding="utf-8")
        return real_read(self, *a, **k)

    monkeypatch.setattr(dashboard.Path, "read_text", read_text)

    assert dashboard._no_new_privileges() is True


def test_sudo_is_only_ever_evidence_never_a_decision(tmp_path, monkeypatch):
    """It is asked about only when systemctl is the path being taken, and
    what it says goes in the panel rather than into a refusal."""
    client = _dashboard_at(tmp_path, monkeypatch)
    _unit(monkeypatch)
    monkeypatch.setattr(dashboard, "_process_is_ours", lambda pid: None)
    asked = []
    monkeypatch.setattr(dashboard, "_sudo_diagnosis", lambda: asked.append(True) or {})

    body = client.get("/api/service/public").json()

    assert body["can_restart"] is True
    assert asked == [], "signalling works here, so sudo is not even consulted"


def test_the_public_service_endpoints_are_behind_the_login(_at_admin):
    from fastapi.testclient import TestClient

    dashboard._configure_auth("admin", "a-real-admin-password", must_change=False)
    client = TestClient(dashboard.serving_app(), follow_redirects=False)

    assert client.get("/admin/api/service/public").status_code == 401
    assert client.post("/admin/api/service/public/restart").status_code == 401


# ---------------------------------------------------------------------
# Searching from the admin tool -- which it no longer does
# ---------------------------------------------------------------------


def test_the_admin_tool_does_not_serve_a_search_page():
    """Search belongs to the public site. This tool still *builds* the
    index -- it is the process that writes -- but it does not read it,
    and a browse page here carries no search box.

    Removing a route is the kind of change that looks done in a diff and
    is not, so this asks the app."""
    from fastapi.testclient import TestClient

    dashboard._DASHBOARD_USERNAME = None
    client = TestClient(dashboard.app)

    assert client.get("/search?q=indictable").status_code == 404
    assert not hasattr(dashboard, "_search_box_url")


def test_a_browse_page_carries_no_search_box(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    dashboard._DASHBOARD_USERNAME = None
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    monkeypatch.setattr(corpus.publishing.reader, "contents_page", lambda *a, **k: "<p>contents</p>")
    monkeypatch.setattr(dashboard, "_act_title", lambda slug: "Test Act")
    monkeypatch.setattr(dashboard, "related_documents", lambda slug: [])
    (tmp_path / "data" / "parsed" / "test-act.json").write_text("{}", encoding="utf-8")
    client = TestClient(dashboard.app)

    body = client.get("/browse/test-act/").text

    assert "contents" in body
    assert "sitesearch" not in body


# The endpoint's own reading of which mode was asked for. Worth pinning
# separately from the command it builds: the 409 is the last thing that
# describes the change before it happens, and describing the wrong one is
# how somebody confirms something they did not mean.

def _reparse(monkeypatch, tmp_path, **form):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    (tmp_path / "acts").mkdir(exist_ok=True)
    (tmp_path / "acts" / "demo-act.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(dashboard, "act_status",
                        lambda slug: {"parsed": True, "reviewed_units": 7, "unit_count": 9})
    return TestClient(dashboard.app).post("/api/acts/demo-act/reparse", data=form)


def test_keeping_approved_work_still_asks_before_it_runs(monkeypatch, tmp_path):
    """It changes less than the other two, but it still rewrites every
    unapproved row."""
    res = _reparse(monkeypatch, tmp_path, mode="keep")

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "keeps its text and its tick" in detail
    assert "re-read from the new parse" in detail


def test_the_old_discard_flag_still_means_discard(monkeypatch, tmp_path):
    """Sent by anything that predates the three-way choice."""
    res = _reparse(monkeypatch, tmp_path, discard="true")

    assert res.status_code == 409
    assert "cannot be undone" in res.json()["detail"]


def test_asking_for_a_mode_that_does_not_exist_is_refused(monkeypatch, tmp_path):
    """Rather than quietly falling back to one of the real ones."""
    res = _reparse(monkeypatch, tmp_path, mode="keep-everything-forever")

    assert res.status_code == 400


def test_every_admin_page_finds_its_stylesheets(monkeypatch):
    """The admin pages link their CSS by relative path (static/admin/), so
    each has to resolve from wherever the page is served."""
    import re
    from urllib.parse import urljoin
    from fastapi.testclient import TestClient
    from corpus.review import review

    monkeypatch.setattr(dashboard, "_DASHBOARD_USERNAME", None)
    pages = [(dashboard.app, "/"), (dashboard.app, "/history/act/"), (dashboard.app, "/teaching/act/"),
             (dashboard.app, "/lessons/"), (review.app, "/")]
    for app, url in pages:
        client = TestClient(app)
        html = client.get(url).text
        hrefs = re.findall(r'<link rel="stylesheet" href="([^"]+)"', html)
        assert hrefs, url
        for href in hrefs:
            css = client.get(urljoin(f"http://testserver{url}", href))
            assert css.status_code == 200 and "text/css" in css.headers["content-type"], (url, href)


def test_the_document_list_is_cached_until_a_parse_changes(tmp_path, monkeypatch):
    """The list read every parse (megabytes each) on every load; it keeps
    what it needs against each file's size and mtime instead, on disk so
    a restart doesn't start cold -- and must still see a re-parse."""
    import json
    import os

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_list_cache", None)
    parsed = tmp_path / "data" / "parsed" / "act.json"
    parsed.parent.mkdir(parents=True)
    parsed.write_text(json.dumps({"nodes": [make_node("section", "1", "One", "x")], "document_type": "act"}))

    assert [d["node_count"] for d in dashboard.list_acts()] == [1]
    assert (tmp_path / "data" / ".cache" / "dashboard-list.json").exists()

    parsed.write_text(json.dumps({"nodes": [make_node("section", "1", "One", "x"),
                                            make_node("section", "2", "Two", "y")], "document_type": "bill"}))
    os.utime(parsed, ns=(1, 10**18))   # a re-parse: a new mtime
    monkeypatch.setattr(dashboard, "_list_cache", None)   # and a restart in between
    [doc] = dashboard.list_acts()
    assert (doc["node_count"], doc["unit_count"], doc["kind"]) == (2, 2, "bill")
