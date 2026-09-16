"""Tests for public.py -- the read-only public site.

Two of these are worth more than the rest put together, and both work by
enumerating the app's own route table rather than by listing what to
check: one asserts every route is a GET, and one asserts every route is
behind the gate. A route added later cannot quietly opt out of either,
which is the whole reason this is a separate application from the
dashboard rather than a prefix inside it.
"""
import pytest
from fastapi.testclient import TestClient

import public

PASSPHRASE = "a real site passphrase"


@pytest.fixture(autouse=True)
def gated_site(monkeypatch):
    """Every test starts behind the passphrase, because that is how it
    runs."""
    public.configure(PASSPHRASE)
    public._FAILED.clear()
    yield
    public._FAILED.clear()


@pytest.fixture
def client():
    return TestClient(public.app, follow_redirects=False)


@pytest.fixture
def unlocked(client):
    res = client.post("/api/login", json={"passphrase": PASSPHRASE})
    assert res.status_code == 200
    return client


def _route_paths():
    for route in public.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            yield path, methods


# ---------------------------------------------------------------------
# The two guarantees
# ---------------------------------------------------------------------


def test_the_public_site_writes_nothing():
    """Everything is a GET except the one form post that checks the
    passphrase. This is the test that makes "read-only" something you can
    read off the route table instead of having to audit for."""
    writes = {path: sorted(methods - {"GET", "HEAD", "OPTIONS"})
              for path, methods in _route_paths()
              if methods - {"GET", "HEAD", "OPTIONS"}}

    assert writes == {"/api/login": ["POST"]}


def test_every_route_is_behind_the_gate(client):
    """Enumerated rather than listed, so a route added later cannot
    forget. /api/browse/.../preview is the one that matters most: it
    returns provision text, and it is exactly the kind of route a
    hand-written exemption list leaves out."""
    exempt = public._OPEN_PATHS | {"/assets"}
    for path, methods in _route_paths():
        if path in exempt or path.startswith("/assets"):
            continue
        address = (path.replace("{site_slug}", "crimes-act")
                       .replace("{section_slug}", "s1"))
        res = client.request("POST" if "POST" in methods else "GET", address)
        assert res.status_code in (303, 307, 401), f"{address} answered {res.status_code}"


# ---------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------


def test_the_wrong_passphrase_is_refused(client):
    assert client.post("/api/login", json={"passphrase": "not it"}).status_code == 401


def test_the_right_passphrase_opens_the_site(client):
    res = client.post("/api/login", json={"passphrase": PASSPHRASE})

    assert res.status_code == 200
    assert public.COOKIE_NAME in res.cookies


def test_a_forged_cookie_is_refused():
    """The session is a signed expiry rather than a row in a dict, so the
    signature is the whole of the check."""
    assert public._cookie_is_valid("bm90aGluZw.deadbeef") is False
    assert public._cookie_is_valid("not-even-shaped-right") is False
    assert public._cookie_is_valid(None) is False


def test_an_expired_cookie_is_refused(monkeypatch):
    cookie = public._issue_cookie()
    assert public._cookie_is_valid(cookie) is True

    monkeypatch.setattr(public.time, "time",
                        lambda: 9_999_999_999 + public.SESSION_LIFETIME_SECONDS)
    assert public._cookie_is_valid(cookie) is False


def test_changing_the_passphrase_ends_every_session():
    """The signing key is the key derived from the passphrase, so this
    falls out of the design rather than needing a session store to sweep.
    A leaked passphrase goes from a thirty-day problem to a five-second
    one."""
    cookie = public._issue_cookie()
    assert public._cookie_is_valid(cookie) is True

    public.configure("something else entirely")

    assert public._cookie_is_valid(cookie) is False


def test_the_cookie_is_secure_only_where_the_connection_was(client):
    """Caddy terminates TLS and forwards plain HTTP, so this follows the
    forwarded header rather than what this process sees -- and a loopback
    dev run over plain http still has to work."""
    over_https = client.post("/api/login", json={"passphrase": PASSPHRASE},
                             headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in over_https.headers["set-cookie"]

    plain = client.post("/api/login", json={"passphrase": PASSPHRASE})
    assert "Secure" not in plain.headers["set-cookie"]


def test_the_site_refuses_to_start_without_a_passphrase():
    """An open site is a real choice and stays available, but it has to
    be made rather than arrived at by forgetting to set one."""
    with pytest.raises(SystemExit) as excinfo:
        public.configure(None)

    assert "--open" in str(excinfo.value)


def test_an_open_site_is_open(client):
    public.configure(None, open_site=True)

    assert public.gated() is False
    assert client.get("/robots.txt").status_code == 200


def test_two_visitors_do_not_lock_each_other_out():
    """Behind Caddy the peer is loopback for everyone, so counting that
    would let one person lock the door on the whole internet -- the same
    bug the dashboard had.

    The client address is set to loopback deliberately: that is what this
    process sees in production, and it is the condition under which the
    forwarded header is believed at all."""
    behind_caddy = TestClient(public.app, client=("127.0.0.1", 40000))
    for _ in range(public.LOCKOUT_THRESHOLD):
        behind_caddy.post("/api/login", json={"passphrase": "wrong"},
                          headers={"X-Forwarded-For": "203.0.113.7"})

    locked = behind_caddy.post("/api/login", json={"passphrase": PASSPHRASE},
                               headers={"X-Forwarded-For": "203.0.113.7"})
    assert locked.status_code == 429

    everyone_else = behind_caddy.post("/api/login", json={"passphrase": PASSPHRASE},
                                      headers={"X-Forwarded-For": "198.51.100.4"})
    assert everyone_else.status_code == 200


def test_the_forwarded_address_is_only_believed_from_the_proxy():
    """From anywhere else the header is just something a stranger sent,
    and believing it would let anyone choose whose failures they are
    counted as."""
    class _Request:
        def __init__(self, peer, forwarded):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}

    assert public._client_ip(_Request("127.0.0.1", "203.0.113.7")) == "203.0.113.7"
    assert public._client_ip(_Request("127.0.0.1", "1.2.3.4, 5.6.7.8")) == "5.6.7.8"
    assert public._client_ip(_Request("198.51.100.9", "127.0.0.1")) == "198.51.100.9"


