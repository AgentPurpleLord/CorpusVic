"""Tests for corpus/sync.py -- committing and pushing the review work
from the admin tool instead of a terminal on the server.

Against real repositories in tmp_path rather than a mocked git: what
this module is for is the handful of ways git says no, and a mock would
only ever say what it was told to.
"""
import subprocess

import pytest

from corpus import sync


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


@pytest.fixture
def remote(tmp_path):
    """A bare repository standing in for GitHub."""
    path = tmp_path / "remote.git"
    path.mkdir()
    _git(path, "init", "--bare", "--initial-branch=main")
    return path


@pytest.fixture
def repo(tmp_path, remote):
    """A checkout of it with one commit and some review data."""
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "data").mkdir()
    (path / "data" / "legislation.db").write_bytes(b"first")
    (path / "code.py").write_text("print('hi')\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "First")
    _git(path, "remote", "add", "origin", str(remote))
    _git(path, "push", "-u", "origin", "main")
    return path


@pytest.fixture(autouse=True)
def _no_real_checkpoint(monkeypatch):
    """The real one opens the project's own database, which these tests
    have nothing to do with."""
    monkeypatch.setattr(sync, "checkpoint_database", lambda: None)


def test_a_clean_checkout_has_nothing_to_push(repo):
    state = sync.status(repo)

    assert state["error"] is None
    assert state["branch"] == "main"
    assert (state["pending"], state["ahead"], state["behind"]) == ([], 0, 0)
    assert state["reachable"] is True


def test_a_changed_database_shows_up_as_pending(repo):
    (repo / "data" / "legislation.db").write_bytes(b"second")

    assert sync.status(repo)["pending"] == ["data/legislation.db"]


def test_pushing_nothing_says_so_rather_than_failing(repo):
    result = sync.push(repo, "nothing here")

    assert result["pushed"] is False
    assert "up to date" in result["message"]


def test_pushing_commits_the_database_and_sends_it(repo, remote):
    (repo / "data" / "legislation.db").write_bytes(b"second")

    result = sync.push(repo, "Review progress")

    assert (result["pushed"], result["committed"]) == (True, True)
    assert sync.status(repo)["pending"] == []
    landed = subprocess.run(["git", "show", "main:data/legislation.db"], cwd=str(remote),
                            capture_output=True).stdout
    assert landed == b"second"


def test_a_push_carries_only_the_review_data(repo, remote):
    """The button's whole job is the review work. An edit someone left in
    the working tree on the server is not review work, and publishing it
    because it happened to be there is how a half-finished change reaches
    everyone else."""
    (repo / "data" / "legislation.db").write_bytes(b"second")
    (repo / "code.py").write_text("print('half-finished')\n")

    sync.push(repo, "Review progress")

    landed = subprocess.run(["git", "show", "main:code.py"], cwd=str(remote),
                            capture_output=True, text=True).stdout
    assert landed == "print('hi')\n", "the working-tree edit stayed behind"
    assert "code.py" in subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                                       capture_output=True, text=True).stdout


def test_the_checkpoint_runs_before_the_commit(repo, monkeypatch):
    """A database committed with writes still in its write-ahead log is a
    snapshot missing whatever had not been folded in, and nothing says
    so. The repository ships a pre-commit hook that checkpoints, but
    hooks do not travel with a clone."""
    order = []
    monkeypatch.setattr(sync, "checkpoint_database", lambda: order.append("checkpoint"))
    real_git_ok = sync._git_ok
    monkeypatch.setattr(sync, "_git_ok",
                        lambda r, *a: (order.append(a[0]), real_git_ok(r, *a))[1])
    (repo / "data" / "legislation.db").write_bytes(b"second")

    sync.push(repo, "Review progress")

    assert order.index("checkpoint") < order.index("commit")


