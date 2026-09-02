"""
Single web-server entry point for the whole pipeline: a dashboard for
picking which tool to run against which Act (add a new Act/Bill/EM, run
review.py, export AKN/Markdown, link a Bill to its Act), plus the actual
review GUI itself, all served from one process/port so the whole thing can
be run remotely behind a single exposed port.

Usage:
    python dashboard.py
    python dashboard.py --host 0.0.0.0 --port 8000
    python dashboard.py --host 0.0.0.0 --port 8000 --username alice --password <a-real-password>

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
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from ai_pipeline import db, html_view
from ai_pipeline.act_registry import load_act_registry
from ai_pipeline.amendments import build_amendment_index, summarise_by_act
from ai_pipeline.commentary import build_commentary_index
from ai_pipeline.extract import slugify
from review import _resume_point, build_current_nodes, group_into_units

BASE_DIR = Path(__file__).parent
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
    parsed_dir = BASE_DIR / "data" / "ai_parsed"
    if parsed_dir.exists():
        for p in parsed_dir.glob("*.json"):
            slugs.add(p.stem)
    return sorted(slugs)


def act_status(slug: str) -> dict:
    acts_dir = BASE_DIR / "acts"
    has_pdf = acts_dir.exists() and any(acts_dir.glob(f"{slug}.*"))
    parsed_path = BASE_DIR / "data" / "ai_parsed" / f"{slug}.json"
    profiles_dir = BASE_DIR / "ai_pipeline" / "profiles"
    status = {
        "slug": slug,
        "has_pdf": has_pdf,
        # Whether this Act has a pattern profile of its own to pass to
        # run_pipeline.py -- the reparse modal pre-fills it, since
        # forgetting it silently produces a worse parse (the Criminal
        # Procedure Act's own Parts fall through to generic heading_group
        # nodes without it -- see its profile's own comment) rather than
        # any kind of error.
        "has_profile": any((profiles_dir / f"{slug}{ext}").exists() for ext in (".yaml", ".yml")),
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

    if not (BASE_DIR / "data" / "ai_parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "review.py", slug, "--port", str(port)],
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
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Legislation pipeline dashboard")

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


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
  const res = await fetch("/api/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    }),
  });
  if (res.ok) { location.href = "/"; return; }
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
  const res = await fetch("/api/change-password", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({current_password: document.getElementById("current").value, new_password: new1}),
  });
  if (res.ok) { location.href = "/"; return; }
  const data = await res.json().catch(() => ({}));
  err.textContent = data.detail || "Couldn't change password.";
});
</script>
</body></html>"""


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if _DASHBOARD_USERNAME is None:
        return await call_next(request)

    path = request.url.path
    if path in ("/login", "/api/login"):
        return await call_next(request)

    if not _session_is_valid(request.cookies.get(_COOKIE_NAME)):
        if path.startswith("/api/") or path.startswith("/review/"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login")

    if _MUST_CHANGE_PASSWORD and path not in ("/change-password", "/api/change-password", "/api/logout"):
        if path.startswith("/api/") or path.startswith("/review/"):
            return JSONResponse({"detail": "password change required"}, status_code=403)
        return RedirectResponse("/change-password")

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


@app.post("/api/login")
def do_login(req: LoginRequest, request: Request):
    ip = _client_ip(request)
    if _is_locked_out(ip):
        raise HTTPException(429, "Too many failed attempts -- try again later.")
    if not _check_credentials(req.username, req.password):
        _record_failed_login(ip)
        raise HTTPException(401, "Invalid username or password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE_NAME, _new_session(), httponly=True, samesite="lax", max_age=_SESSION_LIFETIME_SECONDS)
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


@app.post("/api/logout")
def do_logout(request: Request):
    token = request.cookies.get(_COOKIE_NAME)
    if token:
        _SESSIONS.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_COOKIE_NAME)
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/acts")
def list_acts():
    return [act_status(slug) for slug in discover_slugs()]


def _validate_parse_params(kind: str, profile: str, engine: str, backend: str, start_page: str, end_page: str) -> None:
    if kind not in ("act", "bill", "em"):
        raise HTTPException(400, f"Invalid kind: {kind!r}")
    if engine not in ("rules", "ai"):
        raise HTTPException(400, f"Invalid engine: {engine!r}")
    if backend not in ("ollama", "claude"):
        raise HTTPException(400, f"Invalid backend: {backend!r}")
    if profile.strip() and not _SLUG_RE.match(profile.strip()):
        raise HTTPException(400, f"Invalid profile name: {profile!r}")
    for field_name, value in (("start_page", start_page), ("end_page", end_page)):
        if value.strip() and not value.strip().isdigit():
            raise HTTPException(400, f"{field_name} must be a positive integer")


def _repo_relative(path: Path) -> str:
    """run_pipeline.py records the PDF path it was given verbatim into
    data/ai_parsed/<slug>.json's own "source" field, and that file is
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


def _build_parse_command(
    pdf_path: Path, kind: str, profile: str, engine: str, backend: str, model: str, start_page: str, end_page: str
) -> list[str]:
    """Shared by new_act (a freshly uploaded PDF) and reparse_act (an
    already-uploaded one, re-run to pick up a different engine/profile or
    to regenerate after a parser change) -- same options either way, only
    which PDF path they point at differs."""
    source = _repo_relative(pdf_path)
    if kind == "em":
        return [sys.executable, "run_em_pipeline.py", source]
    cmd = [sys.executable, "run_pipeline.py", source, "--document-type", "bill" if kind == "bill" else "act"]
    if profile.strip():
        cmd += ["--profile", profile.strip()]
    if start_page.strip():
        cmd += ["--start-page", start_page.strip()]
    if end_page.strip():
        cmd += ["--end-page", end_page.strip()]
    if engine == "ai":
        cmd += ["--engine", "ai", "--backend", backend]
        if model.strip():
            cmd += ["--model", model.strip()]
    return cmd


def _run_parse_subprocess(cmd: list[str]) -> tuple[bool, "int | None", str]:
    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        return False, None, f"Timed out after 30 minutes.\n{e.stdout or ''}\n{e.stderr or ''}"
    return result.returncode == 0, result.returncode, result.stdout + result.stderr


def _find_source_pdf(slug: str) -> "Path | None":
    acts_dir = BASE_DIR / "acts"
    if not acts_dir.exists():
        return None
    matches = sorted(p for p in acts_dir.glob(f"{slug}.*") if p.suffix.lower() == ".pdf")
    return matches[0] if matches else None


@app.post("/api/acts/new")
async def new_act(
    pdf: UploadFile = File(...),
    kind: str = Form("act"),
    profile: str = Form(""),
    engine: str = Form("rules"),
    backend: str = Form("ollama"),
    model: str = Form(""),
    start_page: str = Form(""),
    end_page: str = Form(""),
):
    _validate_parse_params(kind, profile, engine, backend, start_page, end_page)
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")

    acts_dir = BASE_DIR / "acts"
    acts_dir.mkdir(parents=True, exist_ok=True)
    dest = acts_dir / Path(pdf.filename).name  # .name strips any directory components
    dest.write_bytes(await pdf.read())
    slug = slugify(dest.stem)
    _act_title_cache.pop(slug, None)  # a re-upload under this slug may have a different title

    cmd = _build_parse_command(dest, kind, profile, engine, backend, model, start_page, end_page)
    ok, returncode, log = _run_parse_subprocess(cmd)
    return {"ok": ok, "slug": slug, "returncode": returncode, "log": log}


@app.post("/api/acts/{slug}/reparse")
def reparse_act(
    slug: str,
    kind: str = Form("act"),
    profile: str = Form(""),
    engine: str = Form("rules"),
    backend: str = Form("ollama"),
    model: str = Form(""),
    start_page: str = Form(""),
    end_page: str = Form(""),
    confirm: str = Form(""),
):
    """Re-runs the pipeline against an already-uploaded PDF -- no new
    upload needed -- so an Act can be re-parsed with a different engine
    (rules vs AI) or profile, or just regenerated after a parser code
    change, without starting over from "Add Act/Bill/EM".

    data/ai_parsed/<slug>.json is plain regenerable output on its own,
    but review.py's own verified rows in data/legislation.db are keyed by
    a *positional* index into that exact file (see .gitignore's own
    comment on why the two are committed as a pair) -- so overwriting it
    while real review progress already exists is exactly the "replacing
    the previous data" this needs an explicit nod for, not a silent
    default. Refuses (409) unless `confirm` is set, once there's any
    reviewed progress to actually put at risk."""
    _validate_slug(slug)
    _validate_parse_params(kind, profile, engine, backend, start_page, end_page)

    pdf_path = _find_source_pdf(slug)
    if pdf_path is None:
        raise HTTPException(404, f"No source PDF found for {slug!r} in acts/ -- add it via 'Add Act/Bill/EM' first.")

    status = act_status(slug)
    reviewed = status.get("reviewed_units") or 0
    if status["parsed"] and reviewed > 0 and confirm.strip().lower() != "true":
        raise HTTPException(
            409,
            f"{slug} has {reviewed} of {status['unit_count']} unit(s) already reviewed. Re-parsing regenerates "
            "the raw structure from the PDF -- if node positions shift, existing review progress can silently "
            "misalign with the wrong provisions. Confirm to replace it anyway.",
        )

    _act_title_cache.pop(slug, None)
    cmd = _build_parse_command(pdf_path, kind, profile, engine, backend, model, start_page, end_page)
    ok, returncode, log = _run_parse_subprocess(cmd)
    if ok:
        # The parse this Act's review.py process (if any) loaded into
        # memory at startup is now stale -- force a fresh one on the next
        # "Review" click rather than let it keep serving the old node
        # list against a database that may no longer line up with it.
        _kill_review_process(slug)
    return {"ok": ok, "slug": slug, "returncode": returncode, "log": log}


@app.post("/api/acts/{slug}/export/akn")
def export_akn(slug: str):
    _validate_slug(slug)
    result = subprocess.run([sys.executable, "export_akn.py", slug], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300)
    return {"ok": result.returncode == 0, "log": result.stdout + result.stderr}


@app.post("/api/acts/{slug}/export/markdown")
def export_markdown(slug: str):
    _validate_slug(slug)
    result = subprocess.run(
        [sys.executable, "export_markdown.py", slug], cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300
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
    cmd = [sys.executable, "run_bill_linking.py", bill_slug, act_slug]
    if em_slug.strip():
        _validate_slug(em_slug.strip())
        cmd += ["--em", em_slug.strip()]
    result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=300)
    return {"ok": result.returncode == 0, "log": result.stdout + result.stderr}


_current_nodes_cache: dict[str, tuple[tuple, tuple]] = {}


def _browse_state_signature(slug: str) -> tuple:
    """A cheap stamp of everything build_current_nodes reads: this Act's
    parse plus the review database (whose -wal file is where a write
    actually lands first under WAL, so the .db's own mtime alone would
    miss an edit a reviewer just made)."""
    paths = [
        BASE_DIR / "data" / "ai_parsed" / f"{slug}.json",
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
    if not (BASE_DIR / "data" / "ai_parsed" / f"{slug}.json").exists():
        index = {"by_node_index": {}, "by_number": {}}
    else:
        nodes, _unattached, hierarchy = _current_nodes(slug)
        index = html_view.build_page_index({"nodes": nodes, "hierarchy": hierarchy}, _act_title(slug))
    _page_index_cache[slug] = (signature, index)
    return index


def _section_crossrefs(act_slug: str, section_number: str | None) -> list[dict]:
    """The "Explained in" chips for one Act section: the Bill clause it was
    enacted from, and each Explanatory Memorandum note about it, as
    ordinary links into those documents' own browse pages (so the hover
    preview reads them like any other link). A related document that
    hasn't been parsed has no page to link to and is simply left out --
    the link record is about a document this pipeline may not hold."""
    if not section_number:
        return []
    entry = _commentary_index(act_slug).get(section_number.lower())
    if not entry:
        return []
    chips = []
    for bill in entry["bill"]:
        page = _page_index(bill["bill_slug"])["by_number"].get(str(bill["clause_number"]).lower())
        if not page:
            continue
        title = f"{_act_title(bill['bill_slug'])} \u2014 the clause this section was enacted from"
        if bill.get("status") == "flagged":
            title += f" (wording diverged; {bill['similarity']} text similarity -- worth checking)"
        chips.append({
            "kind": "bill",
            "label": f"Bill clause {bill['clause_number']}",
            "href": f"/browse/{bill['bill_slug']}/section/{page}",
            "title": title,
        })
    # An EM can carry more than one note about the same clause (a second
    # one after a Chapter heading, a pinpoint note on "clause 6(4)"), so
    # number the repeats rather than showing identical chips.
    seen_clause: dict[str, int] = {}
    for em in entry["em"]:
        page = _page_index(em["em_slug"])["by_node_index"].get(em["em_node_index"])
        if not page:
            continue
        if em["clause_number"]:
            nth = seen_clause[em["clause_number"]] = seen_clause.get(em["clause_number"], 0) + 1
            label = f"EM on clause {em['clause_number']}" + (f" ({nth})" if nth > 1 else "")
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
    parsed_path = BASE_DIR / "data" / "ai_parsed" / f"{slug}.json"
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


_act_title_cache: dict[str, str] = {}


# _detect_act_citation looks for an Act's own "Xxx Act YYYY" citation
# block, which a Bill or an Explanatory Memorandum simply doesn't have --
# so those used to fall back to their raw slug, and a Bill's browse page
# was headed "criminal-procedure-bill-2008". The slug is derived from the
# source PDF's filename, which for these documents is already the
# document's name; turning the hyphens back into spaces recovers a
# readable title without guessing at anything.
_EM_SLUG_SUFFIX = "-em"


def _title_from_slug(slug: str) -> str:
    name = slug[: -len(_EM_SLUG_SUFFIX)] if slug.endswith(_EM_SLUG_SUFFIX) else slug
    title = " ".join(word if word.isdigit() else word.capitalize() for word in name.split("-"))
    return f"{title} \u2014 Explanatory Memorandum" if slug.endswith(_EM_SLUG_SUFFIX) else title


def _act_title(slug: str) -> str:
    """_detect_act_citation re-extracts the *whole* source PDF (every
    page, via PyMuPDF) just to read the title off its first couple of
    pages -- fine as a one-off in export_akn.py/export_markdown.py, but
    browse_index/browse_section call this on every single page view, so
    without caching, a 500-page Act would re-run full PDF extraction on
    every click. Cached for this process's lifetime; new_act() clears a
    slug's entry after (re-)parsing it so a changed source PDF is picked
    up on the next browse request rather than staying stale forever."""
    if slug in _act_title_cache:
        return _act_title_cache[slug]

    from ai_pipeline.akn_export import _detect_act_citation

    parsed_path = BASE_DIR / "data" / "ai_parsed" / f"{slug}.json"
    source = None
    if parsed_path.exists():
        try:
            source = json.loads(parsed_path.read_text(encoding="utf-8")).get("source")
        except (OSError, ValueError):
            pass
    title = _detect_act_citation(source).get("title") or _title_from_slug(slug)
    _act_title_cache[slug] = title
    return title


_KIND_LABELS = {"act": "Act", "bill": "Bill", "em": "Explanatory Memorandum"}


def _document_kind(slug: str) -> str:
    """"act" / "bill" / "em", from what the pipeline recorded when it
    parsed this document (run_pipeline.py and run_em_pipeline.py both
    write document_type). Anything parsed before that was recorded reads
    as an Act, which is what it will have been."""
    parsed_path = BASE_DIR / "data" / "ai_parsed" / f"{slug}.json"
    try:
        return json.loads(parsed_path.read_text(encoding="utf-8")).get("document_type") or "act"
    except (OSError, ValueError):
        return "act"


def _preview_bar(slug: str) -> str:
    kind = _KIND_LABELS.get(_document_kind(slug), "Act")
    return (
        '<div class="previewbar">'
        f"Live preview of this {kind} &mdash; reflects your saved review progress, not just what's fully reviewed &middot; "
        f'<a href="/">Dashboard</a> &middot; <a href="/review/{slug}/">Review</a>'
        "</div>"
    )


@app.get("/browse/{slug}")
def browse_redirect(slug: str):
    _validate_slug(slug)
    return RedirectResponse(f"/browse/{slug}/")


@app.get("/browse/{slug}/", response_class=HTMLResponse)
def browse_index(slug: str):
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "ai_parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    nodes, _unattached, hierarchy = _current_nodes(slug)
    title = _act_title(slug)
    body = html_view.render_index(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": _amendments(slug)["endnotes"]},
        title, f"/browse/{slug}",
    )
    return HTMLResponse(html_view.page_shell(title, body, _preview_bar(slug), base_url=f"/browse/{slug}"))


@app.get("/browse/{slug}/section/{section_slug}", response_class=HTMLResponse)
def browse_section(slug: str, section_slug: str):
    # section_slug isn't an act slug -- it comes from assign_filenames'
    # per-Section ids (e.g. "s12", "s12_2" for a disambiguated repeat),
    # which can contain underscores that _validate_slug's pattern rejects.
    # It never touches the filesystem: html_view.render_section only
    # compares it in-memory against computed section ids and returns None
    # (-> 404) for anything that doesn't match a real one.
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "ai_parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    nodes, _unattached, hierarchy = _current_nodes(slug)
    title = _act_title(slug)
    # Which provision this page is, so its Bill/EM commentary can be looked
    # up by number (see ai_pipeline/commentary.py for why by number).
    by_node_index = _page_index(slug)["by_node_index"]
    node_index = next((i for i, page in by_node_index.items() if page == section_slug), None)
    section_number = nodes[node_index].get("number") if node_index is not None else None
    body = html_view.render_section(
        {"nodes": nodes, "hierarchy": hierarchy}, title, f"/browse/{slug}", section_slug,
        crossrefs=_section_crossrefs(slug, section_number),
        amendment_index=_amendments(slug)["index"],
    )
    if body is None:
        raise HTTPException(404, f"No such section {section_slug!r} in {slug!r}")
    return HTMLResponse(html_view.page_shell(title, body, _preview_bar(slug), base_url=f"/browse/{slug}"))


@app.get("/browse/{slug}/endnotes", response_class=HTMLResponse)
def browse_endnotes(slug: str):
    """The Act's own Endnotes -- General information, the Table of
    Amendments read as a real table, and Explanatory details. 404s for a
    document that has none (a Bill, an Explanatory Memorandum, or an Act
    parsed before ai_pipeline/endnotes.py existed -- re-parse it)."""
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "ai_parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    nodes, _unattached, hierarchy = _current_nodes(slug)
    amendments = _amendments(slug)
    title = _act_title(slug)
    body = html_view.render_endnotes(
        {"nodes": nodes, "hierarchy": hierarchy, "endnotes": amendments["endnotes"]},
        title, f"/browse/{slug}", amendments["summary"],
    )
    if body is None:
        raise HTTPException(404, f"{slug!r} has no endnotes -- re-parse it if it's an Act.")
    return HTMLResponse(html_view.page_shell(f"{title} \u2014 Endnotes", body, _preview_bar(slug), base_url=f"/browse/{slug}"))


@app.get("/api/browse/{slug}/preview")
def browse_preview(slug: str, section: str | None = None, fragment: str | None = None):
    """Backs the hover cards on a browse page: the content one link leads
    to, small enough to read without leaving the page. `section` and
    `fragment` are the two halves of a link the page itself rendered (see
    html_view.render_preview) -- neither touches the filesystem, both are
    only ever matched in memory against computed ids, so an unknown one is
    a plain 404 and the card simply doesn't appear."""
    _validate_slug(slug)
    if not (BASE_DIR / "data" / "ai_parsed" / f"{slug}.json").exists():
        raise HTTPException(404, f"{slug!r} hasn't been parsed yet -- add it first.")
    nodes, _unattached, hierarchy = _current_nodes(slug)
    preview = html_view.render_preview({"nodes": nodes, "hierarchy": hierarchy}, _act_title(slug), section, fragment)
    if preview is None:
        raise HTTPException(404, "No such link target")
    return preview


@app.get("/review/{slug}")
def review_redirect(slug: str):
    _validate_slug(slug)
    return RedirectResponse(f"/review/{slug}/")


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 to accept remote connections")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--username", default=None, help="login username; also read from DASHBOARD_USERNAME; defaults to a placeholder that must be changed on first login")
    ap.add_argument("--password", default=None, help="login password; also read from DASHBOARD_PASSWORD; defaults to a placeholder that must be changed on first login")
    ap.add_argument("--no-auth", action="store_true", help="disable the login gate entirely -- only ever use this on a strictly loopback-only run")
    args = ap.parse_args()

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

    print(f"{len(discover_slugs())} Act(s)/Bill(s)/EM(s) known.")
    print(f"Open http://{args.host if args.host != '0.0.0.0' else '<this-machine-address>'}:{args.port}/ in a browser.")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        _shutdown_review_processes()


if __name__ == "__main__":
    main()
