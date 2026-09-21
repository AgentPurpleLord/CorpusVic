"""
An admin dashboard for the parsing, review and publishing pipeline.
The user can:
    Add new Acts, Bills or Explanatory Memorandums.
    Link Acts, Bills and Explanatory Memorandums.
    Review Acts, Bills or Explanatory Memorandums.
    Push or pull changes from the remote repository.
    Restart the Dashboard.
    Restart the public site.
    Publish Acts or de-publish Acts.

Usage:
    python dashboard.py
    python dashboard.py --host 0.0.0.0 --port 8000
    python dashboard.py --host 0.0.0.0 --port 8000 --username alice --password <a-real-password>
    python dashboard.py --base-path /admin        # behind a proxy, at corpusvic.au/admin

--base-path is the path this is served under when it shares a domain
with something else -- the published site at corpusvic.au, with this at
corpusvic.au/admin. The app is mounted there (see serving_app), so no
route below mentions the prefix; what carries it is every URL handed
back to a browser, each of which goes through _url. The login password
for this is its own, and deliberately not the passphrase that gates the
published site -- see deploy/README.md.

--host 0.0.0.0 is what makes this reachable from outside the machine it
runs on (the default, 127.0.0.1, is loopback-only). Anything bound to
0.0.0.0 is reachable by anyone who can reach the host on that port, and
this dashboard can both upload PDFs and shell out to the pipeline scripts
-- so it always requires a login (username + password, --username/
--password or the DASHBOARD_USERNAME/DASHBOARD_PASSWORD env vars). With
neither set, the very first run falls back to a placeholder
(AgentPurpleLord/password) and immediately forces a password change on
first login before anything else in the dashboard works -- see
_resolve_auth. That changed password (a salted hash, never the plain
password) is written to .dashboard_auth.json (gitignored) so it survives
a restart; pass --no-auth instead to disable the login gate entirely,
and only ever on a loopback-only run.

The password is never stored or compared in plain text (PBKDF2-HMAC-
SHA256, constant-time comparison); a successful login gets a random
session token in an httponly cookie -- the password itself never touches
the browser again after that one request. Repeated failed logins from
the same client lock that client out for a while (see
_LOCKOUT_THRESHOLD/_LOCKOUT_WINDOW_SECONDS below).

review.py itself is unchanged and still perfectly usable standalone
(`python review.py <act>`) -- this dashboard instead launches it as a
child process on an OS-assigned loopback port per Act (started lazily, on
first "Review" click, and kept running for the rest of this dashboard
process's life) and reverse-proxies /review/<act>/* to it. That keeps
review.py's existing single-Act-per-process design (and its whole test
suite) untouched while still letting several Acts be reviewed at once
through the one exposed port. static/review.html's own fetch() calls are
relative ("api/...", not "/api/...") specifically so the same file works
both ways: served at "/" by review.py directly, or at "/review/<act>/" by
this proxy.

The "Add new Act/Bill/EM" and "Export" actions all just shell out to the
existing run_pipeline.py/run_em_pipeline.py/export_akn.py/
export_markdown.py/run_bill_linking.py scripts (capturing their stdout/
stderr to show as a log) rather than reimplementing their logic here --
they're already the tested, documented entry points for those jobs.
"""
from corpus import PROJECT_ROOT
import argparse
import hashlib
import hmac
import html
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import sqlite3
import socket
import subprocess
from datetime import datetime, timezone
import sys
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Mount

from corpus.search import search
from corpus.review import review_sync, sync
from corpus.publishing import html_view, reader
from corpus.storage import db
from corpus.domain import commentary, diffing
from corpus.domain.act_registry import load_act_registry
from corpus.domain.amendments import build_amendment_index, summarise_by_act
from corpus.domain.commentary import build_commentary_index
from corpus.parsing.extract import slugify
from corpus.review.link_targets import load_known_acts
from corpus.domain.profiles import available_profiles, profile_for
from corpus.ai.backend import OllamaBackend, pull_model
from corpus.parsing.versions import document_slug, read_front_matter, split_document_slug
from corpus.review.review import _resume_point, build_current_nodes, group_into_units

BASE_DIR = PROJECT_ROOT
STATIC_DIR = BASE_DIR / "static"
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _validate_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug):
        raise HTTPException(400, f"Invalid slug: {slug!r}")
    return slug


# ---------------------------------------------------------------------------
# Act discovery / status -- pure filesystem inspection, no subprocess.
# ---------------------------------------------------------------------------


def discover_slugs() -> list[str]:
    slugs = set()
    acts_dir = BASE_DIR / "acts"
    if acts_dir.exists():
        for p in acts_dir.iterdir():
            if p.suffix.lower() == ".pdf":
                slugs.add(slugify(p.stem))
            elif p.is_dir():
                # A work directory: each PDF in it is one version of that
                # work as this pipeline reads it, addressed by the work's
                # name and its own version number rather than by its
                # filename (see corpus/versions.py).
                for pdf in p.glob("*.pdf"):
                    version = _pdf_version(pdf)
                    # A Bill or an EM filed with the Act it became belongs
                    # to the work's history without being a point on its
                    # timeline -- it keeps its own filename as its slug.
                    slugs.add(document_slug(p.name, version) if version is not None else slugify(pdf.stem))
    parsed_dir = BASE_DIR / "data" / "parsed"
    if parsed_dir.exists():
        for p in parsed_dir.glob("*.json"):
            slugs.add(p.stem)
    return sorted(slugs)


_pdf_version_cache: dict[tuple, "int | None"] = {}


def _pdf_version(pdf: Path) -> "int | None":
    """This PDF's version number (as it states it -- see
    corpus/versions.py), cached against the file's own
    mtime and size -- discover_slugs runs on every dashboard load, and
    reading the front matter of every version of every Act on each one
    would be paying repeatedly for something that only changes when a file
    does."""
    try:
        st = pdf.stat()
    except OSError:
        return None
    key = (str(pdf), st.st_mtime_ns, st.st_size)
    if key not in _pdf_version_cache:
        _pdf_version_cache[key] = read_front_matter(pdf).get("version")
    return _pdf_version_cache[key]


def act_status(slug: str, publication: "dict | None" = None) -> dict:
    parsed_path = BASE_DIR / "data" / "parsed" / f"{slug}.json"
    work, version = split_document_slug(slug)
    # Passed in when a caller is asking about every document at once, so
    # the one small table is read once rather than per document.
    if publication is None:
        publication = db.load_publication(BASE_DIR)
    status = {
        "slug": slug,
        "has_pdf": _find_source_pdf(slug) is not None,
        # Whether this work is on the public site. Per work, so every
        # reprint of an Act answers the same -- see corpus/db.py's
        # publication table for why that is the only coherent key.
        "published": bool(publication.get(work, False)),
        # A version of a work, or a document in its own right. "work" is
        # the Act itself and is the same for all its versions; "version" is
        # None for a Bill, an EM, or an Act not being version-tracked.
        "work": work,
        "version": version,
        "version_as_at": None,
        # Whether this Act has a pattern profile of its own to pass to
        # run_pipeline.py -- the reparse modal pre-fills it, since
        # forgetting it silently produces a worse parse (the Criminal
        # Procedure Act's own Parts fall through to generic heading_group
        # nodes without it -- see its profile's own comment) rather than
        # any kind of error. Looked up under the work first: how an Act
        # numbers its Parts is a fact about the Act, not about one reprint
        # of it, so one profile serves all its versions.
        # The profile this document should be parsed with, by name --
        # not merely whether one exists. The re-parse dialog pre-fills
        # this field, and filling it with the slug meant a versioned Act
        # was handed "criminal-procedure-act-v114", which is not a
        # profile that exists: its Parts stopped matching and Chapter 2
        # swallowed Part 2.1's heading, with nothing to say why.
        "profile": profile_for(slug, BASE_DIR),
        "parsed": parsed_path.exists(),
        # "act" / "bill" / "em" -- what the pipeline recorded when it
        # parsed this one (filled in below, from the parse this function
        # already reads). Surfaced so a dashboard card says which of the
        # three it is, since all three browse and review the same way now.
        "kind": "act",
        "node_count": None,
        "unit_count": None,
        "reviewed_units": None,
        "review_status": "not-parsed",
        "akn_exported": (BASE_DIR / "data" / "akn" / f"{slug}.xml").exists(),
        "markdown_exported": (BASE_DIR / "data" / "markdown" / slug / "index.md").exists(),
    }
    if not parsed_path.exists():
        return status
    data = json.loads(parsed_path.read_text(encoding="utf-8"))
    status["kind"] = data.get("document_type") or "act"
    status["version_as_at"] = (data.get("version") or {}).get("as_at_printed")
    nodes = data.get("nodes", [])
    units = group_into_units(nodes)
    status["node_count"] = len(nodes)
    status["unit_count"] = len(units)

    verified = db.load_verified(slug, base_dir=BASE_DIR)
    resume_unit = _resume_point(units, list(verified))  # copy: _resume_point may trim its list arg
    status["reviewed_units"] = min(resume_unit, len(units))
    if not units:
        status["review_status"] = "reviewed"
    elif resume_unit >= len(units):
        status["review_status"] = "reviewed"
    elif resume_unit > 0:
        status["review_status"] = "in-progress"
    else:
        status["review_status"] = "not-started"
    return status


# ---------------------------------------------------------------------------
# review.py child-process management + reverse proxy
# ---------------------------------------------------------------------------

