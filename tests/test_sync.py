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