def test_a_remote_that_has_moved_on_is_refused_not_forced(repo, remote, tmp_path):
    """The one that matters. The review database is synced as one whole
    file -- there is no merge -- so forcing here would silently discard
    whichever side lost, which is the accident this exists to prevent."""
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "clone", str(remote), ".")
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "data" / "legislation.db").write_bytes(b"from the laptop")
    _git(other, "commit", "-am", "Reviewed on the laptop")
    _git(other, "push")

    (repo / "data" / "legislation.db").write_bytes(b"from the server")

    with pytest.raises(sync.SyncError, match="doesn't"):
        sync.push(repo, "Review progress")

    landed = subprocess.run(["git", "show", "main:data/legislation.db"], cwd=str(remote),
                            capture_output=True).stdout
    assert landed == b"from the laptop", "the other side's work is untouched"


def test_being_behind_is_reported_before_anyone_presses_anything(repo, remote, tmp_path):
    other = tmp_path / "other2"
    other.mkdir()
    _git(other, "clone", str(remote), ".")
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")
    (other / "data" / "legislation.db").write_bytes(b"elsewhere")
    _git(other, "commit", "-am", "Elsewhere")
    _git(other, "push")

    assert sync.status(repo)["behind"] == 1


def test_a_checkout_with_no_remote_says_so(tmp_path):
    path = tmp_path / "lonely"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "data").mkdir()
    (path / "data" / "legislation.db").write_bytes(b"x")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "First")

    state = sync.status(path)

    assert "nowhere to push" in state["error"]
    assert state["reachable"] is False


def test_an_unreachable_remote_is_reported_rather_than_raised(repo):
    _git(repo, "remote", "set-url", "origin", "/nonexistent/path/to.git")

    state = sync.status(repo)

    assert state["reachable"] is False
    assert "Couldn't reach the remote" in state["error"]


@pytest.mark.parametrize("url, expected", [
    ("https://x-access-token:ghp_secret@github.com/o/r", "https://github.com/o/r"),
    ("https://user:pw@example.com/r.git", "https://example.com/r.git"),
    ("git@github.com:AgentPurpleLord/CorpusVic.git", "git@github.com:AgentPurpleLord/CorpusVic.git"),
    ("https://github.com/AgentPurpleLord/CorpusVic", "https://github.com/AgentPurpleLord/CorpusVic"),
])
def test_a_token_in_the_remote_is_not_shown_on_the_page(url, expected):
    """The status is rendered into a web page, and a remote can carry a
    token."""
    assert sync.safe_remote_url(url) == expected


# ---------------------------------------------------------------------
# Failing in a way a page can render
# ---------------------------------------------------------------------
# The admin dashboard reported all of this as
# "TypeError: Cannot read properties of undefined (reading 'length')",
# because status() let some failures escape as a 500 and the page read
# .pending off {"detail": ...}. Every way this can go wrong has to come
# back as a sentence in `error`, with the rest of the shape intact.

def test_somewhere_that_is_not_a_repository_is_reported(tmp_path):
    state = sync.status(tmp_path)

    assert "not a git repository" in state["error"]
    assert state["pending"] == [] and state["reachable"] is False


def test_a_timeout_is_reported_rather_than_raised(repo, monkeypatch):
    """subprocess.TimeoutExpired is not an OSError, so it went straight
    past the guard and out of the endpoint as a 500. A git fetch over SSH
    to a host the server has never seen is exactly how that happened."""
    import subprocess as sp

    def times_out(*a, **k):
        raise sp.TimeoutExpired(cmd="git fetch", timeout=15)

    monkeypatch.setattr(sync.subprocess, "run", times_out)
    state = sync.status(repo)

    assert "gave up after" in state["error"]
    assert state["pending"] == []


