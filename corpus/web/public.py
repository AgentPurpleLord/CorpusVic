"""
The public site: corpusvic.au, served live from the database -- and the
same site written out as static files, the archive (see "The archive"
below).

    python -m corpus.web.public                        serve it
    python -m corpus.web.public build --out _site      write the archive

A passphrase gates either: the live site checks it at a login page; the
archive encrypts every page behind it.
"""
import argparse
import base64
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from corpus.web import dashboard
from corpus.web.page_cache import PageCache
from corpus.search import search
from corpus.search import search_view
from corpus.publishing import html_view, reader, site_env
from corpus import PROJECT_ROOT
from corpus.storage import db
from corpus.domain.hierarchy import group_into_units
from corpus.publishing.site_crypto import ROBOTS_TXT, ROBOTS_TXT_ALLOW_ALL, SiteGate
from corpus.parsing.versions import split_document_slug

# The project, not this module's own folder. data/, deploy/ and the
# published corpus all hang off the root, and counting .parents from a
# module that has since moved into a package is how this came to point at
# corpus/web/ -- where there is no database, so the site served nothing.
BASE_DIR = PROJECT_ROOT

COOKIE_NAME = "corpus_site"
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30
PBKDF2_ITERATIONS = 200_000
LOCKOUT_THRESHOLD = 10
LOCKOUT_WINDOW_SECONDS = 15 * 60

# Set by configure(). None means an open site, which has to be asked for.
_KEY: "bytes | None" = None
_ALLOW_INDEXING = False
_FAILED: dict[str, list[float]] = {}
_INDEX = search.Index(BASE_DIR)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def configure(passphrase: "str | None", open_site: bool = False,
              allow_indexing: bool = False) -> None:
    """Sets the passphrase for this run, once, at startup.

    The plaintext is derived and dropped: what is kept is a key, which is
    both what a submitted passphrase is checked against and what session
    cookies are signed with. That the two are the same key is the point --
    change the passphrase and every outstanding session stops verifying."""
    global _KEY, _ALLOW_INDEXING
    _ALLOW_INDEXING = bool(allow_indexing)
    if open_site:
        _KEY = None
        return
    if not passphrase:
        raise SystemExit(
            "Refusing to serve the corpus to anyone who asks.\n"
            f"  - to put it behind a passphrase: set {site_env.VARIABLE} in "
            f"{site_env.SITE_ENV_FILE} (see deploy/site.env.example)\n"
            "  - to publish it openly, on purpose: pass --open"
        )
    # A salt fixed to the passphrase rather than random, because the key
    # has to come out the same on every restart or every session would
    # be invalidated by an ordinary deploy.
    _KEY = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                               b"corpusvic-site-gate", PBKDF2_ITERATIONS)


def gated() -> bool:
    return _KEY is not None


def _client_ip(request: Request) -> str:
    """Who a request is from. Behind Caddy the peer is always loopback,
    so counting that would make the lockout global -- see dashboard.py's
    own copy, which had exactly that bug."""
    peer = request.client.host if request.client else None
    if peer in {"127.0.0.1", "::1"}:
        nearest = (request.headers.get("x-forwarded-for") or "").rsplit(",", 1)[-1].strip()
        if nearest:
            return nearest
    return peer or "unknown"


