"""Tests for deploy/githooks/pre-commit -- the guard that stops a
private key reaching a commit, and the one that keeps a review commit
whole.

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
import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = PROJECT_ROOT / "deploy" / "githooks"
HOOK = HOOKS_DIR / "pre-commit"

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
    """A checkout with the hook installed, as deploy/README.md says to:
    a tracked directory git is pointed at, not a copy inside .git/."""
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "code.py").write_text("print('hi')\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "First")

    hooks = path / "deploy" / "githooks"
    hooks.mkdir(parents=True)
    shutil.copy(HOOK, hooks / "pre-commit")
    (hooks / "pre-commit").chmod(0o755)
    # Absolute, so the test does not depend on which directory git
    # resolves a relative hooksPath against.
    _git(path, "config", "core.hooksPath", str(hooks))
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
    shutil.copy(HOOK, repo / "a-copy-of-the-hook")
    _git(repo, "add", "a-copy-of-the-hook")

    result = _commit(repo, "Add the hook")

    assert result.returncode == 0, result.stderr


def test_the_tests_own_fixture_does_not_trip_it(repo):
    """Same trap one step along: this file carries a banner too."""
    shutil.copy(Path(__file__), repo / "test_precommit_hook.py")
    _git(repo, "add", "test_precommit_hook.py")

    assert _commit(repo, "Add the tests").returncode == 0


# ---------------------------------------------------------------------------
# Keeping a review commit whole -- the half that broke
# ---------------------------------------------------------------------------
# The private-key half of the hook is pure shell and was never at risk.
# The other half runs two of this project's own modules, and when the
# restructure renamed both, every copy of the hook already installed kept
# calling the old names. Commits were refused, the dashboard's Push button
# failed with a message about a missing .py file, and Pull stuck behind it.
# Nothing here noticed, because nothing here ran that branch.
#
# Run against a PATH shim rather than the real modules: what is being
# tested is what the hook asks for, and a throwaway repo has no corpus/
# package to answer with. The names are then checked against the real
# package separately, which is the part that actually went stale.

def _python_shim(directory: Path, record: Path, exit_code: int = 0) -> None:
    """A `python3` on PATH that writes down how it was called."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{record}"\n'
        f"exit {exit_code}\n"
    )
    shim.chmod(0o755)


def _commit_with_shim(repo, tmp_path, record, exit_code=0):
    """Commit with the shim first on PATH, so the hook finds it as
    `python3` unless it has gone looking for the venv's instead."""
    _python_shim(tmp_path / "bin", record, exit_code)
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["git", "commit", "-m", "Review progress"],
                          cwd=str(repo), capture_output=True, text=True, env=env)


def test_a_review_commit_runs_the_checkpoint_and_the_export(repo, tmp_path):
    """The branch the restructure broke. Staging review files is what
    makes the hook run them, and it has to ask for both: a checkpoint, so
    a write still in the -wal file is in what gets committed, and an
    export, so data/review/ is not whatever it held last time."""
    (repo / "data" / "review").mkdir(parents=True)
    (repo / "data" / "review" / "crimes-act.jsonl").write_text('{"node_id": "s1"}\n')
    _git(repo, "add", "data/review/crimes-act.jsonl")
    record = tmp_path / "called"

    result = _commit_with_shim(repo, tmp_path, record)

    assert result.returncode == 0, result.stderr
    called = record.read_text()
    assert "-m corpus.storage.checkpoint_db" in called
    assert "-m corpus.review.review_sync export" in called


def test_an_ordinary_commit_never_sweeps_up_a_days_reviewing(repo, tmp_path):
    """Only when review files are already part of the commit. Otherwise a
    code commit would export the database over data/review/ and carry a
    day's work nobody meant to commit."""
    (repo / "code.py").write_text("print('changed')\n")
    _git(repo, "add", "code.py")
    record = tmp_path / "called"

    assert _commit_with_shim(repo, tmp_path, record).returncode == 0
    assert not record.exists(), "python was run for a commit with no review files in it"


def test_a_failing_checkpoint_stops_the_commit(repo, tmp_path):
    """Half a review commit is worse than none: the point of the hook is
    that what lands is whole."""
    (repo / "data" / "review").mkdir(parents=True)
    (repo / "data" / "review" / "crimes-act.jsonl").write_text('{"node_id": "s1"}\n')
    _git(repo, "add", "data/review/crimes-act.jsonl")

    result = _commit_with_shim(repo, tmp_path, tmp_path / "called", exit_code=1)

    assert result.returncode != 0
    assert _git(repo, "log", "--oneline").stdout.count("\n") == 1, "nothing was committed"


def test_the_modules_it_names_are_modules_this_project_has():
    """The test that would have caught it. The hook names two modules;
    if either is renamed and the hook is not, every review commit fails
    -- and on the server, where nobody runs pytest, it fails silently
    until somebody presses Push."""
    named = re.findall(r"-m (corpus[\w.]+)", HOOK.read_text())

    assert named, "the hook no longer runs anything -- has the review branch gone?"
    for module in named:
        assert importlib.util.find_spec(module), f"{module} is named by the hook but does not exist"


def test_it_prefers_the_projects_own_interpreter(repo, tmp_path):
    """On the server the service runs .venv/bin/python3, but systemd's
    PATH does not include the venv, so a bare `python3` here would be a
    different interpreter from the one everything else uses."""
    (repo / "data" / "review").mkdir(parents=True)
    (repo / "data" / "review" / "crimes-act.jsonl").write_text('{"node_id": "s1"}\n')
    _git(repo, "add", "data/review/crimes-act.jsonl")
    on_path, in_venv = tmp_path / "path-called", tmp_path / "venv-called"
    _python_shim(repo / ".venv" / "bin", in_venv)

    result = _commit_with_shim(repo, tmp_path, on_path)

    assert result.returncode == 0, result.stderr
    assert in_venv.exists(), "the venv's interpreter was not used"
    assert not on_path.exists(), "PATH's python3 was used instead of the venv's"


# ---------------------------------------------------------------------------
# This checkout's own installation
# ---------------------------------------------------------------------------

def test_this_checkout_runs_the_tracked_hook():
    """The hook is a tracked file git is pointed at, so a pull keeps it
    current -- but only once git has been pointed at it. A checkout still
    on the old copied hook is running something a pull cannot reach,
    which is the failure this whole arrangement replaced."""
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True).stdout.strip()
    stale = PROJECT_ROOT / ".git" / "hooks" / "pre-commit"

    assert configured == "deploy/githooks", (
        "this checkout is not using the tracked hook -- run:\n"
        "    git config core.hooksPath deploy/githooks")
    assert not stale.exists(), (
        f"{stale} is left over from the old copy-install and is now dead weight. "
        "Remove it: rm .git/hooks/pre-commit")
