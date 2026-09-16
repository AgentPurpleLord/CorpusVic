"""
Committing and pushing the review work from wherever it is being done --
so that a reviewer on the server does not have to open a terminal on it
to put a day's decisions somewhere safe.

What travels is `data/`: the review database and the parses its rows are
keyed against (see .gitignore's own note on why those two are committed
together). Nothing else is staged, deliberately -- a button that pushed
whatever happened to be in the working tree would sooner or later publish
a half-finished edit someone left on the server.

Three things this does that a person typing the commands would have to
remember:

  - checkpoints the database's write-ahead log first, so what is
    committed is the whole state rather than whatever had been folded in
    (see checkpoint_db.py). The repository ships a pre-commit hook that
    does this too, but git hooks do not travel with a clone, so relying
    on it here would be relying on a manual step having been done;

  - refuses to push when the remote has commits this checkout does not,
    rather than forcing. The review database is one file synced whole --
    there is no merge -- so a force here would silently drop whichever
    side lost, which is exactly the accident this is meant to prevent;

  - never invents credentials. Pushing is whatever `git push` can already
    do from this checkout, which on the server is the deploy key. If that
    is not set up, the failure says so instead of appearing to work.
"""
import os
import re
import subprocess
from pathlib import Path

# Everything committed by a push from here. Anything else in the working
# tree is left exactly as it is.
TRACKED_PATHS = ("data",)

# Two budgets, because they fail for different reasons. Reading this
# checkout is local and near-instant, so a long wait there means
# something is wrong rather than slow. Anything touching the network is
# bounded much more tightly than it would be from a terminal: this runs
# while somebody is looking at a page, and a status line is worth a few
# seconds and not a few minutes.
_TIMEOUT_SECONDS = 30
_NETWORK_TIMEOUT_SECONDS = 15

# git must never wait for a human here. There is nobody at this end of
# it: an ssh asking whether to trust a new host, or a helper asking for a
# password, would sit until the timeout and report as "slow" rather than
# as the thing it is. BatchMode turns those prompts into immediate
# failures with a message worth reading.
_NON_INTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
}

# A remote can carry a token ("https://x-access-token:ghp_...@github.com/..."),
# and this address is shown in a web page. Whatever is between the scheme
# and the host goes.
_CREDENTIALS_RE = re.compile(r"(?<=://)[^/@]*@")


class SyncError(RuntimeError):
    """Something git said no to, phrased for whoever pressed the button."""


def _git(repo: Path, *args: str, network: bool = False) -> subprocess.CompletedProcess:
    """One git command. Never raises for a command that merely failed --
    that is the caller's to read off the result -- but a timeout or a
    missing git is turned into a SyncError here, so that every caller
    gets one kind of thing to handle rather than three."""
    try:
        return subprocess.run(
            ["git", *args], cwd=str(repo),
            capture_output=True, text=True,
            timeout=_NETWORK_TIMEOUT_SECONDS if network else _TIMEOUT_SECONDS,
            env={**os.environ, **_NON_INTERACTIVE},
        )
    except subprocess.TimeoutExpired as e:
        raise SyncError(
            f"git {args[0]} gave up after {e.timeout:.0f}s. "
            + ("The remote didn't answer in time." if network
               else "Something is holding this checkout open.")
        ) from e
    except FileNotFoundError as e:
        raise SyncError("git isn't installed, or isn't on this process's PATH.") from e


