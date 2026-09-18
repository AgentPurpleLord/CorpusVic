"""Tests for deploy/pre-commit.hook.example -- the guard that stops a
private key reaching a commit.

Worth testing rather than trusting, for the same reason it exists: on the
server the `dashboard` account's home directory *is* the checkout, so the
GitHub deploy key sits at .ssh/id_ed25519 inside the working tree.
.gitignore covers it; this covers `git add -f`, a rewritten ignore rule,
and a key saved somewhere new. It has been needed once already -- GitHub's
push protection caught a deploy key in a commit, which is a backstop and
not a design.

Run against real repositories in tmp_path with the hook really installed,
because what is being tested is whether git refuses the commit.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "deploy" / "pre-commit.hook.example"

# A private key's opening line. The body is not a key and does not need
# to be: what the hook matches on is the PEM banner, which is the part
# every private key format shares.
#
# Assembled from pieces so that the banner never appears as a literal in
# this file. Otherwise the hook refuses the commit that adds its own
# tests, and a scanner reading the repository has to decide whether this
# fixture is a real key -- both of which are somebody else's afternoon.
_BANNER = "-----%s OPENSSH PRIVATE KEY-----"
KEY_TEXT = (
    (_BANNER % "BEGIN") + "\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtz\n"
    + (_BANNER % "END") + "\n"
)


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=check)


@pytest.fixture
def repo(tmp_path):
    """A checkout with the hook installed, as deploy/README.md says to."""
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "code.py").write_text("print('hi')\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "First")

    installed = path / ".git" / "hooks" / "pre-commit"
    shutil.copy(HOOK, installed)
    installed.chmod(0o755)
    return path


def _commit(repo, message="A commit"):
    return _git(repo, "commit", "-m", message, check=False)


def test_a_private_key_is_refused_wherever_it_is(repo):
    """By content, not by filename -- the filename is the part that
    varies. This one is called something innocuous on purpose."""
    (repo / "notes.txt").write_text(KEY_TEXT)
    _git(repo, "add", "-f", "notes.txt")

    result = _commit(repo)

    assert result.returncode != 0, "the commit should have been refused"
    assert "private key" in result.stderr
    assert "notes.txt" in result.stderr
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 1, "nothing was committed"


def test_anything_under_ssh_is_refused_even_without_a_key_in_it(repo):
    """known_hosts and a .pub are not secret, but nothing in that
    directory belongs to the repository, and a rule that only catches the
    private half invites the commit that carries everything else."""
    (repo / ".ssh").mkdir()
    (repo / ".ssh" / "known_hosts").write_text("github.com ssh-ed25519 AAAA...\n")
    _git(repo, "add", "-f", ".ssh/known_hosts")

    result = _commit(repo)

    assert result.returncode != 0
    assert ".ssh/known_hosts" in result.stderr


def test_the_deploy_key_as_it_actually_sits_on_the_server(repo):
    """The real case, with the real path: .ssh/id_ed25519 inside the
    checkout, staged past .gitignore by a `git add -f` -- or by an
    ignore rule that was not there yet, which is how it happened."""
    (repo / ".ssh").mkdir()
    (repo / ".ssh" / "id_ed25519").write_text(KEY_TEXT)
    _git(repo, "add", "-f", ".ssh/id_ed25519")

    assert _commit(repo).returncode != 0


def test_an_ordinary_commit_is_not_slowed_down_or_refused(repo):
    """The guard has to be invisible when it is not needed. A hook that
    cries wolf gets installed once and then removed."""
    (repo / "code.py").write_text("print('changed')\n")
    (repo / "notes.md").write_text("A line about private keys, mentioning\n"
                                   "ssh and id_ed25519 in passing.\n")
    _git(repo, "add", ".")

    result = _commit(repo, "An ordinary change")

    assert result.returncode == 0, result.stderr
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 2


def test_a_deleted_file_is_not_inspected(repo):
    """Only what is being added or changed. Reading the index copy of a
    path that is being removed asks git for a blob that is not there."""
    (repo / "code.py").unlink()
    _git(repo, "add", "-A")

    assert _commit(repo, "Remove it").returncode == 0


def test_a_binary_file_is_never_matched_on_a_coincidence(repo):
    """grep -I, so a PDF that happens to contain the banner's bytes is
    not read as a key. The corpus is made of PDFs."""
    (repo / "scan.pdf").write_bytes(
        b"%PDF-1.4\n\x00\x01\x02" + (_BANNER % "BEGIN").encode() + b"\x00\xff\n")
    _git(repo, "add", ".")

    assert _commit(repo, "Add a PDF").returncode == 0


def test_the_hook_does_not_refuse_its_own_source(repo):
    """It did, once. The pattern is a PEM banner, so the first version of
    it matched the line that defines it and refused the commit that
    introduced the guard. A guard that blocks ordinary work gets removed,
    so this is the property that keeps it installed."""
    shutil.copy(HOOK, repo / "pre-commit.hook.example")
    _git(repo, "add", "pre-commit.hook.example")

    result = _commit(repo, "Add the hook template")

    assert result.returncode == 0, result.stderr


def test_the_tests_own_fixture_does_not_trip_it(repo):
    """Same trap one step along: this file carries a banner too."""
    shutil.copy(Path(__file__), repo / "test_precommit_hook.py")
    _git(repo, "add", "test_precommit_hook.py")

    assert _commit(repo, "Add the tests").returncode == 0


def test_the_installed_hook_matches_the_tracked_template(repo):
    """Git hooks do not travel with a clone, so the tracked template is
    the only copy anybody can get. If this repository's own installed
    hook has drifted from it, the template is not what is being run and
    these tests are measuring the wrong file."""
    installed = Path(__file__).resolve().parent.parent / ".git" / "hooks" / "pre-commit"
    if not installed.exists():
        pytest.skip("no pre-commit hook installed in this checkout")
    assert installed.read_text() == HOOK.read_text(), (
        "deploy/pre-commit.hook.example and .git/hooks/pre-commit differ -- "
        "re-install it: cp deploy/pre-commit.hook.example .git/hooks/pre-commit")
