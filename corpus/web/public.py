"""
The public site: corpusvic.au, served live from the database.

A passphrase is required currently to gain access to the public view. All the pages are encrypted behind this.

"""
import argparse
import base64
import hashlib
import hmac
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from corpus.web import dashboard
from corpus.search import search
from corpus.search import search_view
from corpus.publishing import html_view, reader, site_env
from corpus import PROJECT_ROOT
from corpus.storage import db
from corpus.publishing.site_crypto import ROBOTS_TXT, ROBOTS_TXT_ALLOW_ALL
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
    at the work's own name, and an older reprint keeps its versioned one
    so a citation to a point in time keeps meaning that point."""
    from corpus.exporters.export_static_site import site_slugs

    works = db.published_works(BASE_DIR)
    candidates = [
        slug for slug in dashboard.discover_slugs()
        if (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists()
        and split_document_slug(slug)[0] in works
    ]
    return site_slugs(sorted(candidates))


def _resolve(site_slug: str) -> str:
    """The parse this address is served from, or a 404 saying which of
    the two reasons it is."""
    for slug, address in _published_slugs().items():
        if address == site_slug:
            return slug
    raise HTTPException(404, f"{site_slug!r} is not published here.")


def _page(title: str, body: str, base_url: "str | None" = None,
          reader_layout: bool = False, query: str = "",
          canonical: "str | None" = None) -> HTMLResponse:
    """Built through the archive's own page wrapper, so the site's legal
    notice cannot be left off a page by forgetting it here. What differs
    is that this side has a server: hover cards are rendered on demand
    rather than pre-built, and the search box has somewhere to go."""
    from corpus.exporters.export_static_site import _page as finished_page

    return HTMLResponse(finished_page(
        title, body, base_url=base_url, reader=reader_layout,
        search_url="/search", preview_source="api", canonical=canonical))


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def landing():
    from corpus.exporters.export_static_site import _landing_page_html

    published = []
    for slug, address in sorted(_published_slugs().items()):
        status = dashboard.act_status(slug)
        published.append({
            "slug": slug, "site_slug": address,
            "title": dashboard._act_title(slug), "kind": status["kind"],
            "as_at": status["version_as_at"],
            "checked_provisions": 0, "total_provisions": 0,
        })
    return HTMLResponse(_landing_page_html(published, "", search_url="/search",
                                          preview_source="api"))


@app.get("/browse/{site_slug}")
def browse_redirect(site_slug: str):
    return RedirectResponse(f"/browse/{site_slug}/", status_code=307)


@app.get("/browse/{site_slug}/", response_class=HTMLResponse)
def contents(site_slug: str):
    slug = _resolve(site_slug)
    body = reader.contents_page(
        dashboard, slug, f"/browse/{site_slug}", show_review_badge=False,
        notice=_partial_notice(slug),
        related=[
            {"slug": d["slug"], "kind": d["kind"], "title": dashboard._act_title(d["slug"]),
             "href": f"/browse/{_published_slugs()[d['slug']]}/"}
            for d in dashboard.related_documents(slug)
            if d["slug"] in _published_slugs()
        ],
    )
    return _page(dashboard._act_title(slug), body, f"/browse/{site_slug}")


@app.get("/browse/{site_slug}/section/{section_slug}", response_class=HTMLResponse)
def section(site_slug: str, section_slug: str):
    slug = _resolve(site_slug)
    body = reader.section_page(
        dashboard, slug, f"/browse/{site_slug}", section_slug,
        show_review_badge=False, notice=_unverified_notice(slug, section_slug))
    if body is None:
        raise HTTPException(404, f"No such provision in {site_slug!r}.")
    return _page(dashboard._act_title(slug), body, f"/browse/{site_slug}", reader_layout=True,
                 canonical=f"/browse/{site_slug}/section/{section_slug}")


@app.get("/browse/{site_slug}/endnotes", response_class=HTMLResponse)
def endnotes(site_slug: str):
    slug = _resolve(site_slug)
    body = reader.endnotes_page(dashboard, slug, f"/browse/{site_slug}")
    if body is None:
        raise HTTPException(404, f"{site_slug!r} has no endnotes.")
    return _page(f"{dashboard._act_title(slug)} — Endnotes", body, f"/browse/{site_slug}")


@app.get("/api/browse/{site_slug}/preview")
def preview(site_slug: str, section: "str | None" = None, fragment: "str | None" = None):
    """The hover cards. Behind the same publication check as the pages --
    without it, hovering a cross-reference would hand out the full text
    of a provision the site refuses to serve."""
    slug = _resolve(site_slug)
    card = html_view.render_preview(dashboard._parsed(slug),
                                    dashboard._act_title(slug), section, fragment)
    if card is None:
        raise HTTPException(404, "No card for that.")
    return card


def _checked_pages(slug: str) -> tuple:
    from corpus.exporters.export_static_site import approved_page_slugs
    from corpus.review.review import group_into_units

    nodes, _unattached, _hierarchy = dashboard._current_nodes(slug)
    page_index = dashboard._page_index(slug)
    all_pages = set(page_index["by_node_index"].values())
    checked = approved_page_slugs(nodes, group_into_units(nodes), page_index["by_node_index"])
    return checked, all_pages


def _partial_notice(slug: str) -> "str | None":
    from corpus.exporters.export_static_site import _partial_notice_html

    checked, all_pages = _checked_pages(slug)
    if checked == all_pages:
        return None
    return _partial_notice_html(len(checked), len(all_pages))


def _unverified_notice(slug: str, section_slug: str) -> "str | None":
    from corpus.exporters.export_static_site import _unverified_notice_html

    checked, _all_pages = _checked_pages(slug)
    return None if section_slug in checked else _unverified_notice_html()


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


@app.get("/search", response_class=HTMLResponse)
def search_page(q: str = "", superseded: str = "", bills: str = "", em: str = "",
                offset: int = 0):
    """A plain page for a plain GET form, so search works with
    JavaScript off -- which for a reference work about the law is worth
    more than a type-ahead.

    The three scope parameters default to off, which is what makes an
    ordinary search a search of the law as it stands. See
    corpus/search.py's Scope."""
    scope = search.Scope.from_params(
        {"superseded": superseded, "bills": bills, "em": em})
    body = search_view.page_body(_INDEX, q, scope, offset,
                                 action="/search", unavailable=search.SearchUnavailable)
    return _page("Search", body, query=q)


@app.get("/api/search")
def search_api(q: str = "", superseded: str = "", bills: str = "", em: str = "",
               offset: int = 0, limit: int = 20):
    scope = search.Scope.from_params(
        {"superseded": superseded, "bills": bills, "em": em})
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
# Running it
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--open", action="store_true",
                    help="serve with no passphrase at all -- a deliberate choice, "
                         "not something to arrive at by forgetting to set one")
    ap.add_argument("--allow-indexing", action="store_true",
                    help="let search engines index an open site (the default asks them not to)")
    args = ap.parse_args()

    configure(site_env.site_password(), args.open, args.allow_indexing)
    if not gated():
        print("OPEN SITE -- anyone who can reach this can read the whole corpus.", file=sys.stderr)

    works = db.published_works(BASE_DIR)
    print(f"{len(works)} work(s) published.")
    if not _INDEX.available():
        print("No search index yet -- build it from the dashboard.", file=sys.stderr)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