def _git_ok(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise SyncError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def safe_remote_url(url: str) -> str:
    return _CREDENTIALS_RE.sub("", url)


def pending_changes(repo: Path) -> list[str]:
    """The paths a push would commit: what has changed under `data/`, in
    git's own porcelain terms, with anything gitignored already excluded
    because git is the one answering.

    Read off the raw output rather than a stripped copy. Porcelain v1 is
    two status columns and a space before the path, so stripping the
    output first takes the leading space off the first line with it and
    every path after that loses its first letter."""
    result = _git(repo, "status", "--porcelain", "--", *TRACKED_PATHS)
    if result.returncode != 0:
        raise SyncError((result.stderr or result.stdout).strip() or "git status failed")
    paths = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        # A rename reads "R  old -> new"; what changed is where it landed.
        paths.append(path.split(" -> ")[-1].strip().strip('"'))
    return paths


def status(repo: Path) -> dict:
    """Where this checkout stands against its remote, for a page that has
    to say what a push would do before anyone presses it.

    Every field is best-effort: a checkout with no remote, or one that
    cannot reach it, still has to render a page rather than a stack
    trace, so the parts that need the network degrade to None and say
    why."""
    repo = Path(repo)
    info: dict = {
        "branch": None, "remote": None, "pending": [], "ahead": 0, "behind": 0,
        "last_commit": None, "reachable": False, "error": None,
    }
    # Everything, not just the first few calls. A page has to render
    # whatever git does, and the ways it can fail here -- no git on PATH,
    # a checkout that is not one, a remote that never answers -- all have
    # to arrive as a sentence in `error` rather than as a 500 that the
    # page then reports as a type error on a field that isn't there.
    try:
        info["branch"] = _git_ok(repo, "rev-parse", "--abbrev-ref", "HEAD")
        info["pending"] = pending_changes(repo)
        subject = _git_ok(repo, "log", "-1", "--pretty=%h\x1f%s\x1f%cI")
        sha, message, when = subject.split("\x1f")
        info["last_commit"] = {"sha": sha, "subject": message, "when": when}

        remote = _git(repo, "remote", "get-url", "origin")
        if remote.returncode != 0:
            info["error"] = "This checkout has no 'origin' remote, so there is nowhere to push."
            return info
        info["remote"] = safe_remote_url(remote.stdout.strip())

        # Counted against the remote as it actually is, not against
        # whatever this checkout last heard: without the fetch, "0 behind"
        # would mean "nothing had arrived by the last time anyone looked".
        fetched = _git(repo, "fetch", "origin", info["branch"], network=True)
        if fetched.returncode != 0:
            info["error"] = f"Couldn't reach the remote: {(fetched.stderr or '').strip()}"
            return info
        info["reachable"] = True
        counts = _git(repo, "rev-list", "--left-right", "--count", f"origin/{info['branch']}...HEAD")
        if counts.returncode == 0 and counts.stdout.split():
            behind, ahead = counts.stdout.split()
            info["ahead"], info["behind"] = int(ahead), int(behind)
    except (SyncError, OSError, ValueError) as e:
        info["error"] = str(e)
    return info


def checkpoint_database() -> None:
    """Folds the write-ahead log into the database file, so what gets
    committed is the whole state. See checkpoint_db.py."""
    from . import db

    conn = db._connect()
    busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise SyncError(
            "The database is still being written to, so it can't be safely committed yet. "
            "Finish what you're doing and try again in a moment."
        )


def push(repo: Path, message: str) -> dict:
    """Commits what has changed under data/ and pushes it.

    Returns what happened, rather than raising, for the cases that are
    ordinary rather than wrong: nothing to commit, or a remote that has
    moved on."""
    repo = Path(repo)
    state = status(repo)
    if state["error"]:
        raise SyncError(state["error"])
    if state["behind"]:
        raise SyncError(
            f"The remote has {state['behind']} commit(s) this server doesn't. Pushing would be "
            "rejected, and forcing it would overwrite them -- the review database is synced as "
            "one whole file, so there is no merge to fall back on. Pull them in first, on a "
            "machine where you can see what they are."
        )
    if not state["pending"] and not state["ahead"]:
        return {"pushed": False, "committed": False, "message": "Nothing to push -- already up to date.",
                "status": state}

    committed = False
    if state["pending"]:
        checkpoint_database()
        _git_ok(repo, "add", "--", *TRACKED_PATHS)
        # Staged rather than -a, so a push only ever carries data/ even
        # when something else in the tree has been edited.
        staged = _git(repo, "diff", "--cached", "--quiet", "--", *TRACKED_PATHS)
        if staged.returncode != 0:
            _git_ok(repo, "commit", "-m", message)
            committed = True

    pushed = _git(repo, "push", "origin", f"HEAD:{state['branch']}")
    if pushed.returncode != 0:
        raise SyncError((pushed.stderr or pushed.stdout).strip() or "git push failed")
    after = status(repo)
    count = len(state["pending"])
    return {
        "pushed": True, "committed": committed,
        "message": (f"Pushed {count} change(s) to {after['branch']}." if committed
                    else f"Pushed {state['ahead']} commit(s) already waiting."),
        "status": after,
    }
