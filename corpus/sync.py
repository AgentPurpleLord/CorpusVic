"""
Moving the review work between the server and GitHub from wherever it is
being done -- so that a reviewer on the server does not have to open a
terminal on it to put a day's decisions somewhere safe, or to take in
what was decided somewhere else.

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

Coming the other way, `pull` is fast-forward only and `discard` keeps a
copy, for the same reason: with the database synced as one whole file
there is no merge, so every operation here either moves cleanly or stops
and says which of its reasons applied. None of them silently picks a
side.
"""
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
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


def explain(message: str) -> str:
    """git's own words, plus what they mean here where that differs.

    Only for the ones whose obvious reading sends you the wrong way. The
    rest are left exactly as git put them."""
    if "dubious ownership" in message:
        return (
            message
            + "\n\nThis is git refusing to work in a repository owned by another user. The "
            "advice it prints -- adding a safe.directory exception -- lets the command "
            "through but leaves the files it writes owned by whoever ran it, which is how "
            "the service later finds a checkout it cannot write to. Run git as the owner "
            "instead (`sudo -u dashboard git ...`), and if the ownership is already mixed, "
            "put it back with `sudo chown -R dashboard:dashboard /opt/corpusvic`."
        )
    return message


def _git_ok(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise SyncError(explain((result.stderr or result.stdout).strip()) or f"git {args[0]} failed")
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
        raise SyncError(explain((result.stderr or result.stdout).strip()) or "git status failed")
    paths = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        # A rename reads "R  old -> new"; what changed is where it landed.
        paths.append(path.split(" -> ")[-1].strip().strip('"'))
    return paths


def clear_stale_sidecars(repo: Path) -> list[str]:
    """Removes a -wal and -shm left beside a database git has just
    replaced.

    Tidying, not the protection -- and worth being exact about which,
    because the two are easy to confuse and only one of them works.

    What protects a pull is the checkpoint at the top of it. Measured, on
    a database another process was holding open while its file was
    replaced: with no checkpoint the pulled data read back as the data
    that was there before, whether or not these files had been deleted;
    with the checkpoint it read back correctly, again either way. Deleting
    the files cannot help on its own, because the holder's copy of the log
    is mapped into its memory and gets written back when it closes.

    What this does catch is a log orphaned by a process that died -- a
    review child that was killed, a machine that lost power. Safe here
    only because the checkpoint ran first and emptied the log: deleting a
    write-ahead log that legitimately belongs to the current file would
    throw away committed transactions, which is why this is called
    nowhere else."""
    removed = []
    for name in ("legislation.db-wal", "legislation.db-shm"):
        path = Path(repo) / "data" / name
        try:
            if path.exists():
                path.unlink()
                removed.append(name)
        except OSError:
            # Another process has it open on a platform that will not
            # unlink it. Nothing to do but let the check below speak.
            pass
    return removed


def database_is_sound(repo: Path) -> "str | None":
    """None if the review database is intact, or what sqlite said.

    Run after anything replaces it. It costs milliseconds on a file this
    size, and it is the difference between a corrupt database noticed now
    and one noticed a week later by a reviewer whose work will not
    save."""
    import sqlite3

    path = Path(repo) / "data" / "legislation.db"
    if not path.exists():
        return None
    # Only a file that really is a sqlite database. The damage this
    # check exists to catch -- a stale write-ahead log replayed over a
    # newly pulled file -- is page-level and always leaves the header
    # intact, so anything without one was never a database and is a
    # different problem with a different cause. Refusing a pull over it
    # would be this function having an opinion about what the repository
    # contains, which is not its job.
    try:
        if path.read_bytes()[:16] != b"SQLite format 3\x00":
            return None
    except OSError as e:
        return str(e)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            answer = conn.execute("PRAGMA integrity_check").fetchone()
            return None if answer and answer[0] == "ok" else (answer[0] if answer else "no answer")
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return str(e)


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
            info["error"] = f"Couldn't reach the remote: {explain((fetched.stderr or '').strip())}"
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


def head(repo: Path) -> "str | None":
    """This checkout's current commit, or None if that can't be read.

    Used to notice that the code on disk has moved on from the code that
    is running (see dashboard.py), which is the failure a pull button
    would otherwise introduce: git succeeds, the process keeps serving
    what it loaded at startup, and nothing anywhere says so."""
    result = _git(Path(repo), "rev-parse", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def commit(repo: Path, message: str) -> dict:
    """Commits what has changed under data/ without touching the network.

    Worth having separately from push for the case push cannot help
    with: a remote that isn't answering. Committing still puts the work
    somewhere it survives a restart, and the push can follow whenever
    the network does."""
    repo = Path(repo)
    pending = pending_changes(repo)
    if not pending:
        return {"committed": False, "message": "Nothing to commit -- data/ matches the last commit."}
    checkpoint_database()
    _git_ok(repo, "add", "--", *TRACKED_PATHS)
    staged = _git(repo, "diff", "--cached", "--quiet", "--", *TRACKED_PATHS)
    if staged.returncode == 0:
        return {"committed": False, "message": "Nothing to commit -- data/ matches the last commit."}
    _git_ok(repo, "commit", "-m", message)
    return {"committed": True, "message": f"Committed {len(pending)} change(s), not yet pushed."}


def pull(repo: Path) -> dict:
    """Brings in commits from the remote, fast-forward only.

    Fast-forward only because the review database is one file synced
    whole: git cannot merge two versions of it, and the merge it would
    otherwise attempt ends in a conflict on a binary file that nobody
    can resolve by hand. So this either moves cleanly onto what the
    remote has, or refuses and says which of the three reasons it is."""
    from . import db

    repo = Path(repo)
    # Before reading what has changed, not after: a write still sitting
    # in the write-ahead log is a change git cannot see, and a pull that
    # believed the tree was clean would replace the database out from
    # under it.
    checkpoint_database()
    state = status(repo)
    if state["error"]:
        raise SyncError(state["error"])
    if state["pending"]:
        raise SyncError(
            f"There are {len(state['pending'])} uncommitted change(s) under data/ that a pull "
            "would overwrite. Push them first, or discard them if they aren't wanted."
        )
    if not state["behind"]:
        return {"pulled": False, "message": "Nothing to pull -- already up to date.", "status": state}
    if state["ahead"]:
        raise SyncError(
            f"This server has {state['ahead']} commit(s) the remote doesn't, and the remote has "
            f"{state['behind']} this server doesn't. The review database is synced as one whole "
            "file, so there is no merge that keeps both -- one of the two has to be chosen, on a "
            "machine where you can see what each contains."
        )

    was = head(repo)
    # sqlite is holding the database file open by descriptor and git
    # replaces rather than rewrites it, so a connection left open here
    # would go on reading the old file after the pull -- see
    # db.close_connections.
    db.close_connections()
    merged = _git(repo, "merge", "--ff-only", f"origin/{state['branch']}")
    if merged.returncode != 0:
        raise SyncError(explain((merged.stderr or merged.stdout).strip()) or "git merge failed")
    # git has just written a new database. Anything still beside it
    # describes the one that was there before -- see clear_stale_sidecars.
    clear_stale_sidecars(repo)
    broken = database_is_sound(repo)
    if broken:
        raise SyncError(
            "The pull completed, but the review database it brought will not open: "
            f"{broken}\n\nNothing is lost -- what was pulled is in git. Restore it with "
            "`git checkout -- data/legislation.db` after removing any "
            "data/legislation.db-wal and -shm beside it."
        )

    changed = []
    if was:
        listed = _git(repo, "diff", "--name-only", f"{was}..HEAD")
        if listed.returncode == 0:
            changed = [line for line in listed.stdout.splitlines() if line.strip()]
    return {
        "pulled": True,
        "message": f"Pulled {state['behind']} commit(s), {len(changed)} file(s) changed.",
        "code_changed": any(path.endswith(".py") for path in changed),
        "status": status(repo),
    }


# Where a discarded database is kept. Gitignored, and outside data/ so
# that a backup can never itself become something to commit.
BACKUP_DIR = "_backups"


def discard(repo: Path) -> dict:
    """Throws away uncommitted changes under data/ and goes back to the
    last commit.

    The one destructive thing in this module, so it is also the one that
    keeps a copy: the database is written to `_backups/` first, named
    for the moment it was taken. A discard is usually somebody choosing
    the remote's version over this server's, and "usually" is not a good
    enough reason for a day's review work to be unrecoverable.

    Only tracked files are restored. Anything untracked under data/ -- a
    PDF just uploaded, a parse not yet committed -- is left exactly
    where it is, because it has no committed version to go back to and
    deleting it would be a different and much larger promise."""
    from . import db

    repo = Path(repo)
    pending = pending_changes(repo)
    if not pending:
        return {"discarded": False, "message": "Nothing to discard -- data/ matches the last commit."}

    checkpoint_database()
    backup = None
    source = repo / "data" / "legislation.db"
    if source.exists():
        backup_dir = repo / BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"legislation-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
        shutil.copy2(source, backup)

    db.close_connections()
    restored = _git(repo, "checkout", "HEAD", "--", *TRACKED_PATHS)
    if restored.returncode != 0:
        raise SyncError(explain((restored.stderr or restored.stdout).strip()) or "git checkout failed")
    clear_stale_sidecars(repo)
    broken = database_is_sound(repo)
    if broken:
        raise SyncError(f"The database was restored but will not open: {broken}")

    kept = f" The previous database is in {BACKUP_DIR}/{backup.name}." if backup else ""
    return {
        "discarded": True,
        "message": f"Discarded {len(pending)} change(s).{kept}",
        "backup": backup.name if backup else None,
        "status": status(repo),
    }
