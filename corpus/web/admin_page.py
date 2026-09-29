"""The admin screens' pages, served so a browser never keeps old CSS.

The public site puts a content hash on every asset URL. The admin pages
linked static/admin/<name>.css at a fixed URL with no cache headers, and
a browser kept a long-unchanged stylesheet "fresh" on its own heuristics:
after the redesign the dashboard went on showing the old palette until a
hard refresh.
"""
import hashlib
import re
from pathlib import Path

from fastapi.responses import HTMLResponse

from corpus import PROJECT_ROOT

_STATIC = PROJECT_ROOT / "static"
_memo: dict = {}
_ADMIN_CSS_RE = re.compile(r'(href="[^"]*static/admin/[\w-]+\.css)(")')


def static_version() -> str:
    """A hash of static/admin/ and static/site/ -- the admin stylesheets
    and the tokens.css they import -- read again only when a file's size
    or mtime changes."""
    files = sorted(p for d in ("admin", "site") for p in (_STATIC / d).rglob("*.css") if p.is_file())
    listing = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _memo.get("listing") != listing:
        digest = hashlib.sha1()
        for p in files:
            digest.update(p.name.encode())
            digest.update(p.read_bytes())
        _memo.update(listing=listing, version=digest.hexdigest()[:10])
    return _memo["version"]


def admin_html(path: Path) -> HTMLResponse:
    """The page, its stylesheet links carrying static_version(): a changed
    stylesheet is a URL the browser has never cached. The page itself is
    re-checked every time, so it always names the current version."""
    html = Path(path).read_text(encoding="utf-8")
    html = _ADMIN_CSS_RE.sub(rf"\1?v={static_version()}\2", html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


async def revalidate_static(request, call_next):
    """Middleware: anything under /static/ is re-checked (a cheap 304 when
    unchanged). tokens.css is reached by an @import whose URL cannot carry
    the version."""
    response = await call_next(request)
    if "/static/" in request.url.path and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response