_review_procs: dict[str, dict] = {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ensure_review_process(slug: str) -> int:
    entry = _review_procs.get(slug)
    if entry and entry["proc"].poll() is None:
        return entry["port"]

    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "corpus.review.review", slug, "--port", str(port)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _review_procs[slug] = {"proc": proc, "port": port}

    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise HTTPException(500, f"review.py for {slug!r} exited immediately (exit code {proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return port
        except OSError:
            time.sleep(0.1)
    raise HTTPException(500, f"review.py for {slug!r} didn't start in time")


def _shutdown_review_processes() -> None:
    for entry in _review_procs.values():
        if entry["proc"].poll() is None:
            entry["proc"].terminate()


def _kill_review_process(slug: str) -> None:
    """Stops this Act's running review.py child (if any) and forgets it,
    so the next "Review" click starts a fresh one -- see reparse_act,
    which calls this after successfully regenerating that Act's parse."""
    entry = _review_procs.pop(slug, None)
    if entry and entry["proc"].poll() is None:
        entry["proc"].terminate()


# ---------------------------------------------------------------------------
# run_ai_review.py child-process management -- the whole-document AI scan
# (see corpus/ai/scan.py). Unlike review.py's child above, this one
# isn't proxied: it has no HTTP server of its own, just stdout progress
# and rows it writes to data/legislation.db as it goes (see
# db.ai_scan_progress), which is what the dashboard polls instead.
# ---------------------------------------------------------------------------

_ai_scan_procs: dict[str, dict] = {}

# run_ai_review.py's own stdout/stderr (progress lines, and -- critically
# -- the exact OllamaUnavailable message when Ollama isn't installed,
# running, or missing its model) has nowhere else to go: unlike
# review.py's child, this one has no HTTP server of its own for a
# reviewer to open directly. Discarding it (as review.py's own child
# does, harmlessly, since that one's success is checked by whether its
# port comes up) would leave a failed scan saying only "exit code 1"
# with no way to tell why -- so it's captured to a small per-Act log
# file instead, and the progress endpoint below hands back its tail.
_AI_SCAN_LOG_DIR = BASE_DIR / "data" / "ai_scan_logs"


def _ai_scan_log_path(slug: str) -> Path:
    return _AI_SCAN_LOG_DIR / f"{slug}.log"


def _shutdown_ai_scan_processes() -> None:
    for entry in _ai_scan_procs.values():
        if entry["proc"].poll() is None:
            entry["proc"].terminate()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Legislation pipeline dashboard")

# Where this app is mounted, when it is not at the domain root --
# "/admin" for corpusvic.au/admin, "" for a bare host or a subdomain of
# its own. Set once at startup by _configure_base_path.
#
# Routes themselves never mention it: the app is mounted under it (see
# serving_app), and Starlette strips the prefix before a route sees the
# path. What does need it is every URL this app *emits* -- a redirect, a
# link in a page it renders -- because those go back to a browser that
# knows only the outside address. Each one goes through _url.
#
# The browser-side half is the other way round: static/dashboard.html and
# static/review.html ask for "api/..." rather than "/api/...", so the
# page's own address supplies the prefix and neither file has to be told
# what it is.
_BASE_PATH = ""


def _configure_base_path(base_path: str) -> str:
    """Normalises and records the path this app is served under. Returns
    it. "/admin/", "admin" and "/admin" all mean the same thing; "" and
    "/" both mean the domain root."""
    global _BASE_PATH
    cleaned = "/" + (base_path or "").strip().strip("/")
    _BASE_PATH = "" if cleaned == "/" else cleaned
    return _BASE_PATH


def _url(path: str) -> str:
    """An address this app hands to a browser, as the browser will see
    it. Always call this rather than writing "/login" directly: under a
    base path a bare "/login" escapes the mount and lands on whatever
    else is served at the domain root."""
    return f"{_BASE_PATH}{path}"


def serving_app():
    """What uvicorn is given: this app at the domain root, or mounted
    under its base path.

    A Starlette mount strips the prefix from the incoming path before
    routing, so every route below is written as though it were at the
    root either way -- and the review proxy, which forwards whatever path
    it is handed to a child review.py process, keeps working unchanged."""
    if not _BASE_PATH:
        return app
    outer = Starlette(routes=[Mount(_BASE_PATH, app=app)])
    return outer

# static/site/ is the published site's template -- the page shell, its
# stylesheets, its browser-side scripts and Junicode (see
# corpus/html_view.py's TEMPLATE_DIR). Mounted at the same "/assets"
# every page's asset URLs are built from, so a browse page served here
# loads exactly the files export_static_site.py publishes. StaticFiles
# resolves the path itself and refuses to escape the directory, which is
# what the hand-rolled /fonts route this replaces had to check for.
app.mount("/assets", StaticFiles(directory=html_view.TEMPLATE_DIR), name="assets")

# ---------------------------------------------------------------------------
# Username/password login: sessions are random server-side tokens (the
# password itself never rides in a cookie), password comparison is a
# constant-time compare against a PBKDF2 hash (never a plain-text
# comparison), and repeated failures from one client are locked out for a
# while -- see _configure_auth/_check_credentials/_is_locked_out below.
# ---------------------------------------------------------------------------

_COOKIE_NAME = "dashboard_session"
_SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30
_LOCKOUT_THRESHOLD = 5
_LOCKOUT_WINDOW_SECONDS = 15 * 60
_PBKDF2_ITERATIONS = 200_000
_MIN_PASSWORD_LENGTH = 8
_AUTH_STORE_PATH = BASE_DIR / ".dashboard_auth.json"
# A publishable placeholder, not a real credential -- see _resolve_auth: it
# is only ever the *initial* state on a brand-new install, and every login
# with it forces an immediate password change before anything else works.
_DEFAULT_USERNAME = "AgentPurpleLord"
_DEFAULT_PASSWORD = "password"

_DASHBOARD_USERNAME: str | None = None
_PASSWORD_SALT: bytes | None = None
_PASSWORD_HASH: bytes | None = None
_MUST_CHANGE_PASSWORD = False
_SESSIONS: dict[str, float] = {}  # session token -> expiry (epoch seconds)
_FAILED_ATTEMPTS: dict[str, list[float]] = {}  # client IP -> failure timestamps


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)


def _set_auth_state(username: str, salt: bytes, password_hash: bytes, must_change: bool) -> None:
    global _DASHBOARD_USERNAME, _PASSWORD_SALT, _PASSWORD_HASH, _MUST_CHANGE_PASSWORD
    _DASHBOARD_USERNAME = username
    _PASSWORD_SALT = salt
    _PASSWORD_HASH = password_hash
    _MUST_CHANGE_PASSWORD = must_change


def _configure_auth(username: str, password: str, must_change: bool = False) -> None:
    salt = secrets.token_bytes(16)
    _set_auth_state(username, salt, _hash_password(password, salt), must_change)


def _load_auth_store() -> dict | None:
    """Reads the previous run's login state (username + salted password
    hash -- never the plain password) from .dashboard_auth.json, so a
    password set via the forced first-login change (or later, via "change
    password") survives a restart instead of reverting to the built-in
    default every time."""
    if not _AUTH_STORE_PATH.exists():
        return None
    try:
        data = json.loads(_AUTH_STORE_PATH.read_text(encoding="utf-8"))
        return {
            "username": data["username"],
            "salt": bytes.fromhex(data["salt"]),
            "hash": bytes.fromhex(data["hash"]),
            "must_change_password": bool(data.get("must_change_password", False)),
        }
    except (OSError, ValueError, KeyError):
        return None


def _save_auth_store() -> None:
    _AUTH_STORE_PATH.write_text(
        json.dumps(
            {
                "username": _DASHBOARD_USERNAME,
                "salt": _PASSWORD_SALT.hex(),
                "hash": _PASSWORD_HASH.hex(),
                "must_change_password": _MUST_CHANGE_PASSWORD,
            }
        ),
        encoding="utf-8",
    )
    try:
        _AUTH_STORE_PATH.chmod(0o600)  # readable/writable by the owner only
    except OSError:
        pass  # best-effort -- e.g. unsupported on the host filesystem


def _check_credentials(username: str, password: str) -> bool:
    if _DASHBOARD_USERNAME is None:
        return False
    username_ok = hmac.compare_digest(username.encode("utf-8"), _DASHBOARD_USERNAME.encode("utf-8"))
    password_ok = hmac.compare_digest(_hash_password(password, _PASSWORD_SALT), _PASSWORD_HASH)
    return username_ok and password_ok


# The addresses a request arrives from when something on this machine is
# forwarding it. Caddy terminates TLS and proxies over loopback, so every
# request's immediate peer is one of these and none of them identifies
# anybody.
_LOOPBACK = {"127.0.0.1", "::1"}


def _client_ip(request: Request) -> str:
    """Who a request is actually from, for the login lockout to count.

    Behind a reverse proxy the peer is always loopback, so counting that
    made the lockout global: five wrong guesses from anyone on earth
    locked out everyone for fifteen minutes. On a public site sharing one
    passphrase that is a denial of service anybody can perform.

    X-Forwarded-For is a list a client can seed with anything it likes,
    but each proxy appends the peer it actually saw -- so the last entry
    is the one Caddy added and the only one not under the client's
    control. Read only when the peer really is loopback: anywhere else,
    the header is just something a stranger sent."""
    peer = request.client.host if request.client else None
    if peer in _LOOPBACK:
        forwarded = request.headers.get("x-forwarded-for", "")
        nearest = forwarded.rsplit(",", 1)[-1].strip()
        if nearest:
            return nearest
    return peer or "unknown"


def _is_locked_out(ip: str) -> bool:
    cutoff = time.time() - _LOCKOUT_WINDOW_SECONDS
    attempts = [t for t in _FAILED_ATTEMPTS.get(ip, []) if t > cutoff]
    _FAILED_ATTEMPTS[ip] = attempts
    return len(attempts) >= _LOCKOUT_THRESHOLD


def _record_failed_login(ip: str) -> None:
    _FAILED_ATTEMPTS.setdefault(ip, []).append(time.time())


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = time.time() + _SESSION_LIFETIME_SECONDS
    return token


def _session_is_valid(token: str | None) -> bool:
    if not token:
        return False
    expiry = _SESSIONS.get(token)
    if expiry is None:
        return False
    if expiry < time.time():
        del _SESSIONS[token]
        return False
    return True


_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Sign in</title>
<style>
/* Honours the same localStorage["reviewTheme"] preference the rest of the
   GUI uses (no toggle of its own -- this screen is on the way to the
   dashboard, where the toggle lives). */
:root{color-scheme:light;--bg:#f5f5f4;--panel:#fff;--fg:#1a1a1a;--border:#d7d7d7;--accent:#2b6cb0;--danger:#b91c1c}
:root[data-theme="dark"]{color-scheme:dark;--bg:#16181d;--panel:#1e2126;--fg:#e8e8ea;--border:#34383f;--accent:#5b9bd9;--danger:#f87171}
body{font-family:ui-sans-serif,system-ui,sans-serif;background:var(--bg);color:var(--fg);display:flex;
  align-items:center;justify-content:center;height:100vh;margin:0}
form{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:24px;width:280px}
h1{font-size:15px;margin:0 0 14px}
input{width:100%;padding:7px 9px;border:1px solid var(--border);border-radius:6px;font-size:13px;box-sizing:border-box;
  margin-bottom:8px;background:var(--panel);color:var(--fg)}
button{margin-top:6px;width:100%;padding:8px;border:0;border-radius:6px;background:var(--accent);color:#fff;
  font-size:13px;cursor:pointer}
#err{color:var(--danger);font-size:12px;min-height:16px;margin-top:6px}
</style>
<script>
try { if (localStorage.getItem("reviewTheme") === "dark") document.documentElement.dataset.theme = "dark"; } catch (e) {}
</script></head><body>
<form id="f">
  <h1>Legislation pipeline dashboard</h1>
  <input type="text" id="username" placeholder="Username" autocomplete="username" autofocus>
  <input type="password" id="password" placeholder="Password" autocomplete="current-password">
  <button type="submit">Sign in</button>
  <div id="err"></div>
</form>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await fetch("api/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    }),
  });
  if (res.ok) { location.href = "./"; return; }
  const err = document.getElementById("err");
  err.textContent = res.status === 429
    ? "Too many failed attempts -- try again later."
    : "Invalid username or password.";
});
</script>
</body></html>"""


_CHANGE_PASSWORD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Set a new password</title>
<style>
body{font-family:ui-sans-serif,system-ui,sans-serif;background:#f5f5f4;display:flex;
  align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#fff;border:1px solid #d7d7d7;border-radius:8px;padding:24px;width:300px}
h1{font-size:15px;margin:0 0 8px}
p{font-size:12.5px;color:#6b6b6b;margin:0 0 14px}
input{width:100%;padding:7px 9px;border:1px solid #d7d7d7;border-radius:6px;font-size:13px;box-sizing:border-box;margin-bottom:8px}
button{margin-top:6px;width:100%;padding:8px;border:0;border-radius:6px;background:#2b6cb0;color:#fff;
  font-size:13px;cursor:pointer}
#err{color:#b91c1c;font-size:12px;min-height:16px;margin-top:6px}
</style></head><body>
<form id="f">
  <h1>Set a new password</h1>
  <p>You're signed in with the default password -- choose your own before continuing.</p>
  <input type="password" id="current" placeholder="Current password" autocomplete="current-password" autofocus>
  <input type="password" id="new1" placeholder="New password (min. 8 characters)" autocomplete="new-password">
  <input type="password" id="new2" placeholder="Confirm new password" autocomplete="new-password">
  <button type="submit">Set password</button>
  <div id="err"></div>
</form>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("err");
  const new1 = document.getElementById("new1").value;
  const new2 = document.getElementById("new2").value;
  if (new1 !== new2) { err.textContent = "New passwords don't match."; return; }
  const res = await fetch("api/change-password", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({current_password: document.getElementById("current").value, new_password: new1}),
  });
  if (res.ok) { location.href = "./"; return; }
  const data = await res.json().catch(() => ({}));
  err.textContent = data.detail || "Couldn't change password.";
});
</script>
</body></html>"""


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if _DASHBOARD_USERNAME is None:
        return await call_next(request)

    # Without the base path this app is mounted under. request.url.path
    # is the address the browser asked for, prefix and all, while every
    # comparison below is written in this app's own terms -- so under
    # /admin the login page did not match "/login", was treated as
    # protected, and redirected to itself for ever.
    path = _app_path(request)
    if path in ("/login", "/api/login"):
        return await call_next(request)

    if not _session_is_valid(request.cookies.get(_COOKIE_NAME)):
        if path.startswith("/api/") or path.startswith("/review/"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse(_url("/login"))

    if _MUST_CHANGE_PASSWORD and path not in ("/change-password", "/api/change-password", "/api/logout"):
        if path.startswith("/api/") or path.startswith("/review/"):
            return JSONResponse({"detail": "password change required"}, status_code=403)
        return RedirectResponse(_url("/change-password"))

    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.get("/login")
def login_page():
    return HTMLResponse(_LOGIN_HTML)


@app.get("/change-password")
def change_password_page():
    return HTMLResponse(_CHANGE_PASSWORD_HTML)


def _app_path(request: Request) -> str:
    """The request path as this app's own routes see it: whatever the
    browser asked for, with the base path taken off the front."""
    path = request.url.path
    if _BASE_PATH and (path == _BASE_PATH or path.startswith(_BASE_PATH + "/")):
        return path[len(_BASE_PATH):] or "/"
    return path


def _cookie_path() -> str:
    """The session cookie's own path: the base path this app is served
    under, so that on a domain shared with the public site the admin
    cookie is simply never sent with a request for a published page."""
    return _BASE_PATH or "/"


def _served_over_https(request: Request) -> bool:
    """Whether the browser reached us over HTTPS, which behind a reverse
    proxy is what the proxy says rather than what this process sees --
    Caddy terminates TLS and forwards plain HTTP to loopback, so
    request.url.scheme is "http" on a site that is HTTPS-only."""
    forwarded = request.headers.get("x-forwarded-proto", "")
    return (forwarded.split(",")[0].strip() or request.url.scheme) == "https"


def _set_session_cookie(resp, request: Request) -> None:
    resp.set_cookie(
        _COOKIE_NAME, _new_session(), httponly=True, samesite="lax",
        max_age=_SESSION_LIFETIME_SECONDS,
        path=_cookie_path(),
        # Never sent in the clear where the connection was not: a plain
        # http:// run (a loopback dev session) still has to work, so this
        # follows the connection rather than being hard-coded on.
        secure=_served_over_https(request),
    )


@app.post("/api/login")
def do_login(req: LoginRequest, request: Request):
    ip = _client_ip(request)
    if _is_locked_out(ip):
        raise HTTPException(429, "Too many failed attempts -- try again later.")
    if not _check_credentials(req.username, req.password):
        _record_failed_login(ip)
        raise HTTPException(401, "Invalid username or password")
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, request)
    return resp


