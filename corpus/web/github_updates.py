"""The pull requests waiting to go live, and merging one, from the
dashboard's Updates panel.

Getting new code onto the server meant a terminal every time: merge on
GitHub, pull, install, restart. The pull and the restarts were already
the dashboard's (corpus/review/sync.py, dashboard._restart_public); this
is the GitHub half, through its REST API with a token the server holds in
deploy/dashboard.env as GITHUB_TOKEN. The token never leaves this module:
nothing here returns it, and nothing logs it.
"""
import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.github.com"
_TIMEOUT = 20


class UpdateError(Exception):
    pass


def token() -> "str | None":
    return os.environ.get("GITHUB_TOKEN") or None


def repo_of(remote_url: str) -> "str | None":
    """"owner/name" from an origin remote in either form git writes:
    https://github.com/o/n(.git) or git@github.com:o/n(.git). Credentials
    in the URL are not part of the answer."""
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", remote_url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _request(method: str, path: str, body: "dict | None" = None, opener=None):
    key = token()
    if not key:
        raise UpdateError("No GitHub token: add GITHUB_TOKEN=... to deploy/dashboard.env and restart the dashboard.")
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "corpusvic-dashboard",
                 **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=_TIMEOUT) as res:
            raw = res.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read() or b"{}").get("message") or ""
        except ValueError:
            said = ""
        if e.code in (401, 403):
            raise UpdateError(f"The GitHub token can't do this here ({said or e.code}). It needs pull requests "
                              "and contents read and write on this repository.") from e
        if e.code in (404, 405, 409, 422):
            raise UpdateError(said or f"GitHub refused ({e.code}).") from e
        raise UpdateError(f"GitHub answered {e.code}: {said}") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"GitHub didn't answer: {getattr(e, 'reason', e)}") from e


def _checks(repo: str, sha: str, opener=None) -> str:
    """"passing", "failing", "pending" or "none", from the head commit's
    check runs (Actions) and its commit statuses together."""
    runs = _request("GET", f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", opener=opener).get("check_runs") or []
    status = _request("GET", f"/repos/{repo}/commits/{sha}/status", opener=opener)
    states = [r.get("conclusion") or "pending" for r in runs] + [s.get("state") for s in status.get("statuses") or []]
    if not states:
        return "none"
    if any(s in ("failure", "error", "timed_out", "cancelled", "action_required") for s in states):
        return "failing"
    if any(s in ("pending", None, "queued", "in_progress") for s in states):
        return "pending"
    return "passing"


def open_pulls(repo: str, base: str, opener=None) -> list[dict]:
    """The open pull requests into `base`, newest change first."""
    pulls = _request("GET", f"/repos/{repo}/pulls?state=open&base={base}&sort=updated&direction=desc&per_page=20",
                     opener=opener)
    out = []
    for p in pulls:
        detail = _request("GET", f"/repos/{repo}/pulls/{p['number']}", opener=opener)
        out.append({
            "number": p["number"], "title": p.get("title") or "", "head": (p.get("head") or {}).get("ref"),
            "sha": (p.get("head") or {}).get("sha"), "author": (p.get("user") or {}).get("login"),
            "updated": p.get("updated_at"), "url": p.get("html_url"), "draft": bool(p.get("draft")),
            # null while GitHub is still working it out; the page says so.
            "mergeable": detail.get("mergeable"), "mergeable_state": detail.get("mergeable_state"),
            "checks": _checks(repo, (p.get("head") or {}).get("sha") or "", opener=opener),
        })
    return out


def merge(repo: str, number: int, sha: str, opener=None) -> dict:
    """Merges pull request `number` as GitHub's Merge button does. `sha`
    is the head the reviewer saw: a branch pushed to since is refused
    rather than merged unseen."""
    result = _request("PUT", f"/repos/{repo}/pulls/{number}/merge",
                      {"merge_method": "merge", "sha": sha}, opener=opener)
    return {"merged": bool(result.get("merged")), "message": result.get("message") or "Merged."}