def test_git_missing_entirely_is_reported(repo, monkeypatch):
    def not_installed(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(sync.subprocess, "run", not_installed)

    assert "git isn't installed" in sync.status(repo)["error"]


def test_a_network_call_waits_far_less_than_a_local_one(repo, monkeypatch):
    """This runs while somebody is looking at a page. A status line is
    worth a few seconds; the old single budget was three minutes, which
    is how a dashboard came to sit there saying nothing."""
    seen = {}
    real = sync.subprocess.run

    def record(cmd, **kwargs):
        seen[cmd[1]] = kwargs.get("timeout")
        return real(cmd, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", record)
    sync.status(repo)

    assert seen["fetch"] == sync._NETWORK_TIMEOUT_SECONDS
    assert seen["rev-parse"] == sync._TIMEOUT_SECONDS
    assert sync._NETWORK_TIMEOUT_SECONDS < sync._TIMEOUT_SECONDS


def test_git_is_never_left_waiting_for_a_person(repo, monkeypatch):
    """There is nobody at this end. An ssh asking whether to trust a new
    host would sit until the timeout and then report as slow, rather than
    as the thing it is."""
    seen = {}
    real = sync.subprocess.run

    def record(cmd, **kwargs):
        seen.update(kwargs.get("env") or {})
        return real(cmd, **kwargs)

    monkeypatch.setattr(sync.subprocess, "run", record)
    sync.status(repo)

    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in seen["GIT_SSH_COMMAND"]


def test_the_shape_survives_every_failure(tmp_path, repo, monkeypatch):
    """Whatever went wrong, the page gets the same fields -- which is
    what stops a failure being reported as a missing property."""
    import subprocess as sp

    expected = {"branch", "remote", "pending", "ahead", "behind",
                "last_commit", "reachable", "error"}
    assert set(sync.status(repo)) == expected
    assert set(sync.status(tmp_path)) == expected

    monkeypatch.setattr(sync.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("git", 15)))
    assert set(sync.status(repo)) == expected


def test_dubious_ownership_is_explained_rather_than_passed_on(repo, monkeypatch):
    """git's own advice here -- add a safe.directory exception -- gets
    the command through and leaves the files it writes owned by whoever
    ran it, which is how the service later finds a checkout it cannot
    write to. The message says so."""
    import subprocess as sp

    def dubious(cmd, **kwargs):
        return sp.CompletedProcess(cmd, 128, "", "fatal: detected dubious ownership in repository")

    monkeypatch.setattr(sync.subprocess, "run", dubious)
    state = sync.status(repo)

    assert "dubious ownership" in state["error"], "git's own words are kept"
    assert "owned by another user" in state["error"]
    assert "chown -R dashboard" in state["error"], "and how to put it right"


def test_an_ordinary_git_error_is_left_as_git_put_it():
    assert sync.explain("fatal: couldn't find remote ref main") == "fatal: couldn't find remote ref main"


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------


def _commit_elsewhere(tmp_path, remote, name="other", contents=b"second", path="data/legislation.db"):
    """Somebody else's checkout pushing a commit, so the repo under test
    has something real to be behind by."""
    other = tmp_path / name
    other.mkdir()
    _git(other, "clone", str(remote), ".")
    _git(other, "config", "user.email", "other@example.com")
    _git(other, "config", "user.name", "Other")
    target = other / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    _git(other, "add", ".")
    _git(other, "commit", "-m", f"From {name}")
    _git(other, "push", "origin", "main")
    return other


def test_pulling_brings_in_what_was_committed_elsewhere(repo, remote, tmp_path):
    _commit_elsewhere(tmp_path, remote)
    result = sync.pull(repo)
    assert result["pulled"] is True
    assert (repo / "data" / "legislation.db").read_bytes() == b"second"
    assert result["status"]["behind"] == 0


def test_pulling_nothing_says_so_rather_than_failing(repo):
    result = sync.pull(repo)
    assert result["pulled"] is False
    assert "up to date" in result["message"]


def test_a_pull_refuses_to_overwrite_uncommitted_review_work(repo, remote, tmp_path):
    _commit_elsewhere(tmp_path, remote)
    (repo / "data" / "legislation.db").write_bytes(b"a day's reviewing")
    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(repo)
    assert "uncommitted" in str(excinfo.value)
    # And left it exactly where it was, rather than half-applying.
    assert (repo / "data" / "legislation.db").read_bytes() == b"a day's reviewing"


def test_a_diverged_checkout_is_refused_rather_than_merged(repo, remote, tmp_path):
    _commit_elsewhere(tmp_path, remote)
    (repo / "data" / "legislation.db").write_bytes(b"local")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Local work")
    with pytest.raises(sync.SyncError) as excinfo:
        sync.pull(repo)
    message = str(excinfo.value)
    assert "no merge" in message
    assert (repo / "data" / "legislation.db").read_bytes() == b"local"


def test_a_pull_says_when_it_brought_new_code(repo, remote, tmp_path):
    _commit_elsewhere(tmp_path, remote, contents=b"print('new')\n", path="code.py")
    assert sync.pull(repo)["code_changed"] is True


def test_a_pull_of_data_alone_does_not_ask_for_a_restart(repo, remote, tmp_path):
    _commit_elsewhere(tmp_path, remote)
    assert sync.pull(repo)["code_changed"] is False


def test_a_pull_lets_go_of_the_database_before_replacing_it(repo, remote, tmp_path, monkeypatch):
    """sqlite holds the file it opened, and git replaces rather than
    rewrites it. A connection left open here would go on reading the old
    file after a successful pull -- which looks like a pull that did
    nothing, forever."""
    from corpus import db

    opened = {}
    monkeypatch.setattr(db, "_connections", opened)
    import sqlite3
    opened["held"] = sqlite3.connect(str(tmp_path / "held.db"))

    _commit_elsewhere(tmp_path, remote)
    sync.pull(repo)
    assert opened == {}


# ---------------------------------------------------------------------------
# Committing without pushing
# ---------------------------------------------------------------------------


def test_committing_works_with_the_remote_unreachable(repo):
    _git(repo, "remote", "set-url", "origin", "/nonexistent/repo.git")
    result = sync.commit(repo, "Review progress")
    assert result["committed"] is False  # nothing has changed yet

    (repo / "data" / "legislation.db").write_bytes(b"reviewed")
    result = sync.commit(repo, "Review progress")
    assert result["committed"] is True
    assert sync.pending_changes(repo) == []


def test_a_local_commit_carries_only_the_review_data(repo):
    (repo / "data" / "legislation.db").write_bytes(b"reviewed")
    (repo / "code.py").write_text("print('half-finished edit')\n")
    sync.commit(repo, "Review progress")
    listed = subprocess.run(["git", "show", "--name-only", "--pretty=", "HEAD"],
                            cwd=str(repo), capture_output=True, text=True, check=True)
    assert listed.stdout.split() == ["data/legislation.db"]


# ---------------------------------------------------------------------------
# Discarding
# ---------------------------------------------------------------------------


def test_discarding_goes_back_to_the_last_commit(repo):
    (repo / "data" / "legislation.db").write_bytes(b"unwanted")
    result = sync.discard(repo)
    assert result["discarded"] is True
    assert (repo / "data" / "legislation.db").read_bytes() == b"first"


def test_a_discard_keeps_what_it_threw_away(repo):
    (repo / "data" / "legislation.db").write_bytes(b"a day's reviewing")
    result = sync.discard(repo)
    backup = repo / sync.BACKUP_DIR / result["backup"]
    assert backup.read_bytes() == b"a day's reviewing"
    assert result["backup"] in result["message"]


def test_a_discard_leaves_untracked_files_where_they_are(repo):
    """A PDF just uploaded has no committed version to go back to.
    Deleting it would be a different promise from the one this makes."""
    (repo / "data" / "legislation.db").write_bytes(b"unwanted")
    uploaded = repo / "data" / "just-uploaded.pdf"
    uploaded.write_bytes(b"%PDF-1.4")
    sync.discard(repo)
    assert uploaded.exists()


def test_discarding_nothing_says_so_rather_than_failing(repo):
    result = sync.discard(repo)
    assert result["discarded"] is False
    assert not (repo / sync.BACKUP_DIR).exists()


# ---------------------------------------------------------------------------
# Which commit this is
# ---------------------------------------------------------------------------


def test_head_is_the_current_commit(repo):
    expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True, check=True).stdout.strip()
    assert sync.head(repo) == expected


def test_head_of_somewhere_that_is_not_a_repository_is_none(tmp_path):
    assert sync.head(tmp_path) is None