@app.post("/api/change-password")
def do_change_password(req: ChangePasswordRequest):
    if not _check_credentials(_DASHBOARD_USERNAME, req.current_password):
        raise HTTPException(401, "Current password is incorrect")
    if len(req.new_password) < _MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"New password must be at least {_MIN_PASSWORD_LENGTH} characters")
    if req.new_password in (req.current_password, _DEFAULT_PASSWORD):
        raise HTTPException(400, "Choose a password different from your current one and the default")
    _configure_auth(_DASHBOARD_USERNAME, req.new_password, must_change=False)
    _save_auth_store()
    return {"ok": True}


class PushRequest(BaseModel):
    # What the commit will say. Optional: the point of the button is not
    # having to think of one.
    message: "str | None" = None


# ---------------------------------------------------------------------------
# The search index
# ---------------------------------------------------------------------------
#
# Built here because this is the process that writes: the public site
# reads the index and has no business creating one. A full rebuild takes
# a few seconds over the whole corpus, so there is no incremental path --
# it is thrown away and built again whenever what it is built from moves.

_search_lock = threading.Lock()
_search_state: dict = {"running": False, "built_at": None, "error": None, "stats": None}


def _rebuild_search_index() -> None:
    """Rebuilds the index, one at a time.

    The lock is not for safety -- the build writes to one side and moves
    the finished file into place, so a half-built index is never readable
    either way -- but to stop three clicks from doing the same work three
    times."""
    if not _search_lock.acquire(blocking=False):
        return
    _search_state.update({"running": True, "error": None})
    try:
        stats = search.rebuild(BASE_DIR, source=sys.modules[__name__])
        _search_state.update({"stats": stats, "built_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:  # noqa: BLE001 -- reported on the page, not swallowed
        _search_state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _search_state["running"] = False
        _search_lock.release()


def _rebuild_search_index_soon() -> None:
    """In the background, for the things that change what is indexed as a
    side effect of doing something else -- publishing a work, pulling
    somebody else's review work. Nobody should wait on it."""
    threading.Thread(target=_rebuild_search_index, daemon=True).start()


@app.get("/api/search/status")
def search_status():
    """Whether there is an index, and whether it still matches the data.

    Staleness is compared rather than guessed: the signature covers every
    parse file, the review database and which works are published.

    An index built by an older version of the builder is stale too, even
    when the data has not moved. That is not hypothetical: the index
    gained a table of the corpus's own words, and without it typo
    correction silently does nothing -- an index that works, returns
    results, and quietly lacks a feature is the hardest kind of broken to
    notice."""
    path = search.index_path(BASE_DIR)
    built = path.exists()
    stale = None
    if built:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            stored = dict(conn.execute(
                "SELECT key, value FROM meta WHERE key IN ('signature', 'schema_version')"))
            conn.close()
            stale = bool(stored.get("signature")) and (
                stored["signature"] != search.signature(BASE_DIR)
                or stored.get("schema_version") != search.SCHEMA_VERSION)
        except sqlite3.Error as e:
            stale = None
            _search_state["error"] = f"Couldn't read the index: {e}"
    return {
        "built": built,
        "stale": stale,
        "running": _search_state["running"],
        "built_at": _search_state["built_at"],
        "error": _search_state["error"],
        "stats": _search_state["stats"],
    }


@app.post("/api/search/rebuild")
def search_rebuild():
    """Builds the index now. A few seconds over the whole corpus, so it
    is worth waiting for rather than polling."""
    if _search_state["running"]:
        raise HTTPException(409, "A rebuild is already running.")
    _rebuild_search_index()
    if _search_state["error"]:
        raise HTTPException(500, _search_state["error"])
    stats = _search_state["stats"] or {}
    return {"ok": True, "message": (f"Indexed {stats.get('provisions', 0)} provisions from "
                                    f"{stats.get('documents', 0)} document(s)."),
            "stats": stats}


@app.get("/api/sync/status")
def sync_status():
    """Where this checkout stands against GitHub, so the page can say
    what a push would do before anyone presses it -- and whether the code
    answering this request is still the code on disk."""
    state = sync.status(BASE_DIR)
    on_disk = sync.head(BASE_DIR)
    can_restart, why_not = _restart_capability()
    # Two ways the database can be unfit to write over the review files,
    # and one remedy for both. A fresh clone has the text and nothing
    # built from it. A checkout that pulled has text *newer* than what it
    # holds -- and that one is the dangerous half, because everything
    # looks healthy right up until an export deletes the work that
    # arrived. Said here rather than left to be discovered.
    try:
        needs_import = review_sync.unloaded(BASE_DIR) or review_sync.files_are_ahead(BASE_DIR)
    except OSError as e:
        needs_import = f"Couldn't read the review files: {e}"
    return {
        **state,
        "needs_import": needs_import,
        "running_head": _RUNNING_HEAD,
        "checkout_head": on_disk,
        # Both known and different: the checkout has moved since this
        # process started, so what is being served is not what is there.
        "code_stale": bool(_RUNNING_HEAD and on_disk and _RUNNING_HEAD != on_disk),
        "can_restart": can_restart,
        "restart_blocked": why_not,
    }


@app.post("/api/sync/push")
def sync_push(req: PushRequest):
    """Commits whatever has changed under data/ and pushes it.

    Here rather than in review.py because it is about the whole checkout
    rather than one document: a session usually touches more than one,
    and two review processes racing to commit the same database would be
    a way to lose work rather than save it."""
    message = (req.message or "").strip() or _default_commit_message()
    try:
        return sync.push(BASE_DIR, message)
    except sync.SyncError as e:
        raise HTTPException(409, str(e)) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"Couldn't run git: {e}") from e


def _default_commit_message() -> str:
    """Dated, because the point of the button is not having to think of a
    message, and "Review progress" fifty times over is a history nobody
    can read. Anything more specific is the reviewer's to type."""
    return f"Review progress, {datetime.now(timezone.utc):%Y-%m-%d}"


# ---------------------------------------------------------------------------
# Whether the code on disk is still the code that is running
# ---------------------------------------------------------------------------
#
# A pull button without this would be a trap of its own making: git
# succeeds, the checkout moves forward, and this process goes on serving
# whatever it imported at startup. Nothing anywhere would say so -- which
# is exactly the shape of failure the pull was meant to fix.

_RUNNING_HEAD = sync.head(BASE_DIR)

# systemd sets this for every service it starts, and nothing else does.
# Its absence means a restart here would stop the dashboard and leave it
# stopped, which is a worse outcome than asking someone to type the
# command.
_RESTART_UNIT_DIRS = (
    "/etc/systemd/system", "/run/systemd/system",
    "/lib/systemd/system", "/usr/lib/systemd/system",
)
# Restart= values that bring a service back after a deliberate exit.
# "no" (and an absent setting, which means the same) does not.
_RESTARTING_POLICIES = {"always", "on-failure", "on-abnormal", "on-abort", "on-success"}


def _own_unit_name() -> "str | None":
    """This process's systemd unit, read from its own cgroup."""
    try:
        line = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"([\w@.\-\\]+\.service)", line)
    return match.group(1) if match else None


def _restart_capability() -> tuple:
    """Whether pressing a restart button would actually bring the service
    back, and if not, why not.

    Measured rather than assumed: the unit file is read and its Restart=
    setting checked. A service systemd will not restart is one this
    process must not exit from, so the button is disabled with the reason
    rather than offered and found out afterwards."""
    if not os.environ.get("INVOCATION_ID"):
        return False, "This dashboard wasn't started by systemd, so nothing would bring it back."
    unit = _own_unit_name()
    if not unit:
        return False, "Couldn't work out which systemd unit this is."
    # The unit file and its drop-ins, in the order systemd reads them, so
    # that a drop-in overriding Restart= is seen the same way.
    text = ""
    for directory in _RESTART_UNIT_DIRS:
        base = Path(directory) / unit
        candidates = [base] if base.exists() else []
        candidates += sorted(Path(f"{base}.d").glob("*.conf"))
        for path in candidates:
            try:
                text += path.read_text(encoding="utf-8") + "\n"
            except OSError:
                pass
    if not text:
        return False, f"Couldn't read {unit} to check that systemd would restart it."
    # The last one wins, the same way systemd reads them.
    found = re.findall(r"^\s*Restart\s*=\s*(\S+)", text, re.MULTILINE)
    if not found or found[-1] not in _RESTARTING_POLICIES:
        setting = found[-1] if found else "no"
        return False, f"{unit} has Restart={setting}, so stopping here would leave it stopped."
    return True, None


# ---------------------------------------------------------------------------
# The public site's own service
# ---------------------------------------------------------------------------
#
# A different process from this one, on the same box, reading the same
# database. Restarting it is not the same problem as restarting this:
# this one restarts by exiting and letting systemd bring it back, which
# needs no privilege at all, while another unit needs systemd to be asked
# -- and this service runs as `dashboard`, which by default may not ask.
#
# So both the state and the permission are read rather than assumed, and
# the button says which of the two is missing. A button that fails when
# pressed teaches you not to trust the page.

# Overridable because the unit is named by whoever installed it;
# deploy/README.md calls it corpusvic-public.
PUBLIC_UNIT = os.environ.get("PUBLIC_SERVICE_UNIT", "corpusvic-public.service")

# How long to wait for systemctl. A restart of this service is quick, and
# a systemctl that has not answered in ten seconds is one that is not
# going to -- most likely sitting on a polkit prompt nobody can see.
_SYSTEMCTL_TIMEOUT = 10


def _systemctl(*arguments, timeout: int = _SYSTEMCTL_TIMEOUT):
    """Runs systemctl, with sudo when this is not already root.

    `sudo -n`, never interactive: there is no terminal here to type a
    password into, and a sudo that decides to ask for one would hang
    until the timeout rather than fail."""
    command = list(arguments)
    if os.geteuid() != 0:
        command = ["sudo", "-n"] + command
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=timeout, check=False)


def _systemd_is_running() -> bool:
    """Whether systemd is the init system here.

    The same check libsystemd's own sd_booted() makes. Without it, a
    laptop running this dashboard to review documents gets told that a
    service it never installed is in trouble, which is noise dressed as
    a warning."""
    return Path("/run/systemd/system").is_dir()


def _unit_properties(unit: str, *names) -> dict:
    """What systemd says about a unit.

    Reading properties needs no privilege -- which is the whole reason
    the rest of this can work. Returns {} when systemd cannot be asked at
    all, so a caller distinguishes "no answer" from "answered, nothing
    there"."""
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--no-page",
             "--property=" + ",".join(names)],
            capture_output=True, text=True, timeout=_SYSTEMCTL_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _public_service_state() -> dict:
    """What systemd says about the public site's unit."""
    if not _systemd_is_running():
        return {"known": False, "systemd": False,
                "detail": "There is no systemd here, so there is no service to restart."}
    values = _unit_properties(
        PUBLIC_UNIT, "LoadState", "ActiveState", "SubState", "ActiveEnterTimestamp",
        "MainPID", "Restart", "User")
    if not values:
        return {"known": False, "systemd": True, "detail": f"systemd didn't answer about {PUBLIC_UNIT}."}
    try:
        main_pid = int(values.get("MainPID", "0"))
    except ValueError:
        main_pid = 0
    return {
        "known": True,
        "systemd": True,
        # "not-found" is the interesting one: the unit was never
        # installed, which is a different problem from it being stopped.
        "installed": values.get("LoadState") != "not-found",
        "active": values.get("ActiveState") == "active",
        "state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", ""),
        "since": values.get("ActiveEnterTimestamp", ""),
        "main_pid": main_pid,
        "restart_policy": values.get("Restart", ""),
        "unit_user": values.get("User", ""),
    }


def _process_is_ours(pid: int) -> "str | None":
    """Why this pid must not be signalled, or None if it may be.

    Three cheap checks, because signalling the wrong process is the one
    way this can do real damage. systemd's MainPID is authoritative, but
    it is read over a socket and the process can be gone by the time we
    act on it -- and pids are reused."""
    if pid <= 0:
        return "systemd reports no main process for it."
    try:
        owner = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return f"There is no process {pid} any more."
    if owner != os.geteuid():
        return (f"Process {pid} belongs to uid {owner} and this service runs as "
                f"uid {os.geteuid()}, so it cannot signal it.")
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return f"Couldn't read the command line of process {pid}."
    # The module moved into corpus/web/; a checkout that predates the
    # move still launches it by filename, and both spellings are the
    # public site.
    if "corpus.web.public" not in cmdline and "public.py" not in cmdline:
        return f"Process {pid} does not look like the public site."
    return None


def _restart_method() -> tuple:
    """How this process could restart the public site, and why not if it
    could not.

    Advisory. It decides which way to *try* and what the page says while
    idle -- it is never a gate, because the previous version of this was
    one and its single message ("add this sudoers line") was the one
    thing that could not help somebody who had already added it.

    Signalling is preferred and needs no privilege at all: both units run
    as the same user, so this process may send SIGTERM to that one, and
    systemd's Restart= brings it back. That is exactly how the dashboard
    restarts itself, one process over."""
    if not _systemd_is_running():
        return None, "There is no systemd here."
    state = _public_service_state()
    if not state.get("known"):
        return None, state.get("detail", "systemd didn't answer.")
    if not state.get("installed"):
        return None, f"{PUBLIC_UNIT} is not installed on this server."

    if state.get("restart_policy") not in _RESTARTING_POLICIES:
        # Signalling a unit systemd will not bring back is how you take
        # the public site down and leave it down.
        policy = state.get("restart_policy") or "no"
        return "systemctl", (f"{PUBLIC_UNIT} has Restart={policy}, so stopping its process "
                             f"would leave it stopped. Restarting it has to go through systemd.")

    if state.get("active"):
        why_not = _process_is_ours(state.get("main_pid", 0))
        if why_not is None:
            return "signal", None
    else:
        # Nothing to signal. systemd has to start it.
        why_not = f"{PUBLIC_UNIT} is {state.get('state')}, so there is no process to signal."
    return "systemctl", why_not