def test_the_unlock_page_carries_its_own_styling(client):
    """So that everything else, /assets included, can sit behind the gate
    without the one page a visitor can reach arriving unstyled."""
    body = client.get("/login").text

    assert "<style>" in body
    assert "/assets/" not in body


# ---------------------------------------------------------------------
# What it will serve
# ---------------------------------------------------------------------


def test_an_unpublished_work_is_not_served(unlocked, monkeypatch, tmp_path):
    """Including its hover cards. Without the check there, hovering a
    cross-reference would hand out the full text of a provision the site
    refuses to serve."""
    monkeypatch.setattr(public, "_published_slugs", dict)

    for address in ("/browse/crimes-act/", "/browse/crimes-act/section/s1",
                    "/browse/crimes-act/endnotes",
                    "/api/browse/crimes-act/preview?section=s1"):
        assert unlocked.get(address).status_code == 404, address


def test_robots_asks_crawlers_away_from_a_gated_site(client):
    assert "Disallow: /" in client.get("/robots.txt").text


def test_an_open_site_still_asks_crawlers_away_unless_told_otherwise(client):
    public.configure(None, open_site=True)
    assert "Disallow: /" in client.get("/robots.txt").text

    public.configure(None, open_site=True, allow_indexing=True)
    assert "Disallow: /" not in client.get("/robots.txt").text


# ---------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------


def test_searching_without_an_index_says_so_rather_than_failing(unlocked, monkeypatch):
    """A missing index is an ordinary state on a fresh server, not a
    server fault."""
    class Missing:
        def search(self, *a, **k):
            raise public.search.SearchUnavailable("The search index hasn't been built yet.")

    monkeypatch.setattr(public, "_INDEX", Missing())

    page = unlocked.get("/search?q=anything")
    assert page.status_code == 200
    assert "hasn&#x27;t been built" in page.text or "hasn't been built" in page.text

    assert unlocked.get("/api/search?q=anything").status_code == 503


def test_the_search_page_works_without_javascript(unlocked, monkeypatch):
    """A plain GET form. For a reference work about the law that is worth
    more than a type-ahead."""
    monkeypatch.setattr(public, "_INDEX", _StubIndex())

    body = unlocked.get("/search?q=indictable").text

    assert "<form" in body and 'method="get"' in body.replace("'", '"')
    assert "Committal proceeding" in body


def test_a_search_result_links_to_the_provision(unlocked, monkeypatch):
    monkeypatch.setattr(public, "_INDEX", _StubIndex())

    body = unlocked.get("/search?q=indictable").text

    assert "/browse/criminal-procedure-act/section/s242#s242-1" in body


def test_the_superseded_switch_is_carried_through(unlocked, monkeypatch):
    stub = _StubIndex()
    monkeypatch.setattr(public, "_INDEX", stub)

    unlocked.get("/search?q=indictable&superseded=1")

    assert stub.asked[-1]["include_superseded"] is True


def test_what_somebody_typed_is_escaped_back_into_the_box(unlocked, monkeypatch):
    monkeypatch.setattr(public, "_INDEX", _StubIndex())

    body = unlocked.get("/search?q=%3Cscript%3Ealert(1)%3C/script%3E").text

    assert "<script>alert(1)" not in body


class _StubIndex:
    """One result, in the shape corpus/search.py returns."""

    def __init__(self):
        self.asked = []

    def search(self, raw, include_superseded=False, limit=20, offset=0):
        self.asked.append({"raw": raw, "include_superseded": include_superseded})
        return {
            "query": raw, "parsed": raw, "total": 1, "truncated": False,
            "results": [{
                "title": "Criminal Procedure Act 2009", "kind": "act",
                "as_at": "1 July 2024", "is_current": True, "version": 114,
                "label": "Section 242 Committal proceeding",
                "breadcrumb": "Chapter 4 › Part 4.9",
                "snippet_html": "an <mark>indictable</mark> offence",
                "slug": "criminal-procedure-act-v114",
                "site_slug": "criminal-procedure-act",
                "page": "s242",
                "fragment": "s242-1",
                "href": "/browse/criminal-procedure-act/section/s242#s242-1",
            }],
        }