def _locked_out(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _FAILED.get(ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    _FAILED[ip] = recent
    return len(recent) >= LOCKOUT_THRESHOLD


def _record_failure(ip: str) -> None:
    _FAILED.setdefault(ip, []).append(time.time())


def _sign(payload: str) -> str:
    return hmac.new(_KEY, payload.encode("ascii"), hashlib.sha256).hexdigest()


def _issue_cookie() -> str:
    """A session as a signed expiry, rather than a row in a dict.

    Stateless, so it survives a restart and cannot grow without bound --
    both of which matter more here than on a single-admin tool. And
    signed with the key derived from the passphrase, so rotating the
    passphrase ends every session that exists."""
    expiry = str(int(time.time() + SESSION_LIFETIME_SECONDS))
    encoded = base64.urlsafe_b64encode(expiry.encode("ascii")).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(encoded)}"


def _cookie_is_valid(cookie: "str | None") -> bool:
    if not cookie or _KEY is None:
        return False
    encoded, _, signature = cookie.partition(".")
    if not signature or not hmac.compare_digest(signature, _sign(encoded)):
        return False
    try:
        padding = "=" * (-len(encoded) % 4)
        expiry = int(base64.urlsafe_b64decode(encoded + padding).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return False
    return expiry > time.time()


def _served_over_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    return (forwarded.split(",")[0].strip() or request.url.scheme) == "https"


# Everything else is behind the gate. /assets is here because the unlock
# page would otherwise be the only page on the site that cannot be
# styled -- and it carries its own CSS inline for exactly that reason, so
# nothing here leaks the corpus.
_OPEN_PATHS = {"/login", "/api/login", "/robots.txt", "/favicon.ico"}


app = FastAPI(title="CorpusVic", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=html_view.TEMPLATE_DIR), name="assets")
# A contents page is 100KB of HTML and the endnotes three times that;
# compressed, a sixth.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def asset_caching(request: Request, call_next):
    """Assets named with their version (html_view.asset_version) never
    change at that URL, so a browser keeps them a year without asking.
    Without it, every page re-checked nine files before it could draw."""
    response = await call_next(request)
    if request.url.path.startswith("/assets/") and response.status_code == 200:
        if request.query_params.get("v") or request.url.path.startswith("/assets/fonts/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def gate(request: Request, call_next):
    if not gated():
        return await call_next(request)
    path = request.url.path
    if path in _OPEN_PATHS or path.startswith("/assets/"):
        return await call_next(request)
    if _cookie_is_valid(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "This site is not open."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


_UNLOCK_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CorpusVic</title>
<!-- Styled inline, so that everything else on this site -- /assets
     included -- can sit behind the gate without the one page a visitor
     can reach arriving unstyled. -->
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: ui-sans-serif, system-ui, sans-serif; background:#f3f2f2; color:#201e1d; }
  @media (prefers-color-scheme: dark) { body { background:#120e0e; color:#eeeaea; } form { background:#1c1717 !important; border-color:#413b3b !important; } input { background:#120e0e !important; color:#eeeaea !important; border-color:#413b3b !important; } }
  form { background:#eae9e9; border:1px solid #cfcccc; padding:26px; width:320px; }
  h1 { font-size:16px; margin:0 0 4px; }
  p { font-size:13px; color:#605d5d; margin:0 0 16px; }
  input { width:100%; padding:8px; font:inherit; border:1px solid #cfcccc; background:#fff; color:#201e1d; box-sizing:border-box; }
  button { width:100%; margin-top:10px; padding:9px; border:0; background:#ec3013; color:#fff; font:inherit; cursor:pointer; }
  .err { color:#ae1800; font-size:12.5px; margin-top:10px; min-height:1em; }
</style></head>
<body>
<form onsubmit="unlock(event)">
  <h1>CorpusVic</h1>
  <p>Victorian legislation, read closely. This site is not open yet.</p>
  <input id="p" type="password" autocomplete="current-password" autofocus placeholder="Passphrase">
  <button type="submit">Read</button>
  <div class="err" id="err">__ERROR__</div>
</form>
<script>
async function unlock(e) {
  e.preventDefault();
  const err = document.getElementById("err");
  err.textContent = "";
  const res = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passphrase: document.getElementById("p").value }),
  });
  if (res.ok) { location.href = "/"; return; }
  const data = await res.json().catch(() => ({}));
  err.textContent = data.detail || "That is not the passphrase.";
}
</script>
</body></html>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    if not gated():
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_UNLOCK_PAGE.replace("__ERROR__", ""))


class Unlock(BaseModel):
    passphrase: str = ""


@app.post("/api/login")
def unlock(body: Unlock, request: Request):
    if not gated():
        return JSONResponse({"ok": True})
    ip = _client_ip(request)
    if _locked_out(ip):
        raise HTTPException(429, "Too many attempts -- try again later.")
    offered = hashlib.pbkdf2_hmac("sha256", body.passphrase.encode("utf-8"),
                                  b"corpusvic-site-gate", PBKDF2_ITERATIONS)
    if not hmac.compare_digest(offered, _KEY):
        _record_failure(ip)
        raise HTTPException(401, "That is not the passphrase.")
    _FAILED.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, _issue_cookie(), httponly=True, samesite="lax",
                    max_age=SESSION_LIFETIME_SECONDS, path="/",
                    secure=_served_over_https(request))
    return resp


# ---------------------------------------------------------------------------
# What is on the site
# ---------------------------------------------------------------------------


def _published_slugs() -> dict:
    """{parse slug -> the address it is served at}, for the works that
    are on the site.

    Same rule as the archive build: the newest version of a work answers
    at the work's own name, and an older reprint at one naming its date,
    so a citation to a point in time keeps meaning that point."""
    works = db.published_works(BASE_DIR)
    candidates = [
        slug for slug in dashboard.discover_slugs()
        if (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists()
        and split_document_slug(slug)[0] in works
        and dashboard._document_kind(slug) != "bill"
    ]
    return site_slugs(sorted(candidates), as_at_of(candidates))


def as_at_of(slugs, source=None) -> dict:
    """{parse slug -> the ISO date its text is as at}, what site_slugs
    names an older reprint by."""
    version = getattr(source or dashboard, "_act_version", None)
    return {slug: (version(slug) if version else {}).get("as_at") for slug in slugs}


class _Moved(Exception):
    """A document asked for by its parse slug: the old -v112 addresses,
    which named the Authorised Version number (issue #95). Links to them
    are out in the world, so they redirect rather than 404."""

    def __init__(self, old: str, new: str):
        self.old, self.new = old, new


@app.exception_handler(_Moved)
def _moved(request: Request, exc: _Moved):
    url = request.url.path.replace(f"/{exc.old}", f"/{exc.new}", 1)
    if request.url.query:
        url += "?" + request.url.query
    return RedirectResponse(url, status_code=308)


def _resolve(site_slug: str) -> str:
    """The parse this address is served from, or a 404 saying which of
    the two reasons it is."""
    published = _published_slugs()
    for slug, address in published.items():
        if address == site_slug:
            return slug
    if site_slug in published:
        raise _Moved(site_slug, published[site_slug])
    raise HTTPException(404, f"{site_slug!r} is not published here.")


def _site_links(value):
    """dashboard.py's /browse/<parse slug> URLs, as this site's addresses
    -- the same rewrite the archive applies (see _rewrite_urls)."""
    return _rewrite_urls(value, "", _published_slugs())


# ---------------------------------------------------------------------------
# Pages as last rendered (corpus/web/page_cache.py)
# ---------------------------------------------------------------------------

_stamp_memo: dict = {}


def _site_stamp() -> str:
    """What the site's pages are rendered from, as it is now: the running
    code and assets, the review database, and the parses. Asked on every
    request, so worked out at most once a second."""
    now = time.monotonic()
    if _stamp_memo.get("at", -1.0) + 1.0 > now and _stamp_memo.get("base") == BASE_DIR:
        return _stamp_memo["stamp"]
    parts = [dashboard._RUNNING_CODE, html_view.asset_version()]
    for path in (db.db_path(BASE_DIR), Path(f"{db.db_path(BASE_DIR)}-wal")):
        try:
            st = path.stat()
            parts.append((st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(None)
    parsed = sorted((p.name, p.stat().st_mtime_ns) for p in (BASE_DIR / "data" / "parsed").glob("*.json"))
    parts.append(parsed)
    stamp = hashlib.sha1(repr(parts).encode()).hexdigest()[:16]
    _stamp_memo.update(at=now, base=BASE_DIR, stamp=stamp)
    return stamp


_caches: dict = {}


def _pages() -> PageCache:
    if BASE_DIR not in _caches:
        _caches[BASE_DIR] = PageCache(BASE_DIR / "data" / ".cache" / "public-pages.sqlite", _site_stamp)
    return _caches[BASE_DIR]


def _served(request: Request, render, media_type: str = "text/html") -> Response:
    """The page at this request's URL, as last rendered (see _pages). A
    reader coming back to a page unchanged since gets a 304, not the page
    again."""
    url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    body, etag = _pages().get(url, render)
    etag = f'"{etag}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)


def _warm_pages():
    """(url, render) for the pages worth having ready before anyone asks,
    most read first: the landing page, every published document's
    contents, then the sections of each work's current version. An older
    reprint's sections are rendered when first read."""
    yield "/", _landing_html
    published = sorted(_published_slugs().items())
    for _slug, address in published:
        yield f"/browse/{address}/", (lambda a=address: _contents_html(a))
    for slug, address in published:
        if address != split_document_slug(slug)[0]:
            continue   # an older reprint, at its dated address
        yield f"/browse/{address}/endnotes", (lambda a=address: _endnotes_html(a))
        pages = dict.fromkeys(dashboard._page_index(slug)["by_node_index"].values())
        for page in pages:
            yield f"/browse/{address}/section/{page}", (lambda a=address, p=page: _section_html(a, p))


_PAGE_URLS = [
    (re.compile(r"^/$"), lambda m, q: _landing_html()),
    (re.compile(r"^/browse/([^/]+)/$"), lambda m, q: _contents_html(m[1])),
    (re.compile(r"^/browse/([^/]+)/endnotes$"), lambda m, q: _endnotes_html(m[1])),
    (re.compile(r"^/browse/([^/]+)/section/([^/]+)$"), lambda m, q: _section_html(m[1], m[2])),
    (re.compile(r"^/api/browse/([^/]+)/preview$"),
     lambda m, q: _preview_json(m[1], (q.get("section") or [None])[0], (q.get("fragment") or [None])[0])),
]


def _render_url(url: str) -> str:
    """Any cached page, rendered again by its URL -- how the worker, in
    another process, renders what a reader found stale here."""
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url)
    for pattern, render in _PAGE_URLS:
        m = pattern.match(parts.path)
        if m:
            return render(m, parse_qs(parts.query))
    raise HTTPException(404, f"No page at {url}")


def _render_worker() -> None:
    """The other process: renders the pages readers found stale, and the
    ones never rendered yet, at a lower priority than the site itself."""
    import os

    try:
        os.nice(10)
    except OSError:
        pass
    # Its own connections, not ones shared across the fork. The inherited
    # ones are kept, unused, rather than closed: closing one here could
    # checkpoint the database under the process that opened it.
    global _INHERITED
    _INHERITED = [*db._connections.values(), *_caches.values()]
    db._connections.clear()
    _caches.clear()
    # Served as it was while it is worked out again, so a work's history
    # can be a few minutes behind: rebuilt after every review edit, a
    # hundred versions' worth was most of what this did while someone
    # reviewed.
    dashboard.TIMELINE_MAX_AGE = 300.0
    _pages().work(_render_url, lambda: _warm_pages())


_INHERITED: list = []


def _start_render_worker():
    import multiprocessing

    worker = multiprocessing.get_context("fork").Process(target=_render_worker, name="page-renderer", daemon=True)
    worker.start()
    _pages().worker = worker
    return worker


def _page(title: str, body: str, base_url: "str | None" = None,
          reader_layout: bool = False, query: str = "",
          canonical: "str | None" = None) -> HTMLResponse:
    """Built through the archive's own page wrapper, so the site's legal
    notice cannot be left off a page by forgetting it here. What differs
    is that this side has a server: hover cards are rendered on demand
    rather than pre-built, and the search box has somewhere to go."""
    return HTMLResponse(_finished_page(
        title, body, base_url=base_url, reader=reader_layout,
        search_url="/search", preview_source="api", canonical=canonical))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return _served(request, _landing_html)


def _landing_html() -> str:
    published = []
    for slug, address in sorted(_published_slugs().items()):
        status = dashboard.act_status(slug)
        published.append({
            "slug": slug, "site_slug": address,
            "title": dashboard._act_title(slug), "kind": status["kind"],
            "as_at": status["version_as_at"],
            "checked_provisions": 0, "total_provisions": 0,
        })
    return _landing_page_html(published, "", search_url="/search", preview_source="api")


@app.get("/browse/{site_slug}")
def browse_redirect(site_slug: str):
    return RedirectResponse(f"/browse/{site_slug}/", status_code=307)


@app.get("/browse/{site_slug}/", response_class=HTMLResponse)
def contents(site_slug: str, request: Request):
    _resolve(site_slug)   # live: a work taken off the site is gone at once
    return _served(request, lambda: _contents_html(site_slug))


def _contents_html(site_slug: str) -> str:
    slug = _resolve(site_slug)
    body = reader.contents_page(
        dashboard, slug, f"/browse/{site_slug}", rewrite=_site_links, show_review_badge=False,
        notice=_partial_notice(slug),
        related=dashboard.related_links(
            slug, lambda s: f"/browse/{_published_slugs()[s]}/" if s in _published_slugs() else None),
    )
    # The contents are what carries an outline column now, so this is the
    # page laid out in two; a provision's own page is one column of text.
    return _page(dashboard._act_title(slug), body, f"/browse/{site_slug}", reader_layout=True).body.decode()


@app.get("/browse/{site_slug}/section/{section_slug}", response_class=HTMLResponse)
def section(site_slug: str, section_slug: str, request: Request):
    _resolve(site_slug)
    return _served(request, lambda: _section_html(site_slug, section_slug))


def _section_html(site_slug: str, section_slug: str) -> str:
    slug = _resolve(site_slug)
    body = reader.section_page(
        dashboard, slug, f"/browse/{site_slug}", section_slug,
        rewrite=_site_links, show_review_badge=False, notice=_unverified_notice(slug, section_slug))
    if body is None:
        raise HTTPException(404, f"No such provision in {site_slug!r}.")
    return _page(dashboard._act_title(slug), body, f"/browse/{site_slug}",
                 canonical=f"/browse/{site_slug}/section/{section_slug}").body.decode()


@app.get("/browse/{site_slug}/endnotes", response_class=HTMLResponse)
def endnotes(site_slug: str, request: Request):
    _resolve(site_slug)
    return _served(request, lambda: _endnotes_html(site_slug))


def _endnotes_html(site_slug: str) -> str:
    slug = _resolve(site_slug)
    body = reader.endnotes_page(dashboard, slug, f"/browse/{site_slug}")
    if body is None:
        raise HTTPException(404, f"{site_slug!r} has no endnotes.")
    return _page(f"{dashboard._act_title(slug)} — Endnotes", body, f"/browse/{site_slug}").body.decode()


@app.get("/api/browse/{site_slug}/preview")
def preview(site_slug: str, request: Request, section: "str | None" = None, fragment: "str | None" = None):
    """The hover cards. Behind the same publication check as the pages --
    without it, hovering a cross-reference would hand out the full text
    of a provision the site refuses to serve."""
    _resolve(site_slug)

    return _served(request, lambda: _preview_json(site_slug, section, fragment), media_type="application/json")


def _preview_json(site_slug: str, section: "str | None", fragment: "str | None") -> str:
    slug = _resolve(site_slug)
    card = html_view.render_preview(dashboard._parsed(slug), dashboard._act_title(slug), section, fragment)
    if card is None:
        raise HTTPException(404, "No card for that.")
    return json.dumps(card)


def _checked_pages(slug: str) -> tuple:
    from corpus.review.review import group_into_units

    nodes, _unattached, _hierarchy = dashboard._current_nodes(slug)
    page_index = dashboard._page_index(slug)
    all_pages = set(page_index["by_node_index"].values())
    checked = approved_page_slugs(nodes, group_into_units(nodes), page_index["by_node_index"])
    return checked, all_pages


def _partial_notice(slug: str) -> "str | None":
    checked, all_pages = _checked_pages(slug)
    if checked == all_pages:
        return None
    return _partial_notice_html(len(checked), len(all_pages))


def _unverified_notice(slug: str, section_slug: str) -> "str | None":
    checked, _all_pages = _checked_pages(slug)
    return None if section_slug in checked else _unverified_notice_html()


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
def search_page(q: str = "", superseded: str = "", em: str = "",
                offset: int = 0):
    """A plain page for a plain GET form, so search works with
    JavaScript off -- which for a reference work about the law is worth
    more than a type-ahead.

    The scope parameters default to off, which is what makes an
    ordinary search a search of the law as it stands. See
    corpus/search.py's Scope."""
    scope = search.Scope.from_params(
        {"superseded": superseded, "em": em})
    body = search_view.page_body(_INDEX, q, scope, offset,
                                 action="/search", unavailable=search.SearchUnavailable)
    return _page("Search", body, query=q)


@app.get("/api/search")
def search_api(q: str = "", superseded: str = "", em: str = "",
               offset: int = 0, limit: int = 20):
    scope = search.Scope.from_params(
        {"superseded": superseded, "em": em})
    try:
        return _INDEX.search(q, scope, limit=min(max(limit, 1), 100), offset=max(offset, 0))
    except search.SearchUnavailable as e:
        raise HTTPException(503, str(e)) from e


@app.get("/robots.txt")
def robots():
    """A site behind a passphrase asks crawlers to stay out; an open one
    does too, unless somebody said otherwise. Mostly-unchecked readings
    of the law are not something to invite indexing of by default."""
    body = ROBOTS_TXT_ALLOW_ALL if (not gated() and _ALLOW_INDEXING) else ROBOTS_TXT
    return Response(body, media_type="text/plain")


# ---------------------------------------------------------------------------
# The archive: the same site as static files
# ---------------------------------------------------------------------------
#
# Every page above, written out as plain HTML for a host with no server --
# GitHub Pages, run by .github/workflows/pages.yml on every push that
# changes data/parsed/ or data/review/, and the dashboard's "Rebuild
# public site". The same pages from the same builders (dashboard.py's own
# file-backed helpers), so the two copies of the site cannot drift apart.
#
# What the archive does without a server:
#   - A passphrase ($SITE_PASSWORD, --password, or deploy/site.env) gates
#     it by encrypting every page at build time (corpus/publishing/
#     site_crypto.py), not by hiding readable files behind a form.
#   - The base path is worked out from the repository: none under a
#     custom domain (a CNAME file, also copied into the build, since a
#     Pages deployment serves exactly what was uploaded), else
#     "/<repo>" from $GITHUB_REPOSITORY, else none.
#   - Hover cards are rendered at build time, one preview.json beside each
#     page a link reaches, encrypted like the rest (_write_previews).
#   - An unresolved citation's /legislation/<no> address is not pre-built,
#     so that one link 404s rather than explaining the Act isn't parsed.
#
# A work's newest version is published in full, checked or not, each
# unchecked provision saying so (_unverified_notice_html): publishing only
# the checked ones read as an Act with holes in it, and a section merely
# unchecked looked the same as one that does not exist.

def select_candidate_slugs(statuses: dict[str, dict],
                           include_unpublished: bool = False) -> list[str]:
    """Which documents are even eligible for the site: every parsed one
    somebody has put on the site, including older versions of a work.
    How much of a candidate a human has checked is a separate question,
    answered per provision by approved_page_slugs below, and it decides
    what each page says about itself rather than whether it exists.

    Publication is decided per *work* (see corpus/db.py), so a work's
    reprints are in or out together and this filter never splits a
    version set -- which is what lets site_slugs below keep its promise
    that the newest version holds the work's own address.

    include_unpublished is for an archive of everything held rather than
    of what is on the site. It has to be asked for: a build that
    published more than the site does, by default, would be a way to
    publish something by accident.

    Older reprints are published because a reader needs to be able to go
    and read one: "Compare with another version" on a provision offers
    every version this pipeline holds, and an offer that 404s is worse
    than no offer. They are published at their own dated addresses,
    and are not listed on the landing page -- see site_slugs, which is
    what decides those addresses, and _landing_page_html.

    Pure and file-I/O-free so it's unit-testable on fabricated status
    dicts -- see tests/test_public_archive.py. `statuses` is
    {slug: dashboard.act_status(slug)}."""
    # Never a Bill: its clauses restate the Act's wording, and the site
    # does not host legislative text it is not the Act's (issue #98). It
    # stays in the pipeline, which uses it to tie EM notes to sections.
    return sorted(
        slug for slug, status in statuses.items()
        if status["parsed"] and (include_unpublished or status.get("published"))
        and status.get("kind") != "bill"
    )


def site_slugs(candidates: list[str], as_at: "dict | None" = None) -> dict[str, str]:
    """{parse slug -> the path segment it is published under}.

    The newest version of a work is published under the work's own name,
    with no version in the address at all: /browse/criminal-procedure-act/
    is the Act as it now stands, and stays that address as new reprints
    land. Anything older has an address of its own, so a link to it
    still means that text a year from now, which is exactly what a
    citation to a point in time needs.

    It also makes the cross-Act links work: known_acts.yaml names a work
    ("criminal-procedure-act"), so every reference to the Act from another
    Act's text has always pointed at the unversioned address -- which,
    until now, nothing was published at.

    An older reprint is named by the day its text is as at
    (criminal-procedure-act-2026-07-01), from `as_at` {slug -> ISO date}.
    Never by its version number: that is the Authorised Version's
    number, which is not ours to reproduce (issue #95). One with no date
    recorded keeps its parse slug; every Act parsed since front matter
    was read has one."""
    as_at = as_at or {}
    newest: dict[str, str] = {}
    for slug in candidates:
        work, version = split_document_slug(slug)
        held = newest.get(work)
        if held is None:
            newest[work] = slug
            continue
        _w, held_version = split_document_slug(held)
        if version is not None and (held_version is None or version > held_version):
            newest[work] = slug
    current = {slug: work for work, slug in newest.items()}
    out, taken = {}, set(current.values())
    for slug in candidates:
        if slug in current:
            out[slug] = current[slug]
            continue
        work, version = split_document_slug(slug)
        address = f"{work}-{as_at[slug]}" if version is not None and as_at.get(slug) else slug
        # Two reprints stating the same day: rare, and neither may take
        # the other's address.
        n = 2
        while address in taken:
            address, n = f"{work}-{as_at[slug]}-{n}", n + 1
        taken.add(address)
        out[slug] = address
    return out


def approved_units(nodes: list, units: list[list[int]]) -> set[int]:
    """Which units a reviewer has actually approved -- positions into
    `units`, for the effective nodes review.build_effective_nodes_indexed
    returns (a merged-away node is None there, and doesn't count against
    the unit it used to be in).

    Approved means every node still in the unit carries verified_at and
    none is flagged for follow-up. The two are deliberately exclusive in
    review.py: flagging a piece means "not sure, revisit this", and
    commit_unit leaves such a node unstamped on purpose. So a flagged
    provision counts as unchecked and says so on its own page, which is
    the point of the reviewer having flagged it."""
    approved = set()
    for u, unit in enumerate(units):
        live = [nodes[i] for i in unit if nodes[i] is not None]
        if live and all(n.get("verified_at") and not n.get("needs_followup") for n in live):
            approved.add(u)
    return approved


def approved_page_slugs(nodes: list, units: list[list[int]], by_node_index: dict[int, str]) -> set[str]:
    """The page ids (build_page_index's own "s14", "s14_2", ...) whose
    provision a human has checked. A page is one unit -- a Section and
    everything nested under it -- so it is checked exactly when that unit
    is. Every page carries its text either way; this decides which of
    them have to say they haven't been confirmed."""
    approved = approved_units(nodes, units)
    unit_of_root = {unit[0]: u for u, unit in enumerate(units)}
    return {
        page for node_index, page in by_node_index.items()
        if unit_of_root.get(node_index) in approved
    }


# dashboard.py builds its browse URLs for the live dashboard: rooted at
# the domain, and naming a document by its parse slug. Neither is right
# here -- the site may sit under a repository path, and the newest version
# of a work is published under the work's own name (see site_slugs). Every
# such URL that reaches a published page therefore goes through here
# first. It is a rewrite rather than a parameter threaded through
# dashboard.py because those helpers serve a running server that is right
# as it stands, and one rule applied at the boundary is easier to keep
# whole than a prefix passed through a dozen call sites.
_BROWSE_URL_RE = re.compile(r"^/browse/([^/]+)(/.*)?$")


def _rewrite_url(url: "str | None", base_path: str, slugs: dict) -> "str | None":
    if not url:
        return url
    m = _BROWSE_URL_RE.match(url)
    if not m:
        return url
    slug, rest = m.group(1), m.group(2) or "/"
    return f"{base_path}/browse/{slugs.get(slug, slug)}{rest}"


def _rewrite_urls(value, base_path: str, slugs: dict):
    """The same rewrite over the shapes dashboard.py hands back: a plain
    URL, the {version -> URL} map behind "Compare with another version",
    and the crossref chips' own hrefs."""
    if isinstance(value, str):
        return _rewrite_url(value, base_path, slugs)
    if isinstance(value, dict):
        return {k: _rewrite_urls(v, base_path, slugs) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite_urls(v, base_path, slugs) for v in value]
    return value


def publishes_anything(slug: str) -> bool:
    """Whether this document has even one provision, and so will
    produce pages at all. The same question _build_doc answers on its way
    past; asked separately because an Act's contents page has to link to
    its Bill and Explanatory Memorandum, and cannot know whether those
    exist until every document has been looked at."""
    return bool(dashboard._page_index(slug)["by_node_index"])


CNAME_FILE = PROJECT_ROOT / "CNAME"


def custom_domain() -> "str | None":
    """The domain this site is published at, from the repository's CNAME
    file, or None if it is published at a github.io address.

    CNAME is GitHub Pages' own way of recording a custom domain -- it is
    the file the Settings page writes when you set one -- so it is read
    here rather than duplicated into a second setting that could disagree
    with it."""
    if not CNAME_FILE.exists():
        return None
    return CNAME_FILE.read_text(encoding="utf-8").strip() or None


def _default_base_path() -> str:
    """The path prefix the site will be served under.

    Nothing, when there is a custom domain: it is mapped at that domain's
    own root, so a link needs no prefix at all. This is what the CNAME
    file decides, and getting it wrong is not subtle -- with a "/repo"
    prefix against a custom domain, every link on the site resolved to
    https://www.corpusvic.au/<repo-name>/browse/..., which is nowhere.

    Otherwise "/repo-name" when $GITHUB_REPOSITORY (owner/repo, set by
    every GitHub Actions job) is present, matching the default URL of a
    project site at https://<owner>.github.io/<repo>/; and "" outside
    Actions, for a local preview served from a directory root."""
    if custom_domain():
        return ""
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"/{repo.split('/')[-1]}" if repo else ""


# Where the server keeps the passphrase, so that it belongs to the
# machine rather than to whoever happens to be typing the build command.
# Gitignored; deploy/site.env.example is the tracked template.
# Kept in corpus/site_env.py now, because the live site reads the same
# file and must not import this module (which imports the dashboard) to
# do it. Re-exported here under the names this module has always used.
SITE_ENV_FILE = site_env.SITE_ENV_FILE

# What a gated page carries and an open one cannot: the encrypted payload
# the unlock script reads (see corpus/site_crypto.py's _GATE_TEMPLATE).
_GATED_MARKER = 'id="payload"'


password_from_env_file = site_env.password_from_env_file


def already_gated(out: Path) -> bool:
    """Whether the build already at `out` is behind a passphrase."""
    landing = Path(out) / "index.html"
    try:
        return _GATED_MARKER in landing.read_text(encoding="utf-8")
    except OSError:
        return False


def resolve_password(cli_password: "str | None", no_password: bool, out: Path) -> "str | None":
    """The passphrase this build should use, and a refusal where using
    none would quietly publish what was behind one.

    Taking the gate off is a real choice and stays available, but it has
    to be made rather than arrived at: pages served once in the clear are
    served, and no later rebuild takes that back."""
    if no_password:
        return None
    # A passphrase on the command line is visible to anything that can
    # list processes, so it is the last resort rather than the first.
    # SITE_ENV_FILE passed rather than left to default, so that this
    # module's own name for the file is the one that decides -- it is
    # what a caller (or a test) overrides.
    password = os.environ.get("SITE_PASSWORD") or password_from_env_file(SITE_ENV_FILE) or cli_password
    if not password and already_gated(out):
        raise SystemExit(
            f"Refusing to rebuild {out}/ without a passphrase: what is there now is gated, and "
            "this build would replace it with pages anyone can read.\n"
            f"  - to keep the gate: put SITE_PASSWORD in {SITE_ENV_FILE} (see "
            "deploy/site.env.example), or set it in the environment\n"
            "  - to open the site deliberately: pass --no-password"
        )
    return password


def _provision_label(node: dict) -> str:
    """"14 Determination of limits" -- enough to name a provision on its
    own placeholder page, so a reader who followed a link knows which one
    they were reaching for."""
    parts = [str(p) for p in (node.get("number"), node.get("heading")) if p]
    return " ".join(parts) or str(node.get("type", "Provision")).replace("_", " ").capitalize()


def _unverified_notice_html() -> str:
    """Set at the top of a provision nobody has checked yet, above its own
    heading, because it qualifies every word below it.

    The text underneath is real: it is what the parser read off the
    official PDF, not a placeholder and not a guess at what the provision
    might say. What it has not had is a human reading it against the page
    to confirm the parser got it right -- which is a different and
    smaller claim than "this may be wrong", and the notice says the
    smaller one, because overstating the doubt would be as misleading as
    hiding it."""
    return (
        '<div class="disclaimer">'
        "<strong>This provision has not been checked by a human.</strong> "
        "The text below was read automatically from the official PDF and has not yet been "
        "verified against it, so it may differ from the provision as published — in its "
        "wording, its numbering, or where one provision ends and the next begins. "
        f'For the authorised text, see <a href="{OFFICIAL_SOURCE_URL}" rel="noopener">'
        f"{OFFICIAL_SOURCE_NAME}</a>."
        "</div>"
    )


def _partial_notice_html(checked: int, total: int) -> str:
    """The same caveat on the contents page, where it is about the
    document rather than about one provision.

    Leads with the state rather than with the arithmetic -- "only part of
    this has been reviewed" is what a reader needs first, and a sentence
    that opens on two numbers makes them do the division before they
    learn anything. The count stays, one clause in, because the question
    behind the state is "how much", and "under review" without a figure
    could mean anything between one provision and all of them.

    What it must not say is that the unreviewed provisions are empty.
    They are not: every one carries the parser's reading of the official
    PDF, and has done since those provisions were published rather than
    withheld. Saying otherwise would send a reader away from a page that
    has what they came for."""
    return (
        '<div class="disclaimer">'
        "<strong>Only part of this document has been reviewed.</strong> "
        f"{checked} of {total} provisions have been checked by a human against the "
        "official PDF. The rest are here in full, read automatically from that PDF, "
        "and say so at the top of their own page."
        "</div>"
    )


def _copy_template(out: Path) -> None:
    """The whole template directory -- the stylesheets, the browser-side
    scripts and Junicode -- published as "assets/", which is where every
    page's asset URLs point (see html_view.page_shell).

    Copied wholesale rather than file by file so that adding a stylesheet
    to static/site/ needs no change here; page.html is left out because
    Python renders it into each page rather than the browser fetching it.
    The fonts travel with their licence, and are self-hosted rather than
    pulled off a CDN so that reading the law here doesn't announce itself
    to a third party (see static/site/fonts/README.md)."""
    shutil.copytree(
        html_view.TEMPLATE_DIR, out / "assets",
        ignore=shutil.ignore_patterns("page.html", "__pycache__"),
        dirs_exist_ok=True,
    )

def _finished_page(title: str, body: str, base_url: "str | None" = None, reader: bool = False,
          gate: "SiteGate | None" = None, site_prefix: "str | None" = None,
          search_url: "str | None" = None, preview_source: str = "static",
          canonical: "str | None" = None) -> str:
    """A finished page: the body, then the site footer. Every published
    page is built through here rather than calling page_shell directly,
    because the footer is the site's legal notice and the failure to
    design against is a new kind of page quietly shipping without it.

    The live dashboard's own /browse pages don't get this -- they're an
    internal preview behind a login, already labelled as one, not a thing
    the public reads.

    search_url and preview_source are what the live public site differs
    by: it has a server, so it answers hover cards on demand and has
    somewhere for a search box to submit to. The archive has neither,
    which is why both default to the archive's answer."""
    return html_view.page_shell(
        title, body + _footer_html(), base_url=base_url, reader=reader,
        canonical=canonical,
        # With no server to render a hover card on demand, the archive's
        # cards are pre-built (see _write_previews) and the page says so.
        preview_source=preview_source,
        search_url=search_url,
        site_salt=base64.b64encode(gate.salt).decode("ascii") if gate else None,
        site_prefix=site_prefix,
    )


def _write(path: Path, page_html: str, gate: "SiteGate | None" = None) -> None:
    """One page, encrypted behind the passphrase gate first if there is
    one (see corpus/site_crypto.py). Everything the site publishes
    goes through here, so a gated build has no page that was missed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gate.wrap(page_html) if gate else page_html, encoding="utf-8")


def _build_doc(slug: str, out_dir: Path, base_path: str, gate: "SiteGate | None" = None,
               slugs: "dict | None" = None, published_slugs: "set[str] | None" = None) -> "dict | None":
    """Every page for one document: its index, one per section, and its
    Endnotes if it has any -- exactly what browse_index/browse_section/
    browse_endnotes each build for one HTTP request, just written to
    files under out_dir/browse/<slug>/ instead.

    Every provision gets its real text. One nobody has checked yet
    carries a notice saying so, above its own heading (see
    _unverified_notice_html). Returns the summary used for the site's own
    landing page, or None for a document with no provisions at all.

    The summary carries "pages" (the page ids this document released) and
    "links" (everything its pages point at), which between them are what
    _write_previews needs -- gathered here because a page's HTML is only
    in hand before it is written and, on a gated build, encrypted.

    slugs is site_slugs()'s {parse slug -> published path segment}: this
    document is written under its own entry, and every URL dashboard.py
    hands back is rewritten through the whole map."""
    links: set = set()
    slugs = slugs or {}
    site_slug = slugs.get(slug, slug)
    base_url = f"{base_path}/browse/{site_slug}"
    doc_dir = out_dir / "browse" / site_slug

    def site(value):
        return _rewrite_urls(value, base_path, slugs)
    # What this function still needs for itself. Everything a page is
    # built from is corpus/reader.py's business now, and each of these
    # lookups is cached against the data it reads (see dashboard.py's
    # signature-keyed caches), so asking per page costs nothing.
    nodes, _unattached, _hierarchy = dashboard._current_nodes(slug)
    title = dashboard._act_title(slug)
    page_index = dashboard._page_index(slug)

    # Units grouped over the same node list page_index was built from, so
    # the two agree on what a node index means. (build_effective_nodes_
    # indexed keeps original parse positions instead, which is what
    # run_ai_review.py needs and exactly what must not be mixed in here:
    # once anything has been merged the two numbering schemes diverge.)
    # _current_nodes returns the stored verified row wherever there is
    # one, so verified_at/needs_followup are readable straight off these.
    units = group_into_units(nodes)
    all_pages = set(page_index["by_node_index"].values())
    if not all_pages:
        return None
    # Which provisions a human has confirmed. It no longer decides what
    # is published -- everything is -- only which pages have to say they
    # haven't been checked.
    checked_pages = approved_page_slugs(nodes, units, page_index["by_node_index"])

    index_body = reader.contents_page(
        dashboard, slug, base_url, rewrite=site, show_review_badge=False,
        # Only documents this build actually published: a link to an EM
        # it did not write would be a link to a page that isn't there.
        related=dashboard.related_links(
            slug, lambda s: f"{base_path}/browse/{slugs.get(s, s)}/" if s in (published_slugs or ()) else None),
        notice=(None if checked_pages == all_pages
                else _partial_notice_html(len(checked_pages), len(all_pages))),
    )
    links |= _link_targets(index_body, base_path)
    # Two columns here, because the contents carry the outline now; one
    # column on a provision's own page, which is the text and nothing else.
    _write(doc_dir / "index.html", _finished_page(title, index_body, base_url, reader=True, gate=gate), gate)

    for _node_index, section_slug in page_index["by_node_index"].items():
        body = reader.section_page(
            dashboard, slug, base_url, section_slug,
            rewrite=site, show_review_badge=False,
            notice=None if section_slug in checked_pages else _unverified_notice_html(),
        )
        if body is None:
            continue  # not expected -- page_index only ever names real sections
        links |= _link_targets(body, base_path)
        _write(doc_dir / "section" / section_slug / "index.html",
               _finished_page(title, body, base_url, gate=gate,
                     canonical=f"{base_url}/section/{section_slug}/"), gate)

    # A provision this version no longer has keeps the address it had,
    # so a citation to it still lands somewhere that says what happened.
    for ghost in dashboard._ghosts(slug):
        body = reader.ghost_page(dashboard, slug, base_url, ghost["page"], rewrite=site)
        if body is None:
            continue
        links |= _link_targets(body, base_path)
        _write(doc_dir / "section" / ghost["page"] / "index.html",
               _finished_page(title, body, base_url, gate=gate,
                     canonical=f"{base_url}/section/{ghost['page']}/"), gate)

    endnotes_body = reader.endnotes_page(dashboard, slug, base_url)
    if endnotes_body is not None:
        links |= _link_targets(endnotes_body, base_path)
        _write(doc_dir / "endnotes" / "index.html",
               _finished_page(f"{title} — Endnotes", endnotes_body, base_url, gate=gate), gate)

    status = dashboard.act_status(slug)
    return {
        "slug": slug, "site_slug": site_slug, "title": title, "kind": status["kind"],
        "as_at": status["version_as_at"], "pages": 1 + len(all_pages),
        "checked_provisions": len(checked_pages), "total_provisions": len(all_pages),
        # Every page this document published, which is all of them: what
        # _write_previews needs to know a link has somewhere to land.
        "published_pages": all_pages, "links": links,
    }


# The official source. Every page this pipeline produces is a reading of
# what's published there, so the disclaimer points at it by name rather
# than describing it vaguely -- a reader who needs the authorised text
# needs to be able to go straight to it.
OFFICIAL_SOURCE_URL = "https://www.legislation.vic.gov.au"
OFFICIAL_SOURCE_NAME = "legislation.vic.gov.au"

_NOT_OFFICIAL_HTML = (
    "<strong>These are not official legislative texts.</strong> "
    "For full, authorised legislative texts you must refer to "
    f'<a href="{OFFICIAL_SOURCE_URL}" rel="noopener">{OFFICIAL_SOURCE_NAME}</a>. '
    "This site provides a computer-based interpretation of that text, with enhanced linking."
)

# Repeated at the foot of the page as well as the head, because the two
# are read by different people: the header catches someone arriving, the
# footer catches someone who has just finished reading a provision and is
# deciding what to do with it.
def _footer_html() -> str:
    """The site's footer, from static/site/footer.html.

    A file rather than a string in here, because a footer is wording, and
    wording is the part of this site most often changed by somebody who
    has no reason to be reading Python. It sat in this module as a
    constant while an editable footer.html existed beside the stylesheets
    and was loaded by nothing at all -- so edits to the obvious file did
    nothing, silently, which is the worst way for a thing to not work.

    Read per call rather than at import: html_view.template_html
    re-reads on mtime, so an edit shows on the next page load without
    restarting the server."""
    from corpus.publishing import html_view

    return html_view.template_html("footer.html")


# ---------------------------------------------------------------------------
# Hover previews
# ---------------------------------------------------------------------------
# On the dashboard a hover card is rendered on demand by an endpoint. A
# static host has nothing to ask, so the same cards are built here, at
# build time, and written as small JSON files beside the pages they
# describe -- one per target page, holding every anchor within it that
# anything actually links to. That last part is what keeps them small:
# the links the site contains are a far smaller set than the provisions
# it has, so an Act with a hundred pages needs about a hundred short
# files rather than a preview of every provision in it.
#
# They go through the gate like everything else. A preview is the
# provision's own words, so publishing it in the clear beside an
# encrypted page would hand over exactly what the gate is there to keep
# back -- see _encrypted_json.

_LINK_RE = re.compile(r'href="([^"]+)"')


def _link_targets(html: str, base_path: str) -> set:
    """The (slug, section id, fragment) each link in this page points at,
    for the links preview.js will try to preview -- a link into a section
    page, or an index anchor. Read off the rendered HTML rather than
    tracked as it is built, because the linkifier produces these deep
    inside the renderers and the page is the honest record of what a
    reader can actually hover."""
    targets = set()
    prefix = f"{base_path}/browse/"
    for href in _LINK_RE.findall(html):
        if not href.startswith(prefix):
            continue
        path, _hash, fragment = href.partition("#")
        parts = path[len(prefix):].strip("/").split("/")
        if len(parts) == 3 and parts[1] == "section":
            targets.add((parts[0], parts[2], fragment))
        elif len(parts) == 1 and parts[0] and fragment:
            targets.add((parts[0], "", fragment))
    return targets


def _encrypted_json(payload: dict, gate: "SiteGate | None") -> str:
    """The JSON a page's previews are read from, encrypted with the same
    key the pages are so that one unlock covers both (preview.js finds it
    by the salt the page carries)."""
    text = json.dumps(payload, separators=(",", ":"))
    return text if gate is None else json.dumps(gate.encrypt(text), separators=(",", ":"))


def _write_previews(out: Path, base_path: str, targets: set, published: dict,
                    gate: "SiteGate | None" = None) -> int:
    """One preview.json per linked-to page. Returns how many previews were
    written, for the build log.

    published maps a document's published path segment to (its parse slug,
    the page ids it actually released) -- the two differ for the newest
    version of a work, which is published under the work's own name (see
    site_slugs), and the targets are read off links and so name the
    published one.

    A link into a document that isn't published gets no preview file and
    so no card, which is the same answer the page behind it would
    give."""
    by_page = {}
    for site_slug, section, fragment in targets:
        entry = published.get(site_slug)
        if entry is None or (section and section not in entry[1]):
            continue
        by_page.setdefault((site_slug, section), set()).add(fragment)

    written = 0
    for (site_slug, section), fragments in sorted(by_page.items()):
        slug = published[site_slug][0]
        parsed = dashboard._parsed(slug)
        title = dashboard._act_title(slug)
        previews = {}
        for fragment in sorted(fragments):
            card = html_view.render_preview(parsed, title, section or None, fragment or None)
            if card is not None:
                previews[fragment] = card
        if not previews:
            continue
        path = out / "browse" / site_slug
        if section:
            path = path / "section" / section
        path.mkdir(parents=True, exist_ok=True)
        (path / "preview.json").write_text(_encrypted_json(previews, gate), encoding="utf-8")
        written += 1
    return written


def _provision_count_html(doc: dict) -> str:
    """"12 of 112 provisions checked" for a document still being worked
    through, and nothing at all for one where every provision has been --
    a count beside every entry would just be noise once the answer is
    always "all of them".

    Checked, not published: the whole document is published either way,
    and what differs between these entries is how much of it a human has
    confirmed against the PDF."""
    checked, total = doc["checked_provisions"], doc["total_provisions"]
    if checked >= total:
        return ""
    return f"{checked} of {total} provisions checked"


def _landing_page_html(published: list[dict], base_path: str,
                       search_url: "str | None" = None,
                       preview_source: str = "static") -> str:
    """The way in. Only current documents are listed: an older reprint is
    published and readable, but it is reached by asking for it -- from the
    provision you are on, where "Compare with another version" knows which
    provision you mean. A list that offered five reprints of one Act side
    by side would make choosing the right one the reader's first problem.

    A document is current here exactly when site_slugs gave it the work's
    own unversioned address.

    An Explanatory Memorandum is left off too: it belongs to the Act
    enacted from the Bill it explains, and that Act's own contents page
    offers it (see render_index's `related`). One that no published Act
    claims is listed after all, under its own heading -- better an odd
    entry than a page nothing reaches. Bills are never published (see
    select_candidate_slugs).

    No blurb (issue #97): the disclaimer says what the site is not, and
    the list is what it is."""
    claimed = {
        d["slug"]
        for doc in published
        for d in dashboard.related_documents(doc["slug"])
    }
    current = [
        doc for doc in published
        if doc["site_slug"] == split_document_slug(doc["slug"])[0]
        and (doc["kind"] == "act" or doc["slug"] not in claimed)
    ]

    def rows(docs):
        out = []
        for doc in docs:
            facts = [f"as at {html.escape(doc['as_at'])}" if doc["as_at"] else "", _provision_count_html(doc)]
            out.append(
                f'<li><a href="{base_path}/browse/{doc["site_slug"]}/">{html.escape(doc["title"])}</a> '
                f'<span class="text-muted">{" &middot; ".join(f for f in facts if f)}</span></li>')
        return "".join(out)

    acts = [doc for doc in current if doc["kind"] == "act"]
    others = [doc for doc in current if doc["kind"] != "act"]
    body = (
        # First in the body, before the heading: a reader should meet the
        # caveat without scrolling, not after deciding what to click.
        f'<div class="disclaimer">{_NOT_OFFICIAL_HTML}</div>'
        "<h1>Published legislation</h1>"
        + (f'<h2>Victorian Acts</h2><ul class="section-list">{rows(acts)}</ul>' if acts else "")
        + (f'<h2>Explanatory memoranda</h2><ul class="section-list">{rows(others)}</ul>' if others else "")
        + ("" if current else "<p>Nothing has been published yet.</p>")
    )
    return _finished_page("Published legislation", body, site_prefix=base_path,
                 search_url=search_url, preview_source=preview_source)


def robots_txt_for(gated: bool, allow_indexing: bool) -> str:
    """What to tell crawlers. Always something: no file at all means the
    crawler decides.

    It used to be written only on a gated build, which had the two cases
    exactly backwards. A gated site publishes ciphertext, so a crawler
    that ignored the file would index gibberish; an open site publishes
    thousands of provisions of mostly unchecked legal text, and that was
    the build with no robots.txt at all.

    So indexing is asked for rather than arrived at, and asking for it on
    a gated site is a contradiction resolved the safe way round. A crawl
    cannot be taken back: the pages come down and the snapshot stays
    up."""
    return ROBOTS_TXT_ALLOW_ALL if (allow_indexing and not gated) else ROBOTS_TXT


def build_site(out: Path, base_path: str, password: "str | None" = None,
               allow_indexing: bool = False, include_unpublished: bool = False) -> tuple:
    """The whole site. With a passphrase, every page is encrypted behind
    the unlock gate and a Disallow-everything robots.txt goes out beside
    them -- a site that isn't ready to be read isn't ready to be indexed
    either, and a crawler that got there first would keep serving a
    snapshot of it long after the gate went up.

    Returns (the documents published, how many preview files were
    written)."""
    gate = SiteGate(password) if password else None
    _copy_template(out)
    publication = db.load_publication(dashboard.BASE_DIR)
    statuses = {slug: dashboard.act_status(slug, publication)
                for slug in dashboard.discover_slugs()}
    candidates = select_candidate_slugs(statuses, include_unpublished)
    slugs = site_slugs(candidates, as_at_of(candidates))
    # Which candidates will publish anything, worked out before any page
    # is written: an Act's contents links to its Bill and Explanatory
    # Memorandum, and it can only do that for documents this build is
    # actually going to produce. Everything it reads is cached, so the
    # pass costs almost nothing.
    will_publish = {slug for slug in candidates if publishes_anything(slug)}
    published = [
        doc for doc in
        (_build_doc(slug, out, base_path, gate, slugs, will_publish) for slug in candidates)
        if doc
    ]
    landing = _landing_page_html(published, base_path)
    _write(out / "index.html", landing, gate)
    # Across the whole site, not per document: the links most worth
    # previewing are the ones into another document (a Bill clause, an
    # Explanatory Memorandum's note), and those can only be resolved once
    # every document's own pages are known.
    targets = _link_targets(landing, base_path).union(*(doc["links"] for doc in published)) if published else set()
    preview_files = _write_previews(
        out, base_path, targets,
        {doc["site_slug"]: (doc["slug"], doc["published_pages"]) for doc in published}, gate)
    # Never encrypted: a crawler has to be able to read the one file that
    # tells it what to do.
    _write(out / "robots.txt", robots_txt_for(gate is not None, allow_indexing))
    domain = custom_domain()
    if domain:
        # Published with the site, not just kept in the repository. A
        # Pages deployment serves exactly what the build uploaded, so a
        # CNAME that stays behind in the source tree is a custom domain
        # that stops being configured the first time this runs.
        (out / "CNAME").write_text(domain + "\n", encoding="utf-8")
    return published, preview_files


def _build(args) -> None:
    """The archive, written out: `python -m corpus.web.public build --out _site`."""
    base_path = args.base_path if args.base_path is not None else _default_base_path()
    out = Path(args.out)
    password = resolve_password(args.password, args.no_password, out)

    all_slugs = dashboard.discover_slugs()
    published, preview_files = build_site(out, base_path, password, args.allow_indexing,
                                          args.include_unpublished)
    published_slugs = {doc["slug"] for doc in published}
    skipped = [s for s in all_slugs if s not in published_slugs]

    print(f"Published {len(published)} document(s) to {out}/ (base path: {base_path or '(none)'}):")
    for doc in published:
        at = "" if doc["site_slug"] == doc["slug"] else f" (at /browse/{doc['site_slug']}/)"
        print(f"  {doc['slug']}{at} -- {doc['total_provisions']} provision(s), "
              f"{doc['checked_provisions']} checked by a human")
    print(f"{preview_files} page(s) carry hover-preview data for the links that reach them.")
    if skipped:
        print(f"Skipped {len(skipped)} document(s) (not parsed):")
        for slug in skipped:
            print(f"  {slug}")
    if password:
        print("Every page is encrypted behind the passphrase, and robots.txt disallows crawlers.")
    elif args.allow_indexing:
        print("OPEN SITE, and robots.txt invites search engines in. Anyone can read it.")
    else:
        print("OPEN SITE -- anyone with the URL can read it. robots.txt asks crawlers to stay out, "
              "which is a request, not a lock. Pass a passphrase to gate it.")


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


def main():
    """Serves the site (`python -m corpus.web.public`), or writes it out
    (`python -m corpus.web.public build --out _site`)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = ap.add_subparsers(dest="command")
    build = commands.add_parser("build", help="write the site out as static files (the archive)")
    build.add_argument("--out", default="_site", help="output directory (default: _site)")
    build.add_argument("--base-path", default=None, help="URL path prefix the site will be served under (default: empty when a CNAME sets a custom domain, else derived from $GITHUB_REPOSITORY, else empty)")
    build.add_argument("--password", default=None, help="passphrase to encrypt every page behind (default: $SITE_PASSWORD, else deploy/site.env)")
    build.add_argument("--no-password", action="store_true",
                       help="publish an open, ungated site, even where the build being replaced was gated")
    build.add_argument("--allow-indexing", action="store_true",
                       help="let search engines index the site (open builds only; the default asks them not to)")
    build.add_argument("--include-unpublished", action="store_true",
                       help="build every parsed document, not only the works put on the site from the dashboard "
                           "-- an archive of everything held rather than a copy of the site")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--open", action="store_true",
                    help="serve with no passphrase at all -- a deliberate choice, "
                         "not something to arrive at by forgetting to set one")
    ap.add_argument("--allow-indexing", action="store_true",
                    help="let search engines index an open site (the default asks them not to)")
    args = ap.parse_args()
    if args.command == "build":
        _build(args)
        return


    configure(site_env.site_password(), args.open, args.allow_indexing)
    if not gated():
        print("OPEN SITE -- anyone who can reach this can read the whole corpus.", file=sys.stderr)

    works = db.published_works(BASE_DIR)
    print(f"{len(works)} work(s) published.")
    if not _INDEX.available():
        print("No search index yet -- build it from the dashboard.", file=sys.stderr)

    _start_render_worker()

    def warm_search():
        # The first search reads the vocabulary and starts the semantic
        # model, most of a second; better at startup than on a reader.
        try:
            _INDEX.search("act")
        except Exception:
            pass
    import threading
    threading.Thread(target=warm_search, name="warm-search", daemon=True).start()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