def _sudo_diagnosis() -> dict:
    """What sudo would say, asked without running anything.

    Only for the details panel. Its answer is evidence, never a decision:
    `sudo -n -l` exits non-zero for several unrelated reasons -- among
    them "a password is required" and this service's own
    NoNewPrivileges=yes -- and reading all of them as "you have not
    written the sudoers line" is what produced a message that repeated
    an instruction back at somebody who had followed it."""
    systemctl = shutil.which("systemctl") or "systemctl"
    try:
        result = subprocess.run(
            ["sudo", "-n", "-l", systemctl, "restart", PUBLIC_UNIT],
            capture_output=True, text=True, timeout=_SYSTEMCTL_TIMEOUT, check=False)
    except FileNotFoundError:
        return {"tried": True, "available": False, "detail": "There is no sudo on this server."}
    except (OSError, subprocess.SubprocessError) as e:
        return {"tried": True, "available": False, "detail": str(e)}
    return {
        "tried": True,
        "available": result.returncode == 0,
        "exit_code": result.returncode,
        # Verbatim. This is the string that names the real cause, and
        # summarising it is how the real cause got lost the first time.
        "detail": (result.stderr or result.stdout or "").strip(),
        "command": f"sudo -n -l {systemctl} restart {PUBLIC_UNIT}",
    }


def _restart_diagnosis(state: dict) -> dict:
    """Everything needed to see why a restart would or would not work,
    rather than a sentence asserting it.

    A message that reads the same whether or not you have done the thing
    it asks for is a message nobody can act on."""
    try:
        whoami = pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, AttributeError):
        whoami = ""
    method, why_not = _restart_method()
    diagnosis = {
        "method": method,
        "reason": why_not,
        "running_as": whoami,
        "running_uid": os.geteuid(),
        "unit_user": state.get("unit_user", ""),
        "unit_main_pid": state.get("main_pid", 0),
        "unit_restart_policy": state.get("restart_policy", ""),
        "systemctl_path": shutil.which("systemctl") or "",
        "no_new_privileges": _no_new_privileges(),
    }
    # Only worth asking sudo when sudo is the path we would take.
    if method == "systemctl":
        diagnosis["sudo"] = _sudo_diagnosis()
    return diagnosis


def _no_new_privileges() -> bool:
    """Whether this process may gain privileges at all.

    systemd's NoNewPrivileges=yes -- which deploy/dashboard.service sets
    -- makes the kernel ignore the setuid bit on anything this process
    executes, and setuid is exactly how sudo becomes root. So under it no
    sudoers line can work, and saying so is the difference between a
    fixable problem and an hour of editing a file that was already
    right."""
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("NoNewPrivs:"):
                return line.split(":", 1)[1].strip() == "1"
    except OSError:
        pass
    return False


def _wait_for_restart(was_pid: int, seconds: float = 8.0) -> dict:
    """Waits for systemd to bring the unit back on a new process.

    Polled rather than assumed. Having sent a signal is not evidence that
    anything came back -- and a service that dies a second later leaves a
    page saying "restarted" over a site that is down, which is the kind
    of reassurance that costs an hour to see through."""
    deadline = time.monotonic() + seconds
    state = _public_service_state()
    while time.monotonic() < deadline:
        state = _public_service_state()
        if state.get("active") and state.get("main_pid") not in (0, was_pid):
            return state
        time.sleep(0.3)
    return state


@app.get("/api/service/public")
def public_service_status():
    """Whether the public site is up, and how this page would restart
    it."""
    state = _public_service_state()
    method, why_not = _restart_method()
    return {"unit": PUBLIC_UNIT, **state,
            # Kept for the page, which greys the button on it -- but the
            # button is no longer *prevented* by it. See the restart
            # endpoint.
            "can_restart": method is not None,
            "restart_blocked": why_not,
            "diagnosis": _restart_diagnosis(state)}


@app.post("/api/service/public/restart")
def public_service_restart():
    """Restarts the public site.

    Worth a button because the public site holds its own handles on the
    database and the search index for the life of the process, and a pull
    that brings new code reaches it only when it starts again -- the same
    staleness this dashboard's own restart button exists for, one process
    over.

    Two ways, tried in that order:

    **Signal it.** Both units run as the same user, so this process may
    send SIGTERM to that one and systemd's Restart= brings it straight
    back. No privilege of any kind, which matters because
    deploy/dashboard.service sets NoNewPrivileges=yes -- under that flag
    the kernel ignores the setuid bit on anything this process runs, and
    setuid is how sudo becomes root, so *no* sudoers line can work here.

    **Ask systemd.** For a split-user setup, where signalling is not
    possible. Needs both a sudoers line and NoNewPrivileges=no.

    What it does not do is decide in advance that it cannot and refuse.
    The previous version did, on a `sudo -n -l` probe that exits non-zero
    for several unrelated reasons, and the single message it could
    produce -- "add this sudoers line" -- was the one thing that could
    not help somebody who had added it. So the attempt is the
    measurement, and a failure carries what actually happened."""
    state = _public_service_state()
    if not state.get("systemd", False):
        raise HTTPException(409, "There is no systemd here, so there is no service to restart.")
    if state.get("known") and not state.get("installed"):
        raise HTTPException(409, f"{PUBLIC_UNIT} is not installed on this server.")

    method, why_not = _restart_method()
    attempts = []

    if method == "signal":
        was_pid = state["main_pid"]
        try:
            os.kill(was_pid, signal.SIGTERM)
        except OSError as e:
            attempts.append(f"signalling process {was_pid}: {e}")
        else:
            after = _wait_for_restart(was_pid)
            if after.get("active") and after.get("main_pid") not in (0, was_pid):
                return {"ok": True,
                        "message": f"The public site has restarted (was {was_pid}, "
                                   f"now {after['main_pid']}).",
                        **after}
            attempts.append(
                f"signalled process {was_pid}, but the unit is {after.get('state')} "
                f"({after.get('sub_state')}) on pid {after.get('main_pid')}")
    elif why_not:
        attempts.append(why_not)

    # Either signalling was not possible, or it was and did not take.
    try:
        result = _systemctl("systemctl", "restart", PUBLIC_UNIT)
    except subprocess.TimeoutExpired:
        attempts.append(f"systemctl did not answer within {_SYSTEMCTL_TIMEOUT}s")
        result = None
    except (OSError, subprocess.SubprocessError) as e:
        attempts.append(f"running systemctl: {e}")
        result = None
    if result is not None and result.returncode != 0:
        # Verbatim, because this string names the real cause -- including
        # the NoNewPrivileges one, which no summary of mine would have.
        attempts.append((result.stderr or result.stdout or "").strip()
                        or f"systemctl exited {result.returncode}")
    elif result is not None:
        after = _wait_for_restart(state.get("main_pid", 0), seconds=5.0)
        if after.get("active"):
            return {"ok": True, "message": "The public site has restarted.", **after}
        attempts.append(f"systemctl accepted the restart but the unit is "
                        f"{after.get('state')} ({after.get('sub_state')})")

    raise HTTPException(500, "Couldn't restart the public site.\n\n"
                             + "\n\n".join(attempts)
                             + f"\n\nFrom a terminal: sudo systemctl restart {PUBLIC_UNIT}\n"
                               f"Then: journalctl -u {PUBLIC_UNIT} -n 50")


class PullRequest(BaseModel):
    pass


class DiscardRequest(BaseModel):
    # Typed out in full by whoever is discarding. The only destructive
    # button on the page, and the only one that asks for more than a
    # click.
    confirm: str = ""


@app.post("/api/sync/commit")
def sync_commit(req: PushRequest):
    """Commits the review work without sending it anywhere.

    For the case push cannot help with: a remote that is not answering.
    The work still lands somewhere it survives a restart."""
    message = (req.message or "").strip() or _default_commit_message()
    try:
        result = sync.commit(BASE_DIR, message)
    except sync.SyncError as e:
        raise HTTPException(409, str(e)) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"Couldn't run git: {e}") from e
    return {**result, "status": sync.status(BASE_DIR)}


@app.post("/api/sync/pull")
def sync_pull(req: "PullRequest | None" = None):
    """Brings in what was committed elsewhere. Fast-forward only -- see
    corpus/sync.py's pull for why there is no merge to fall back on."""
    try:
        return sync.pull(BASE_DIR)
    except sync.SyncError as e:
        raise HTTPException(409, str(e)) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"Couldn't run git: {e}") from e


@app.post("/api/sync/discard")
def sync_discard(req: DiscardRequest):
    """Throws away uncommitted review work and goes back to the last
    commit, keeping a copy of the database first (see sync.discard).

    Requires the word typed out, because a click in the wrong place
    should not be able to do this."""
    if req.confirm.strip().lower() != "discard":
        raise HTTPException(400, "Type 'discard' to confirm.")
    try:
        return sync.discard(BASE_DIR)
    except sync.SyncError as e:
        raise HTTPException(409, str(e)) from e
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"Couldn't run git: {e}") from e


@app.post("/api/sync/restart")
def sync_restart():
    """Exits, so that systemd starts this service again on the code that
    is now on disk.

    There is no way for a process to reload its own imports, so restarting
    is the only honest way to finish a pull that brought new code. The
    exit status is a failure one deliberately: it restarts a unit set to
    either `on-failure` or `always`, where a clean exit only restarts the
    second, and a server whose unit file predates this feature is exactly
    where getting that wrong would leave the dashboard down."""
    can, why = _restart_capability()
    if not can:
        raise HTTPException(409, f"{why} Restart it from a terminal: sudo systemctl restart dashboard")

    def _exit_once_this_response_is_out():
        time.sleep(0.5)
        os._exit(1)

    threading.Thread(target=_exit_once_this_response_is_out, daemon=True).start()
    return {"ok": True, "message": "Restarting -- reload this page in a few seconds."}


# ---------------------------------------------------------------------------
# Rebuilding the published site
# ---------------------------------------------------------------------------
#
# The public site is a static export: nothing about adding an Act or
# reviewing one changes what is being served until export_static_site.py
# runs again. Left to the terminal, that shows up as "the new Acts aren't
# on the site" with nothing wrong anywhere.

_site_build: dict = {}
_SITE_BUILD_LOG = BASE_DIR / "data" / "site_build.log"
_SITE_OUT = "_site"


@app.post("/api/site/rebuild")
def site_rebuild():
    """Runs export_static_site.py over the current data, in the
    background: a full build is minutes, far past what one request should
    be left holding open.

    No --password: the script takes the passphrase from deploy/site.env
    and refuses outright to replace a gated build with an open one (see
    export_static_site.resolve_password), so the way to publish this site
    in the clear stays a deliberate command rather than a button."""
    running = _site_build.get("proc")
    if running and running.poll() is None:
        raise HTTPException(409, "A rebuild is already running.")
    _SITE_BUILD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_SITE_BUILD_LOG, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "corpus.exporters.export_static_site", "--out", _SITE_OUT],
            cwd=str(BASE_DIR), stdout=log_file, stderr=subprocess.STDOUT,
        )
    _site_build.update({"proc": proc, "started": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "message": "Rebuilding the published site. This takes a few minutes."}


@app.get("/api/site/progress")
def site_progress():
    """How the rebuild is going, and what it said.

    The log tail is the whole point: a build that refuses to run -- no
    passphrase where the published site has one -- says so on stdout and
    nowhere else."""
    proc = _site_build.get("proc")
    running = bool(proc and proc.poll() is None)
    log = ""
    if _SITE_BUILD_LOG.exists():
        log = _SITE_BUILD_LOG.read_text(encoding="utf-8", errors="replace")[-4000:]
    built = BASE_DIR / _SITE_OUT / "index.html"
    return {
        "running": running,
        "started": _site_build.get("started"),
        "exit_code": None if running or proc is None else proc.returncode,
        "log": log,
        "last_built": (datetime.fromtimestamp(built.stat().st_mtime, timezone.utc).isoformat()
                       if built.exists() else None),
    }


@app.post("/api/logout")
def do_logout(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if token:
        _SESSIONS.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_NAME, path=_cookie_path())
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "dashboard.html")

@app.get("/api/acts")
def list_acts():
    publication = db.load_publication(BASE_DIR)
    return [act_status(slug, publication) for slug in discover_slugs()]


class DefinitionOverrideRequest(BaseModel):
    term: str
    # "add" links a term the matcher missed; "remove" stops it linking one
    # it should not have; "auto" throws the decision away and hands the
    # term back to the matcher.
    action: str
    section: "str | None" = None


