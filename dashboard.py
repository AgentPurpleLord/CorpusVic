"""
Single web-server entry point for the whole pipeline: a dashboard for
picking which tool to run against which Act (add a new Act/Bill/EM, run
review.py, export AKN/Markdown, link a Bill to its Act), plus the actual
review GUI itself, all served from one process/port so the whole thing can
be run remotely behind a single exposed port.

Usage:
    python dashboard.py
    python dashboard.py --host 0.0.0.0 --port 8000
    python dashboard.py --host 0.0.0.0 --port 8000 --token <shared-secret>

--host 0.0.0.0 is what makes this reachable from outside the machine it
runs on (the default, 127.0.0.1, is loopback-only). Anything bound to
0.0.0.0 is reachable by anyone who can reach the host on that port, and
this dashboard can both upload PDFs and shell out to the pipeline scripts
-- so pass --token (or set the REVIEW_TOKEN env var) whenever --host isn't
127.0.0.1. Once set, every page redirects to a one-time login form; the
browser then carries an httponly cookie for subsequent requests, so
nothing else on this site needs to know the token exists.

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
import json
import os
import re
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

from ai_pipeline.extract import slugify
from review import _resume_point, group_into_units

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
    status = {
        "slug": slug,
        "has_pdf": has_pdf,
        "parsed": parsed_path.exists(),
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
    nodes = data.get("nodes", [])
    units = group_into_units(nodes)
    status["node_count"] = len(nodes)
    status["unit_count"] = len(units)

    verified_path = BASE_DIR / "data" / "verified" / f"{slug}.json"
    verified = json.loads(verified_path.read_text(encoding="utf-8")) if verified_path.exists() else []
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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Legislation pipeline dashboard")

_AUTH_TOKEN: str | None = None
_COOKIE_NAME = "review_token"

_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Sign in</title>
<style>
body{font-family:ui-sans-serif,system-ui,sans-serif;background:#f5f5f4;display:flex;
  align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#fff;border:1px solid #d7d7d7;border-radius:8px;padding:24px;width:280px}
h1{font-size:15px;margin:0 0 14px}
input{width:100%;padding:7px 9px;border:1px solid #d7d7d7;border-radius:6px;font-size:13px;box-sizing:border-box}
button{margin-top:10px;width:100%;padding:8px;border:0;border-radius:6px;background:#2b6cb0;color:#fff;
  font-size:13px;cursor:pointer}
#err{color:#b91c1c;font-size:12px;min-height:16px;margin-top:6px}
</style></head><body>
<form id="f">
  <h1>Legislation pipeline dashboard</h1>
  <input type="password" id="token" placeholder="Access token" autofocus>
  <button type="submit">Sign in</button>
  <div id="err"></div>
</form>
<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await fetch("/api/login", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({token: document.getElementById("token").value}),
  });
  if (res.ok) { location.href = "/"; }
  else { document.getElementById("err").textContent = "Wrong token."; }
});
</script>
</body></html>"""


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if not _AUTH_TOKEN:
        return await call_next(request)
    if request.url.path in ("/login", "/api/login"):
        return await call_next(request)
    if request.cookies.get(_COOKIE_NAME) == _AUTH_TOKEN:
        return await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/review/"):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return RedirectResponse("/login")


class LoginRequest(BaseModel):
    token: str


@app.get("/login")
def login_page():
    return HTMLResponse(_LOGIN_HTML)


@app.post("/api/login")
def do_login(req: LoginRequest):
    if not _AUTH_TOKEN or req.token != _AUTH_TOKEN:
        raise HTTPException(401, "Wrong token")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE_NAME, _AUTH_TOKEN, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/acts")
def list_acts():
    return [act_status(slug) for slug in discover_slugs()]


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
    if kind not in ("act", "bill", "em"):
        raise HTTPException(400, f"Invalid kind: {kind!r}")
    if engine not in ("rules", "ai"):
        raise HTTPException(400, f"Invalid engine: {engine!r}")
    if backend not in ("ollama", "claude"):
        raise HTTPException(400, f"Invalid backend: {backend!r}")
    if not (pdf.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")
    for field_name, value in (("profile", profile), ("start_page", start_page), ("end_page", end_page)):
        if value.strip() and field_name == "profile" and not _SLUG_RE.match(value.strip()):
            raise HTTPException(400, f"Invalid profile name: {value!r}")
        if field_name in ("start_page", "end_page") and value.strip() and not value.strip().isdigit():
            raise HTTPException(400, f"{field_name} must be a positive integer")

    acts_dir = BASE_DIR / "acts"
    acts_dir.mkdir(parents=True, exist_ok=True)
    dest = acts_dir / Path(pdf.filename).name  # .name strips any directory components
    dest.write_bytes(await pdf.read())
    slug = slugify(dest.stem)

    if kind == "em":
        cmd = [sys.executable, "run_em_pipeline.py", str(dest)]
    else:
        cmd = [sys.executable, "run_pipeline.py", str(dest), "--document-type", "bill" if kind == "bill" else "act"]
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

    try:
        result = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "slug": slug, "log": f"Timed out after 30 minutes.\n{e.stdout or ''}\n{e.stderr or ''}"}
    return {"ok": result.returncode == 0, "slug": slug, "returncode": result.returncode, "log": result.stdout + result.stderr}


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


def main():
    global _AUTH_TOKEN

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 to accept remote connections")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--token", default=os.environ.get("REVIEW_TOKEN"), help="shared secret required to use the dashboard; also read from REVIEW_TOKEN")
    args = ap.parse_args()

    _AUTH_TOKEN = args.token
    if args.host != "127.0.0.1" and not _AUTH_TOKEN:
        print(
            "WARNING: binding to a non-loopback host with no --token/REVIEW_TOKEN set -- "
            "this dashboard will be reachable, and usable, by anyone who can reach this host and port.",
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