@app.get("/api/acts/{slug}/definitions")
def list_definitions(slug: str):
    """Which words this document hyperlinks back to where they are
    defined, and which of them a person has decided rather than the
    pattern-matcher.

    Both sides of that are returned, because the useful question is not
    "what links?" but "what did I change?" -- a term the matcher found
    and a term somebody added look identical on the page and need to be
    told apart here."""
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- parse it first.")
    ctx = html_view._build_context(_parsed(slug), _act_title(slug))
    found, effective = ctx["definitions_found"], ctx["definitions"]
    decided = {row["term"]: row for row in db.load_definition_overrides(slug, BASE_DIR)}

    terms = []
    for term in sorted(set(found) | set(effective) | set(decided)):
        decision = decided.get(term)
        state = "auto"
        if decision:
            state = "removed" if decision["action"] == "remove" else "added"
        entry = effective.get(term) or {}
        # "s5.md" is the page file; a reader wants the provision.
        target = (entry.get("file") or "")[:-3] if entry.get("file") else None
        terms.append({
            "term": term,
            "state": state,
            "links_to": target,
            "section": (decision or {}).get("section"),
            "found_by_matcher": term in found,
            # An 'add' naming a Section this document does not have is
            # dropped when the page is built rather than linked wrong, so
            # say so here instead of showing a decision that does nothing.
            "unresolved": state == "added" and term not in effective,
        })
    return {
        "slug": slug,
        "title": _act_title(slug),
        "terms": terms,
        "counts": {
            "linked": len(effective),
            "found": len(found),
            "added": sum(1 for t in terms if t["state"] == "added"),
            "removed": sum(1 for t in terms if t["state"] == "removed"),
        },
        # What an added term may point at, so the form can offer them
        # rather than have somebody guess a Section number.
        "sections": sorted(ctx["section_files"], key=_section_sort_key),
    }


def _section_sort_key(number: str) -> tuple:
    """"7A" after "7" and before "8" -- the order an Act is printed in,
    rather than the order strings sort in."""
    digits = "".join(c for c in number if c.isdigit())
    suffix = "".join(c for c in number if not c.isdigit())
    return (int(digits) if digits else 0, suffix)


@app.post("/api/acts/{slug}/definitions")
def set_definition(slug: str, req: DefinitionOverrideRequest):
    """Records one decision about one term, and returns the list as it
    now stands so the page never has to guess what happened."""
    _validate_slug(slug)
    term = (req.term or "").strip()
    if not term:
        raise HTTPException(400, "Which term?")
    try:
        if req.action == "auto":
            db.clear_definition_override(slug, term, BASE_DIR)
        else:
            db.set_definition_override(slug, term, req.action, req.section, BASE_DIR)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # The browse pages read these through a cache keyed on the review
    # database's own mtime, which the write above has just moved -- but
    # this process holds its own copy, so drop it rather than wait for
    # the next stat to disagree.
    _definition_overrides_cache.pop(slug, None)
    return list_definitions(slug)


class PublicationRequest(BaseModel):
    work: str
    published: bool


@app.post("/api/publication")
def set_publication(req: PublicationRequest):
    """Puts a work on the public site, or takes it off.

    Per work rather than per parsed document: an Act is on the site or it
    isn't, and all of its reprints go with it (see corpus/db.py). The
    decision lands in the database beside the review work, and is written
    out to data/review/publication.jsonl with it, so it travels to GitHub
    on the next push rather than living only on whichever machine it was
    made."""
    if not req.work.strip():
        raise HTTPException(400, "Which work?")
    db.set_publication(req.work.strip(), req.published, BASE_DIR)
    # The index holds what the site serves, so this just changed it.
    _rebuild_search_index_soon()
    publication = db.load_publication(BASE_DIR)
    return {
        "ok": True,
        "work": req.work.strip(),
        "published": req.published,
        "published_works": sorted(w for w, on in publication.items() if on),
    }


@app.get("/api/profiles")
def list_profiles():
    """The pattern profiles that exist, so the upload form can offer them
    instead of asking for one to be typed from memory. A name that is
    wrong is not a small mistake -- parsing the Criminal Procedure Act
    without its profile stops "Part 2.1" matching as a Part at all."""
    return {"profiles": available_profiles()}


@app.post("/api/bill-link/remove")
def bill_unlink(bill_slug: str = Form(...)):
    """Forgets what a Bill was linked to.

    Linking by hand means getting it wrong by hand, and the only way back
    used to be deleting files on the server. Removes every link document
    that names this Bill -- the Bill-to-Act one and the EM one are
    written separately (see _load_bill_link_docs), and a half-removed
    link is worse than either state."""
    _validate_slug(bill_slug)
    links_dir = BASE_DIR / "data" / "bill_links"
    removed = []
    for path in sorted(links_dir.glob("*.json")) if links_dir.is_dir() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and doc.get("bill_slug") == bill_slug:
            path.unlink()
            removed.append(path.name)
    if not removed:
        raise HTTPException(404, f"No links recorded for {bill_slug!r}.")
    return {"ok": True, "removed": removed,
            "log": "Removed " + ", ".join(removed)}


def _bill_link_groups() -> list[dict]:
    """[{"bill_slug", "act_work", "em_slug"}] for the dashboard's own
    grouping: which Bill goes with which Act's card, and which
    Explanatory Memorandum goes with that Bill.

    Merged across every data/bill_links/ file that mentions a given Bill,
    since run_bill_linking.py can write a Bill-to-Act file and a Bill-to-EM
    file separately -- a Bill linked to only one of the two still gets a
    group, with the other slot left null. Keyed on the Act's *work*, not
    one specific version's slug, so the group still finds this Act's card
    however many versions of it exist."""
    groups: dict[str, dict] = {}
    bill_docs, em_docs = _load_bill_link_docs()
    for doc in bill_docs + em_docs:
        bill_slug = doc.get("bill_slug")
        if not bill_slug:
            continue
        group = groups.setdefault(bill_slug, {"bill_slug": bill_slug, "act_work": None, "em_slug": None})
        act_slug = doc.get("act_slug")
        if act_slug:
            group["act_work"] = split_document_slug(act_slug)[0]
        if doc.get("em_slug"):
            group["em_slug"] = doc["em_slug"]
    return list(groups.values())


@app.get("/api/bill-links")
def list_bill_links():
    return _bill_link_groups()


def _validate_parse_params(kind: str, profile: str, start_page: str, end_page: str) -> None:
    if kind not in ("act", "bill", "em"):
        raise HTTPException(400, f"Invalid kind: {kind!r}")
    if profile.strip() and not _SLUG_RE.match(profile.strip()):
        raise HTTPException(400, f"Invalid profile name: {profile!r}")
    for field_name, value in (("start_page", start_page), ("end_page", end_page)):
        if value.strip() and not value.strip().isdigit():
            raise HTTPException(400, f"{field_name} must be a positive integer")


def _repo_relative(path: Path) -> str:
    """run_pipeline.py records the PDF path it was given verbatim into
    data/parsed/<slug>.json's own "source" field, and that file is
    committed to git (see .gitignore's own comment) -- so hand it a
    repo-relative path, the same thing a human running it from the CLI
    would type. An absolute one would bake this particular machine's
    checkout location into the committed output, rewriting that field to
    a different meaningless value on every machine that ever reparses.
    Safe because every subprocess below runs with cwd=BASE_DIR anyway."""
    try:
        return str(path.resolve().relative_to(BASE_DIR.resolve()))
    except ValueError:
        return str(path)  # outside the repo entirely -- nothing relative to say


def _build_parse_command(pdf_path: Path, kind: str, profile: str, start_page: str, end_page: str) -> list[str]:
    """Shared by new_act (a freshly uploaded PDF) and reparse_act (an
    already-uploaded one, re-run to pick up a profile or to regenerate
    after a parser change) -- same options either way, only which PDF path
    they point at differs."""
    source = _repo_relative(pdf_path)
    if kind == "em":
        return [sys.executable, "-m", "corpus.parsing.run_em_pipeline", source]
    cmd = [sys.executable, "-m", "corpus.parsing.run_pipeline", source, "--document-type", "bill" if kind == "bill" else "act"]
    if profile.strip():
        cmd += ["--profile", profile.strip()]
    if start_page.strip():
        cmd += ["--start-page", start_page.strip()]
    if end_page.strip():
        cmd += ["--end-page", end_page.strip()]
    return cmd


def _run_parse_subprocess(cmd: list[str]) -> tuple[bool, "int | None", str]:
    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        return False, None, f"Timed out after 30 minutes.\n{e.stdout or ''}\n{e.stderr or ''}"
    return result.returncode == 0, result.returncode, result.stdout + result.stderr


def _find_source_pdf(slug: str) -> "Path | None":
    """The PDF a document was parsed from, or would be parsed from.

    Three ways in, cheapest first. A parsed document records the path it
    came from, which is authoritative and settles it without opening
    anything. A version of a work lives in that work's own directory under
    a filename nothing can predict, so it is found by reading each PDF's
    front matter for the version number the slug names. Anything else is a
    document in its own right, named after its own file."""
    acts_dir = BASE_DIR / "acts"
    if not acts_dir.exists():
        return None
    recorded = _parse_field(slug, "source")
    if recorded:
        path = BASE_DIR / recorded
        if path.exists():
            return path
    work, version = split_document_slug(slug)
    if version is not None:
        directory = acts_dir / work
        if directory.is_dir():
            for pdf in sorted(directory.glob("*.pdf")):
                if _pdf_version(pdf) == version:
                    return pdf
        return None
    matches = sorted(p for p in acts_dir.glob(f"{slug}.*") if p.suffix.lower() == ".pdf")
    return matches[0] if matches else None


@app.post("/api/acts/new")
async def new_act(
    pdf: UploadFile = File(...),
    kind: str = Form("act"),
    profile: str = Form(""),
    start_page: str = Form(""),
    end_page: str = Form(""),
):
    _validate_parse_params(kind, profile, start_page, end_page)
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")

    acts_dir = BASE_DIR / "acts"
    acts_dir.mkdir(parents=True, exist_ok=True)
    dest = acts_dir / Path(pdf.filename).name  # .name strips any directory components
    dest.write_bytes(await pdf.read())
    slug = slugify(dest.stem)
    _act_title_cache.pop(slug, None)  # a re-upload under this slug may have a different title

    cmd = _build_parse_command(dest, kind, profile, start_page, end_page)
    ok, returncode, log = _run_parse_subprocess(cmd)
    return {"ok": ok, "slug": slug, "returncode": returncode, "log": log}


@app.post("/api/acts/{slug}/reparse")
def reparse_act(
    slug: str,
    kind: str = Form("act"),
    profile: str = Form(""),
    start_page: str = Form(""),
    end_page: str = Form(""),
    confirm: str = Form(""),
    discard: str = Form(""),
):
    """Re-runs the pipeline against an already-uploaded PDF -- no new
    upload needed -- so an Act can be re-parsed with a profile it was
    missing, or just regenerated after a parser code change, without
    starting over from "Add Act/Bill/EM".

    data/parsed/<slug>.json is plain regenerable output on its own,
    but review.py's own verified rows in data/legislation.db are keyed by
    a *positional* index into that exact file (see .gitignore's own
    comment on why the two are committed as a pair). run_pipeline.py now
    re-anchors those rows onto the new parse rather than leaving them
    pointing at whatever moved into their old positions (see
    corpus/reparse.py), so this no longer silently corrupts review
    progress -- but it can still withdraw acceptance from a provision the
    parser now reads differently, and that is a real change to somebody's
    work. Refuses (409) unless `confirm` is set, once there's any
    reviewed progress to re-anchor.

    `discard` throws that progress away instead of carrying it across.
    Re-anchoring is the right default -- it is somebody's work -- but it
    is the wrong answer after a parser change big enough that the old
    decisions describe provisions that no longer exist in that shape, and
    then resetting them one at a time is the only alternative. What it
    clears is listed in db.clear_act_review. It is done *before* the
    pipeline runs, so there is nothing left for the re-anchoring step to
    carry and the new parse starts clean."""
    _validate_slug(slug)
    _validate_parse_params(kind, profile, start_page, end_page)

    pdf_path = _find_source_pdf(slug)
    if pdf_path is None:
        raise HTTPException(404, f"No source PDF found for {slug!r} in acts/ -- add it via 'Add Act/Bill/EM' first.")

    wants_discard = discard.strip().lower() == "true"
    status = act_status(slug)
    reviewed = status.get("reviewed_units") or 0
    if status["parsed"] and reviewed > 0 and confirm.strip().lower() != "true":
        raise HTTPException(
            409,
            f"{slug} has {reviewed} of {status['unit_count']} unit(s) already reviewed. "
            + (
                "Discarding review data throws every one of those decisions away -- accepted pieces, flags, "
                "link annotations, independent assessments and AI scan findings -- and cannot be undone. "
                "The Act comes back with nothing reviewed at all. Confirm to re-parse."
                if wants_discard else
                "Re-parsing regenerates the raw structure from the PDF; your reviewed pieces are carried "
                "across onto the provisions they describe, but any whose wording the parser now reads "
                "differently will have their acceptance withdrawn for you to look at again. Confirm to re-parse."
            ),
        )

    cleared = db.clear_act_review(slug) if wants_discard else {}
    _act_title_cache.pop(slug, None)
    cmd = _build_parse_command(pdf_path, kind, profile, start_page, end_page)
    ok, returncode, log = _run_parse_subprocess(cmd)
    if ok:
        # The parse this Act's review.py process (if any) loaded into
        # memory at startup is now stale -- force a fresh one on the next
        # "Review" click rather than let it keep serving the old node
        # list against a database that may no longer line up with it.
        _kill_review_process(slug)
    return {"ok": ok, "slug": slug, "returncode": returncode, "log": log, "cleared": cleared}


@app.post("/api/acts/{slug}/export/akn")
def export_akn(slug: str):
    _validate_slug(slug)
    result = subprocess.run([sys.executable, "-m", "corpus.exporters.export_akn", slug], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300)
    return {"ok": result.returncode == 0, "log": result.stdout + result.stderr}


@app.post("/api/acts/{slug}/export/markdown")
def export_markdown(slug: str):
    _validate_slug(slug)
    result = subprocess.run(
        [sys.executable, "-m", "corpus.exporters.export_markdown", slug], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
    )
    return {"ok": result.returncode == 0, "log": result.stdout + result.stderr}


@app.get("/api/acts/{slug}/download/akn")
def download_akn(slug: str):
    _validate_slug(slug)
    path = BASE_DIR / "data" / "akn" / f"{slug}.xml"
    if not path.exists():
        raise HTTPException(404, "Not exported yet")
    return FileResponse(path, filename=f"{slug}.xml", media_type="application/xml")


@app.get("/api/acts/{slug}/download/markdown")
def download_markdown(slug: str):
    _validate_slug(slug)
    src_dir = BASE_DIR / "data" / "markdown" / slug
    if not src_dir.exists():
        raise HTTPException(404, "Not exported yet")
    zip_base = BASE_DIR / "data" / "markdown" / f"_{slug}-download"
    zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=str(src_dir))
    return FileResponse(zip_path, filename=f"{slug}-markdown.zip", media_type="application/zip")


@app.post("/api/bill-link")
def bill_link(bill_slug: str = Form(...), act_slug: str = Form(...), em_slug: str = Form("")):
    _validate_slug(bill_slug)
    _validate_slug(act_slug)
    cmd = [sys.executable, "-m", "corpus.review.run_bill_linking", bill_slug, act_slug]
    if em_slug.strip():
        _validate_slug(em_slug.strip())
        cmd += ["--em", em_slug.strip()]
    result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300)
    return {"ok": result.returncode == 0, "log": result.stdout + result.stderr}


@app.get("/api/ai/status")
def ai_status():
    """Whether the AI-assist feature (see corpus/ai/assist.py, and
    the "Ask local AI" button in each Act's review.py) is actually ready
    to use -- Ollama installed, running, and its model pulled. A quick
    local check, not a parse-time dependency: this feature stays
    entirely optional, so nothing here blocks anything else on the
    dashboard if it comes back not-ready."""
    return OllamaBackend().status()


@app.post("/api/ai/install-model")
def ai_install_model():
    """Pulls the AI-assist feature's model via `ollama pull` (see
    corpus.ai.backend.pull_model) -- the same action
    install_ai_model.py performs from the command line, offered here
    too since a user who's already at this dashboard shouldn't have to
    leave it to set this up. Still refuses to do anything about Ollama
    itself not being installed or not running (see pull_model), for the
    same reason install_ai_model.py does: this dashboard has no
    business deciding to install and start a background service on its
    own behalf."""
    backend = OllamaBackend()
    ok, log = pull_model(backend.model, host=backend.host)
    return {"ok": ok, "model": backend.model, "log": log}


@app.post("/api/ai-scan/{slug}/start")
def start_ai_scan(slug: str, restart: bool = False):
    """Starts run_ai_review.py's whole-document audit pass in the
    background for one Act -- see that script's own docstring for why
    it's a separate, offline process rather than something started
    inline: scanning every unit with a local model is genuinely slow,
    far past what one HTTP request should be left holding open for."""
    _validate_slug(slug)
    entry = _ai_scan_procs.get(slug)
    if entry and entry["proc"].poll() is None:
        raise HTTPException(409, f"An AI scan for {slug!r} is already running.")
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")

    _AI_SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ai_scan_log_path(slug)
    cmd = [sys.executable, "-m", "corpus.review.run_ai_review", slug]
    if restart:
        cmd.append("--restart")
    with open(log_path, "w", encoding="utf-8") as log_file:
        # Popen dup()s this fd for the child before returning, so closing
        # our own copy (the `with` block exiting) right after doesn't
        # affect the child's writes.
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=log_file, stderr=subprocess.STDOUT)
    _ai_scan_procs[slug] = {"proc": proc}
    return {"ok": True}


@app.get("/api/ai-scan/{slug}/progress")
def ai_scan_progress_endpoint(slug: str):
    """How far run_ai_review.py has gotten for this Act -- units scanned
    and concerns found so far (see db.ai_scan_progress), plus whether a
    scan is currently running and, if one just stopped, whether it
    exited cleanly. Polled from the dashboard rather than pushed, since
    a scan can span a dashboard restart and this reads straight from
    data/legislation.db either way.

    Also hands back the tail of the script's own output (see
    _AI_SCAN_LOG_DIR) -- the only place a failure reason like "Ollama
    isn't installed" ends up, since this process has no HTTP server of
    its own to report through."""
    _validate_slug(slug)
    progress = db.ai_scan_progress(slug)
    entry = _ai_scan_procs.get(slug)
    running = bool(entry and entry["proc"].poll() is None)
    exit_code = None if running or entry is None else entry["proc"].returncode
    total_units = len(group_into_units(build_current_nodes(slug)[0])) if (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists() else 0
    log_path = _ai_scan_log_path(slug)
    log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.exists() else ""
    return {**progress, "total_units": total_units, "running": running, "exit_code": exit_code, "log": log_tail}


@app.post("/api/ai-scan/{slug}/stop")
def stop_ai_scan(slug: str):
    """Terminates this Act's running scan (if any); already-saved rows
    stay put, so a later start (or --restart) picks up from there, same
    as an interruption from Ctrl-C or a crash would."""
    _validate_slug(slug)
    entry = _ai_scan_procs.pop(slug, None)
    if entry and entry["proc"].poll() is None:
        entry["proc"].terminate()
    return {"ok": True}


_current_nodes_cache: dict[str, tuple[tuple, tuple]] = {}


def _browse_state_signature(slug: str) -> tuple:
    """A cheap stamp of everything build_current_nodes reads: this Act's
    parse plus the review database (whose -wal file is where a write
    actually lands first under WAL, so the .db's own mtime alone would
    miss an edit a reviewer just made)."""
    paths = [
        BASE_DIR / "data" / "parsed" / f"{slug}.json",
        db.db_path(BASE_DIR),
        Path(f"{db.db_path(BASE_DIR)}-wal"),
    ]
    stamp = []
    for path in paths:
        try:
            st = path.stat()
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def _current_nodes(slug: str) -> tuple[list[dict], list[dict], list[str]]:
    """build_current_nodes re-reads the parse and re-merges every verified
    row on each call -- ~0.2s for a large Act, which was fine when only a
    page view paid it, but the hover-preview endpoint can be hit several
    times while a reader skims one page. Cached against the signature
    above, so a reviewer's edit still shows up on the very next request
    (the whole point of this view being live) without re-reading the Act
    for every hover."""
    signature = _browse_state_signature(slug)
    cached = _current_nodes_cache.get(slug)
    if cached is not None and cached[0] == signature:
        return cached[1]
    state = build_current_nodes(slug)
    _current_nodes_cache[slug] = (signature, state)
    return state


_commentary_cache: dict[str, tuple[tuple, dict]] = {}
_page_index_cache: dict[str, tuple[tuple, dict]] = {}


def _bill_links_signature() -> tuple:
    """A stamp of the whole data/bill_links/ directory -- these files are
    rewritten wholesale by run_bill_linking.py, so the set of names plus
    their mtimes is enough to know the answer below has changed."""
    links_dir = BASE_DIR / "data" / "bill_links"
    if not links_dir.is_dir():
        return ()
    stamp = []
    for path in sorted(links_dir.glob("*.json")):
        try:
            st = path.stat()
        except OSError:
            continue
        stamp.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(stamp)


def _load_bill_link_docs() -> tuple[list[dict], list[dict]]:
    """(bill->act documents, EM documents) from data/bill_links/. An
    unreadable or malformed file is skipped rather than failing the page:
    these are an optional enrichment of a browse view, not something it
    depends on to render."""
    bill_docs, em_docs = [], []
    links_dir = BASE_DIR / "data" / "bill_links"
    if not links_dir.is_dir():
        return bill_docs, em_docs
    for path in sorted(links_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict) or "links" not in doc:
            continue  # pre-header format (a bare list) -- nothing to relate it by
        (em_docs if doc.get("em_slug") else bill_docs).append(doc)
    return bill_docs, em_docs


def related_documents(act_slug: str) -> list[dict]:
    """The Bill this Act was enacted from, and that Bill's Explanatory
    Memorandum -- as {"slug", "kind"}, in that order.

    Read off data/bill_links/, which already records both relations: a
    bill->act document names the Act a Bill became, and an EM document
    names the Bill an EM explains. A Bill and an EM belong to their Act
    rather than standing beside it, so this is what lets the Act's own
    contents page offer them instead of the site's front page listing
    all three as if they were separate publications."""
    bill_docs, em_docs = _load_bill_link_docs()
    related = []
    for doc in bill_docs:
        if doc.get("act_slug") != act_slug or not doc.get("bill_slug"):
            continue
        related.append({"slug": doc["bill_slug"], "kind": "bill"})
        for em in em_docs:
            if em.get("bill_slug") == doc["bill_slug"] and em.get("em_slug"):
                related.append({"slug": em["em_slug"], "kind": "em"})
    # One entry per document, even where several link files mention it.
    seen, unique = set(), []
    for entry in related:
        if entry["slug"] not in seen:
            seen.add(entry["slug"])
            unique.append(entry)
    return unique


def _commentary_index(act_slug: str) -> dict:
    signature = _bill_links_signature()
    cached = _commentary_cache.get(act_slug)
    if cached is not None and cached[0] == signature:
        return cached[1]
    bill_docs, em_docs = _load_bill_link_docs()
    index = build_commentary_index(act_slug, bill_docs, em_docs)
    _commentary_cache[act_slug] = (signature, index)
    return index


def _page_index(slug: str) -> dict:
    """Where each of another document's provisions lives, so this Act's
    pages can link into it. Cached against the same signature the browse
    pages use, since it's derived from that document's own parse."""
    signature = _browse_state_signature(slug)
    cached = _page_index_cache.get(slug)
    if cached is not None and cached[0] == signature:
        return cached[1]
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        index = {"by_node_index": {}, "by_key": {}, "schedule_by_node_index": {}}
    else:
        nodes, _unattached, hierarchy = _current_nodes(slug)
        index = html_view.build_page_index(_parsed(slug), _act_title(slug))
    _page_index_cache[slug] = (signature, index)
    return index


_definition_overrides_cache: dict = {}


def _definition_overrides(slug: str) -> list[dict]:
    """What a person has said about this document's defined terms --
    which words the site hyperlinks back to where they are defined, where
    they have overruled the pattern-matcher (see corpus/definitions.py).

    Cached against the same signature the browse pages use. It is read
    once per rendered page and a static build renders thousands; that
    signature stamps the review database's own file, which is where these
    rows live, so recording a decision invalidates it."""
    signature = _browse_state_signature(slug)
    cached = _definition_overrides_cache.get(slug)
    if cached is not None and cached[0] == signature:
        return cached[1]
    rows = db.load_definition_overrides(slug, BASE_DIR)
    _definition_overrides_cache[slug] = (signature, rows)
    return rows


def _parsed(slug: str) -> dict:
    """The document in the shape every renderer takes it.

    One builder rather than the same dict literal in eight places, and
    that is load-bearing rather than tidy: html_view caches the structure
    it derives from this, keyed on what is in it, so a caller that
    assembled a slightly different dict would quietly get its own second
    copy of an 80ms build -- and, now that a person's decisions about
    defined terms travel in here, its own answer about which words are
    defined. A hover card disagreeing with the page behind it is exactly
    the kind of difference nobody would think to look for.

    Callers add the page-specific keys -- endnotes, version -- on top."""
    nodes, _unattached, hierarchy = _current_nodes(slug)
    return {"nodes": nodes, "hierarchy": hierarchy,
            "definition_overrides": _definition_overrides(slug)}


def _section_crossrefs(act_slug: str, section_number: str | None, schedule: str | None = None) -> list[dict]:
    """The "Explained in" chips for one Act provision: the Bill clause it
    was enacted from, and each Explanatory Memorandum note about it, as
    ordinary links into those documents' own browse pages (so the hover
    preview reads them like any other link). A related document that
    hasn't been parsed has no page to link to and is simply left out --
    the link record is about a document this pipeline may not hold.

    `schedule` is the Act Schedule this provision sits in, or None for one
    in the body. A Schedule numbers its own provisions from 1 again, so
    without it section 11 and Schedule 1 clause 11 are the same lookup:
    section 11 collected both their chips and the reader was shown the
    same clause twice."""
    if not section_number:
        return []
    entry = _commentary_index(act_slug).get(commentary.provision_key(schedule, section_number))
    if not entry:
        return []
    chips = []
    for bill in entry["bill"]:
        where = diffing.provision_identity("clause", bill.get("schedule"), bill["clause_number"])
        page = _page_index(bill["bill_slug"])["by_key"].get(where)
        if not page:
            continue
        title = f"{_act_title(bill['bill_slug'])} \u2014 the clause this section was enacted from"
        if bill.get("status") == "flagged":
            title += f" (wording diverged; {bill['similarity']} text similarity -- worth checking)"
        label = (
            f"Bill Schedule {bill['schedule']} clause {bill['clause_number']}"
            if bill.get("schedule") else f"Bill clause {bill['clause_number']}"
        )
        chips.append({
            "kind": "bill",
            "label": label,
            "href": f"/browse/{bill['bill_slug']}/section/{page}",
            "title": title,
        })
    # Two EM notes can name the same clause number without being about the
    # same provision: a Bill's Schedule numbers its own clauses from 1
    # again, so "clause 11" in the body and "clause 11" of Schedule 1 are
    # different things. Say which, rather than showing the reader the same
    # words twice. Genuine repeats within one Schedule (a second note
    # after a Chapter heading, a pinpoint note on "clause 6(4)") are still
    # numbered.
    seen_clause: dict[str, int] = {}
    for em in entry["em"]:
        page = _page_index(em["em_slug"])["by_node_index"].get(em["em_node_index"])
        if not page:
            continue
        if em["clause_number"]:
            where = f"Schedule {em['schedule']} clause" if em.get("schedule") else "clause"
            key = f"{em.get('schedule') or ''}/{em['clause_number']}"
            nth = seen_clause[key] = seen_clause.get(key, 0) + 1
            label = f"EM on {where} {em['clause_number']}" + (f" ({nth})" if nth > 1 else "")
        else:
            label = "EM note"
        chips.append({
            "kind": "em",
            "label": label,
            "href": f"/browse/{em['em_slug']}/section/{page}",
            "title": f"{_act_title(em['em_slug'])} \u2014 the note on this provision",
        })
    return chips


_amendment_cache: dict[str, tuple[tuple, dict]] = {}


def _amendments(slug: str) -> dict:
    """{"index", "summary"} for one Act: the lookup that turns a margin
    note's "No. 68/2009" into a named Act with its assent and commencement
    dates, and that same table read the other way round (per amending Act,
    which provisions it touched). Cached against the same signature the
    browse pages use -- the summary is derived from the current nodes, so a
    reviewer's edit has to be able to change it."""
    signature = _browse_state_signature(slug)
    cached = _amendment_cache.get(slug)
    if cached is not None and cached[0] == signature:
        return cached[1]
    parsed_path = BASE_DIR / "data" / "parsed" / f"{slug}.json"
    endnotes = None
    try:
        endnotes = json.loads(parsed_path.read_text(encoding="utf-8")).get("endnotes")
    except (OSError, ValueError):
        pass
    index = build_amendment_index(endnotes, load_act_registry())
    nodes, _unattached, _hierarchy = _current_nodes(slug) if parsed_path.exists() else ([], [], [])
    result = {"index": index, "summary": summarise_by_act(nodes, index), "endnotes": endnotes}
    _amendment_cache[slug] = (signature, result)
    return result


# ---------------------------------------------------------------------------
# A work's versions, and what changed between them
# ---------------------------------------------------------------------------


def _work_versions(work: str) -> list[str]:
    """Every parsed version slug of one work, oldest first. A work with
    fewer than two has no timeline -- there is nothing to compare."""
    slugs = []
    for slug in discover_slugs():
        this_work, version = split_document_slug(slug)
        if this_work == work and version is not None and (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
            slugs.append((version, slug))
    return [slug for _version, slug in sorted(slugs)]


def _parse_signature(slug: str) -> tuple:
    """A stamp of one document's parse file, and nothing else."""
    try:
        st = (BASE_DIR / "data" / "parsed" / f"{slug}.json").stat()
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


_timeline_cache: dict[str, tuple[tuple, dict]] = {}


def _timeline(work: str) -> dict:
    """{provision key -> its changes, oldest first} across every version of
    one work, plus the version list it was built from.

    Cached against every version's own browse signature, because this is
    the most expensive thing the browse view does: it holds all five
    Criminal Procedure Act parses in memory at once and word-diffs ~690
    provisions across each consecutive pair. That is ~0.3s, which is fine
    once and not fine on every page view of every section.

    Keyed on the *work*, not the version: the timeline of a provision is
    the same object whichever reprint of the Act you are reading it from,
    so all five versions share one entry rather than each building its own
    copy of the same comparisons.

    Built from the **parses**, not from _current_nodes -- the one place in
    the browse view that deliberately ignores review state. A reviewer
    works through one version at a time, so merging their edits in makes
    a correction to the parse of one version look exactly like an
    amendment by Parliament: with review state applied, section 5 of the
    Criminal Procedure Act reported "Part 2.2--Charge-sheet and listing of
    matter" as inserted at version 114, when what actually happened is
    that a reviewer split that heading out of section 5 in one version and
    has not yet reached the other. Two raw parses come from the same
    deterministic parser and their artefacts cancel; a reviewed one
    against an unreviewed one is a comparison of two different things.
    """
    slugs = _work_versions(work)
    # Stamped on the parses alone. _browse_state_signature also stamps the
    # review database, which would throw this away and rebuild all five
    # comparisons every time a reviewer saved anything -- and, now that the
    # timeline is built from the parses, for a change that cannot affect
    # its result.
    signature = tuple(_parse_signature(slug) for slug in slugs)
    cached = _timeline_cache.get(work)
    if cached is not None and cached[0] == signature:
        return cached[1]
    result = {"slugs": slugs, "entries": {}, "mixed_parsers": False}
    # Every version has to have been read by the same parser, or the
    # parsers' own disagreements arrive here as provisions Parliament
    # inserted and repealed. A re-parse is done one document at a time,
    # so a work sits in exactly that state until every version of it has
    # been through -- and reporting a fabricated amendment on a public
    # register of the law is worse than reporting no history at all.
    parsers = {_parse_field(slug, "parser_version") for slug in slugs}
    if len(parsers) > 1:
        result["mixed_parsers"] = True
        _timeline_cache[work] = (signature, result)
        return result
    if len(slugs) > 1:
        documents = []
        for slug in slugs:
            nodes = _parse_field(slug, "nodes", []) or []
            meta = _act_version(slug)
            _work, version = split_document_slug(slug)
            documents.append({
                "version": version,
                "as_at": meta.get("as_at"),
                "as_at_printed": meta.get("as_at_printed"),
                "slug": slug,
                "nodes": nodes,
            })
        result["entries"] = diffing.build_timeline(documents)
    _timeline_cache[work] = (signature, result)
    return result


def _superseded(slug: str) -> "dict | None":
    """Whether this version has been overtaken, and by which -- None for a
    document that is not version-tracked, or is itself the current one.

    "Current" is the highest version number held here, which is the
    strongest claim this tool can make: it knows what it has been given,
    not what the Chief Parliamentary Counsel published this morning. The
    banner says "the versions held here" for that reason."""
    work, version = split_document_slug(slug)
    if version is None:
        return None
    slugs = _work_versions(work)
    if len(slugs) < 2:
        return None
    current_slug = slugs[-1]
    _w, current = split_document_slug(current_slug)
    if current is None or version >= current:
        return None
    return {
        "version": version,
        "current": current,
        "current_url": f"/browse/{current_slug}/",
        "as_at_printed": _act_version(slug).get("as_at_printed"),
    }


def _version_dates(slug: str) -> dict:
    """{version number -> the date that version states it incorporates
    amendments to}, for every parsed version of this slug's work.

    What the "Compare with another version" choices are labelled with: a
    bare "Version 23" asks a reader to know the numbering, where a date is
    the thing they actually have in mind."""
    work, version = split_document_slug(slug)
    if version is None:
        return {}
    dates = {}
    for other in _work_versions(work):
        _w, other_version = split_document_slug(other)
        as_at = _act_version(other).get("as_at_printed")
        if other_version is not None and as_at:
            dates[other_version] = as_at
    return dates


def _provision_page_url(slug: str, page_index: dict, entry: dict) -> "str | None":
    """Where to read one provision in one version, or None where that
    version gives it no page of its own.

    build_page_index's by_key is keyed by kind as well as (Schedule,
    number) for exactly this lookup: a Schedule, a Part or a Division can
    be amended in its own right (see diffing's container types), and a
    number alone would find the *section* of that number instead --
    "Schedule 3" linked to section 3, a different provision entirely.
    Most containers still have no page of their own and so no entry here
    at all (returns None); a Schedule whose own content earned it a page
    (see hierarchy.schedule_is_pageable) does.
    """
    page = page_index["by_key"].get(diffing.provision_identity(entry["type"], entry.get("schedule"), entry["number"]))
    return _url(f"/browse/{slug}/section/{page}") if page else None


def _provision_timeline(slug: str, number: "str | None", schedule: "str | None",
                        node_type: str = "section") -> tuple[list[dict], dict]:
    """One provision's timeline entries, and {version -> the URL of that
    same provision in that version}, so a reader can go and read the words
    in place rather than only in the diff.

    A version whose parse doesn't page that provision (it may not have
    existed yet) simply gets no link -- an entry that says a provision was
    inserted at version 112 must not offer a link into version 111.

    The URLs are worked out whether or not the provision has any timeline
    entries: a provision whose words never changed has no entries at all,
    and is exactly the one a reader checking "was this always like this?"
    wants to be able to open in another version."""
    if not number:
        return [], {}
    work, _version = split_document_slug(slug)
    timeline = _timeline(work)
    if timeline.get("mixed_parsers"):
        # Nothing can honestly be said about this provision's history
        # until every version has been read by the same parser.
        return [], {}
    key = diffing.provision_identity(node_type, schedule, number)
    entries = timeline["entries"].get(key) or []
    urls = {}
    probe = {"type": node_type, "schedule": schedule, "number": number}
    for other in timeline["slugs"]:
        _w, other_version = split_document_slug(other)
        url = _provision_page_url(other, _page_index(other), probe)
        if url:
            urls[other_version] = url
    return entries, urls


_act_title_cache: dict[str, str] = {}


# _detect_act_citation looks for an Act's own "Xxx Act YYYY" citation
# block, which a Bill or an Explanatory Memorandum simply doesn't have --
# so those used to fall back to their raw slug, and a Bill's browse page
# was headed "criminal-procedure-bill-2008". The slug is derived from the
# source PDF's filename, which for these documents is already the
# document's name; turning the hyphens back into spaces recovers a
# readable title without guessing at anything.
_EM_SLUG_SUFFIX = "-em"


# How an Explanatory Memorandum's title says what it is. One string, so
# the name is the same whether it came from the parse or from the slug.
_EM_TITLE_SUFFIX = " \u2014 Explanatory Memorandum"


def _title_from_slug(slug: str) -> str:
    name = slug[: -len(_EM_SLUG_SUFFIX)] if slug.endswith(_EM_SLUG_SUFFIX) else slug
    title = " ".join(word if word.isdigit() else word.capitalize() for word in name.split("-"))
    return f"{title}{_EM_TITLE_SUFFIX}" if slug.endswith(_EM_SLUG_SUFFIX) else title


def _parse_field(slug: str, key: str, default=None):
    """One top-level field of this Act's parse. The parse is a few MB, and
    browse pages want two small things out of it (the title and the
    version block) on every request -- so both go through here and through
    _act_title's cache rather than each re-reading the file."""
    parsed_path = BASE_DIR / "data" / "parsed" / f"{slug}.json"
    if not parsed_path.exists():
        return default
    try:
        return json.loads(parsed_path.read_text(encoding="utf-8")).get(key, default)
    except (OSError, ValueError):
        return default


def _act_version(slug: str) -> dict:
    """Which version of the Act this pipeline's own parse is -- as read
    off the PDF's front matter (see corpus/versions.py), never
    presented as "the Authorised Version" itself. Empty for a Bill, an
    Explanatory Memorandum, or a parse made before the pipeline recorded
    it."""
    return _parse_field(slug, "version") or {}


def _act_title(slug: str) -> str:
    """The Act's own "Xxx Act YYYY" citation, as the pipeline read it off
    the PDF's front matter and recorded in the parse.

    This used to re-extract the *whole* source PDF through the body-line
    pipeline on every browse request to read one line off page 1 -- hence
    the cache. The parse now carries it (run_pipeline.py records the whole
    front-matter block), so the file read is a small JSON one; the cache
    stays because browse_index/browse_section call this per page view.
    A parse made before that field existed still falls back to reading the
    PDF, and a Bill or EM -- which prints no such citation at all -- to a
    title derived from its own slug."""
    if slug in _act_title_cache:
        return _act_title_cache[slug]

    title = (_act_version(slug) or {}).get("title")
    if not title:
        from corpus.exporters.akn_export import _detect_act_citation

        title = _detect_act_citation(_parse_field(slug, "source")).get("title")
    title = title or _title_from_slug(slug)
    title = _named_as_an_em(slug, title)
    _act_title_cache[slug] = title
    return title


def _named_as_an_em(slug: str, title: str) -> str:
    """An Explanatory Memorandum, said so in its own name.

    An EM's front matter names the Bill it is about, so the title read off
    it is the Bill's: "Criminal Procedure Bill 2008" for both documents,
    with nothing to tell them apart. On an Act's contents page that put
    two identical links side by side, and on the EM's own page it meant
    the heading named a different document entirely.

    _title_from_slug has always added this, but only reached an EM whose
    parse recorded no title at all -- so the careful case was the one that
    never ran."""
    if _document_kind(slug) != "em" or _EM_TITLE_SUFFIX.lower() in title.lower():
        return title
    return f"{title}{_EM_TITLE_SUFFIX}"


_KIND_LABELS = {"act": "Act", "bill": "Bill", "em": "Explanatory Memorandum"}


def _document_kind(slug: str) -> str:
    """"act" / "bill" / "em", from what the pipeline recorded when it
    parsed this document (run_pipeline.py and run_em_pipeline.py both
    write document_type). Anything parsed before that was recorded reads
    as an Act, which is what it will have been."""
    parsed_path = BASE_DIR / "data" / "parsed" / f"{slug}.json"
    try:
        return json.loads(parsed_path.read_text(encoding="utf-8")).get("document_type") or "act"
    except (OSError, ValueError):
        return "act"


def _preview_bar(slug: str) -> str:
    kind = _KIND_LABELS.get(_document_kind(slug), "Act")
    return (
        '<div class="previewbar">'
        f"Live preview of this {kind} &mdash; reflects your saved review progress &middot; "
        f'<a href="{_url("/")}">Dashboard</a> &middot; '
        f'<a href="{_url(f"/review/{slug}/")}">Review</a>'
        "</div>"
    )


_LEGISLATION_CITATION_RE = re.compile(r"^(\d+)(?:-(\d{4}))?$")


def _resolve_legislation_citation(citation: str) -> dict:
    """What is known about a bare citation like "68-2009" or "999" -- the
    Act number, and the year if the citation carried one, since that is
    all a margin note or a body reference ever actually states.

    Checked against the general Act registry for a title (act_registry.py
    covers essentially every Victorian Act ever passed, in or out of
    force), then against known_acts.yaml for the slug of one this
    pipeline has actually parsed. Returns {"act_no", "year", "title",
    "in_force", "slug"} -- every key past act_no is None where that much
    isn't known, which is itself a valid, common answer: most citations
    this pipeline meets are never going to be parsed here, and this
    function's job is only to say what it can, not to guess."""
    match = _LEGISLATION_CITATION_RE.match(citation)
    if not match:
        return {"act_no": None, "year": None, "title": None, "in_force": None, "slug": None}
    act_no, year = match.group(1), int(match.group(2)) if match.group(2) else None

    title, in_force = None, None
    for candidate_title, entry in load_act_registry().items():
        if str(entry.get("act_no")) != act_no:
            continue
        # act_registry.json stores "year" as a string ("1991", not 1991) --
        # compared as one here too, rather than silently never matching a
        # citation that carried a year at all.
        entry_year = int(entry["year"]) if entry.get("year") not in (None, "") else None
        if year is not None and entry_year != year:
            continue
        title, in_force = candidate_title, entry.get("in_force")
        if year is None:
            year = entry_year
        break

    slug = next((s for s, t in load_known_acts().items() if t == title), None) if title else None
    return {"act_no": act_no, "year": year, "title": title, "in_force": in_force, "slug": slug}


@app.get("/legislation/{citation}", response_class=HTMLResponse)
def legislation_resolver(citation: str):
    """The one stable address this pipeline uses for referring to a piece
    of legislation by its own Act number, whether or not it has been
    parsed yet -- the target every citation this pipeline detects but
    cannot yet link into more specifically should point at (see
    corpus/amendments.py's linkify_note and html_view.py's
    _linked_citation_html), so a reader always has something to click
    rather than inert text, and a citation that gets parsed later starts
    resolving properly without anything that already links here needing
    to change.

    Redirects straight to the parsed document where one exists.
    Otherwise renders a plain explanation instead of a bare 404: a reader
    arriving here followed a link this pipeline itself made, and deserves
    to know why it didn't go anywhere, not a framework's generic error
    page."""
    info = _resolve_legislation_citation(citation)
    if info["slug"] and (BASE_DIR / "data" / "parsed" / f"{info['slug']}.json").exists():
        return RedirectResponse(_url(f"/browse/{info['slug']}/"))

    if info["title"]:
        cite = f"No. {info['act_no']} of {info['year']}" if info["year"] else f"No. {info['act_no']}"
        body = (
            f"<h1>{html.escape(info['title'])}</h1>"
            f"<p>{html.escape(cite)} has not been parsed into this pipeline yet.</p>"
        )
        if info["in_force"] is False:
            body += "<p>This Act is no longer in force.</p>"
    else:
        body = (
            "<h1>Unrecognised citation</h1>"
            f"<p>{html.escape(citation)} does not match a known Victorian Act.</p>"
        )
    return HTMLResponse(html_view.page_shell("Not parsed yet", body), status_code=404)


# What corpus/reader.py reads the document data through: this module
# itself, which owns those lookups and their caches. Named here rather
# than repeated at each call site, and passed rather than imported so
# that reader.py has no opinion about who is asking.
_SOURCE = sys.modules[__name__]

# The same read-only handle on the index the public site uses, which
# reopens when a rebuild replaces the file underneath it. It matters more
# here than there: this is the process that does the replacing.
_SEARCH = search.Index(BASE_DIR)


def _index() -> "search.Index":
    """The handle, against whatever BASE_DIR is now.

    Made afresh when that moves, because one built at import time goes on
    reading the directory it was born in -- which is the real one, from a
    test that carefully pointed everything else at a temporary copy."""
    global _SEARCH
    if _SEARCH.path != search.index_path(BASE_DIR):
        _SEARCH = search.Index(BASE_DIR)
    return _SEARCH


@app.get("/browse/{slug}")
def browse_redirect(slug: str):
    _validate_slug(slug)
    return RedirectResponse(_url(f"/browse/{slug}/"))


@app.get("/browse/{slug}/", response_class=HTMLResponse)
def browse_index(slug: str):
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    title = _act_title(slug)
    body = reader.contents_page(
        _SOURCE, slug, _url(f"/browse/{slug}"),
        related=[
            {"slug": d["slug"], "kind": d["kind"], "title": _act_title(d["slug"]),
             "href": f"/browse/{d['slug']}/"}
            for d in related_documents(slug)
        ],
    )
    return HTMLResponse(html_view.page_shell(
        title, body, _preview_bar(slug), base_url=_url(f"/browse/{slug}")))


@app.get("/browse/{slug}/section/{section_slug}", response_class=HTMLResponse)
def browse_section(slug: str, section_slug: str):
    # section_slug isn't an act slug -- it comes from assign_filenames'
    # per-Section ids (e.g. "s12", "s12_2" for a disambiguated repeat),
    # which can contain underscores that _validate_slug's pattern rejects.
    # It never touches the filesystem: html_view.render_section only
    # compares it in-memory against computed section ids and returns None
    # (-> 404) for anything that doesn't match a real one.
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    title = _act_title(slug)
    body = reader.section_page(_SOURCE, slug, _url(f"/browse/{slug}"), section_slug)
    if body is None:
        raise HTTPException(404, f"No such section {section_slug!r} in {slug!r}")
    return HTMLResponse(html_view.page_shell(
        title, body, _preview_bar(slug), base_url=_url(f"/browse/{slug}"), reader=True))


@app.get("/browse/{slug}/endnotes", response_class=HTMLResponse)
def browse_endnotes(slug: str):
    """The Act's own Endnotes -- General information, the Table of
    Amendments read as a real table, and Explanatory details. 404s for a
    document that has none (a Bill, an Explanatory Memorandum, or an Act
    parsed before corpus/endnotes.py existed -- re-parse it)."""
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    title = _act_title(slug)
    body = reader.endnotes_page(_SOURCE, slug, _url(f"/browse/{slug}"))
    if body is None:
        raise HTTPException(404, f"{slug!r} has no endnotes -- re-parse it if it's an Act.")
    return HTMLResponse(html_view.page_shell(
        f"{title} \u2014 Endnotes", body, _preview_bar(slug),
        base_url=_url(f"/browse/{slug}")))


@app.get("/api/browse/{slug}/preview")
def browse_preview(slug: str, section: str | None = None, fragment: str | None = None):
    """Backs the hover cards on a browse page: the content one link leads
    to, small enough to read without leaving the page. `section` and
    `fragment` are the two halves of a link the page itself rendered (see
    html_view.render_preview) -- neither touches the filesystem, both are
    only ever matched in memory against computed ids, so an unknown one is
    a plain 404 and the card simply doesn't appear."""
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    preview = html_view.render_preview(_parsed(slug), _act_title(slug), section, fragment)
    if preview is None:
        raise HTTPException(404, "No such link target")
    return preview


@app.get("/review/{slug}")
def review_redirect(slug: str):
    _validate_slug(slug)
    return RedirectResponse(_url(f"/review/{slug}/"))


@app.api_route("/review/{slug}/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def review_proxy(slug: str, path: str, request: Request):
    _validate_slug(slug)
    port = _ensure_review_process(slug)
    body = await request.body()
    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    async with httpx.AsyncClient() as client:
        try:
            upstream = await client.request(
                request.method,
                f"http://127.0.0.1:{port}/{path}",
                params=request.query_params,
                content=body,
                headers=forward_headers,
                timeout=30.0,
            )
        except httpx.ConnectError:
            raise HTTPException(502, f"review server for {slug!r} isn't reachable")
    excluded = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


def _resolve_auth(cli_username: str | None, cli_password: str | None) -> bool:
    """Decides where the running login state comes from, in priority order:
    1. An explicit --username/--password or DASHBOARD_USERNAME/
       DASHBOARD_PASSWORD -- an operator setting these has clearly chosen
       their own credentials, so no forced change and nothing gets written
       to the on-disk store (an env var is the source of truth for this
       run; it shouldn't get silently overwritten by a later in-app
       password change, or vice versa).
    2. A previous run's .dashboard_auth.json (see _load_auth_store) -- so
       a password set via the forced first-login change, or "change
       password" later, survives a restart.
    3. Neither -- first-ever run with no operator-supplied credentials:
       the built-in placeholder, forced to be changed before anything
       else works.
    Returns True if the built-in placeholder credentials are the ones in
    effect (used only to decide whether to print the exposure warning
    below)."""
    explicit_username = cli_username or os.environ.get("DASHBOARD_USERNAME")
    explicit_password = cli_password or os.environ.get("DASHBOARD_PASSWORD")

    if explicit_password:
        _configure_auth(explicit_username or _DEFAULT_USERNAME, explicit_password, must_change=False)
        return False

    store = _load_auth_store()
    if store is not None:
        _set_auth_state(store["username"], store["salt"], store["hash"], store["must_change_password"])
        return False

    _configure_auth(_DEFAULT_USERNAME, _DEFAULT_PASSWORD, must_change=True)
    _save_auth_store()
    return True


def _seed_publication_if_new() -> None:
    """The one moment the publication table arrives in a database that
    predates it.

    Until now, everything parsed was on the published site. Starting with
    an empty table would take all of it down at once, which is not a
    decision anybody made -- so the first run records what was already
    showing as published, and every work parsed after that starts off
    until somebody says otherwise.

    Only when the table has never been written. A row is never deleted,
    including for a work taken down on purpose, so this cannot fire twice
    and quietly republish something that was withdrawn."""
    if db.load_publication(BASE_DIR):
        return
    works = [split_document_slug(slug)[0] for slug in discover_slugs()
             if (BASE_DIR / "data" / "parsed" / f"{slug}.json").exists()]
    seeded = db.seed_publication(works, BASE_DIR)
    if seeded:
        print(f"{seeded} work(s) recorded as already on the public site. "
              f"Change that per work on the dashboard.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 to accept remote connections")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--username", default=None, help="login username; also read from DASHBOARD_USERNAME; defaults to a placeholder that must be changed on first login")
    ap.add_argument("--password", default=None, help="login password; also read from DASHBOARD_PASSWORD; defaults to a placeholder that must be changed on first login")
    ap.add_argument("--no-auth", action="store_true", help="disable the login gate entirely -- only ever use this on a strictly loopback-only run")
    ap.add_argument("--base-path", default=os.environ.get("DASHBOARD_BASE_PATH", ""),
                    help="path this is served under when it shares a domain with something else, "
                         "e.g. /admin for corpusvic.au/admin; also read from DASHBOARD_BASE_PATH "
                         "(default: the domain root)")
    args = ap.parse_args()

    base_path = _configure_base_path(args.base_path)

    if args.no_auth:
        print("WARNING: --no-auth set -- this dashboard has no login gate. Do not bind it to a non-loopback host like this.", file=sys.stderr)
    else:
        using_placeholder_creds = _resolve_auth(args.username, args.password)
        if args.host != "127.0.0.1" and using_placeholder_creds:
            print(
                "WARNING: binding to a non-loopback host while still on the built-in placeholder username/password -- "
                "anyone who can reach this host and port before you complete the forced first-login password change "
                "can sign in with it. Prefer --username/--password (or DASHBOARD_USERNAME/DASHBOARD_PASSWORD) set to "
                "your own credentials before exposing this beyond your own machine.",
                file=sys.stderr,
            )

    import uvicorn

    _seed_publication_if_new()

    print(f"{len(discover_slugs())} Act(s)/Bill(s)/EM(s) known.")
    where = args.host if args.host != "0.0.0.0" else "<this-machine-address>"
    print(f"Open http://{where}:{args.port}{base_path}/ in a browser.")
    try:
        uvicorn.run(serving_app(), host=args.host, port=args.port, log_level="warning")
    finally:
        _shutdown_review_processes()
        _shutdown_ai_scan_processes()


if __name__ == "__main__":
    main()
