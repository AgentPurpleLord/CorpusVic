"""Rolling one document back to an earlier commit (corpus/review/sync.py,
restore_document): a re-parse with "Start again" pushed over the CPA's
v114 and took its review work with it."""
import json
import subprocess

import pytest

from corpus.review import review_sync, sync


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, rows, parse, message, extra=True):
    review = repo / "data" / "review" / "act-v114"
    review.mkdir(parents=True, exist_ok=True)
    (review / "verified.jsonl").write_text("".join(json.dumps({"node_id": f"s{i}"}) + "\n" for i in range(rows)))
    links = review / "links.jsonl"
    if extra:
        links.write_text('{"id": "l1"}\n')
    elif links.exists():
        links.unlink()
    (repo / "data" / "parsed").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "parsed" / "act-v114.json").write_text(json.dumps({"nodes": parse}))
    other = repo / "data" / "review" / "other" / "verified.jsonl"
    other.parent.mkdir(parents=True, exist_ok=True)
    if not other.exists():
        other.write_text('{"node_id": "x"}\n')
    _git(repo, "add", "-A")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    calls = []
    monkeypatch.setattr(review_sync, "export", lambda base: calls.append("export"))
    monkeypatch.setattr(review_sync, "import_", lambda base: calls.append("import") or {"total": 0})
    monkeypatch.setattr(sync, "checkpoint_database", lambda repo: None)
    return tmp_path, calls


def test_the_commits_say_which_one_wiped_the_review(repo):
    path, _calls = repo
    _commit(path, 4921, [1, 2], "Review progress, 2026-10-06")
    _commit(path, 8, [1, 2, 3], "Review progress, 2026-10-06", extra=False)

    history = sync.document_history(path, "act-v114")

    assert [c["approved"] for c in history] == [8, 4921]


def test_a_document_is_put_back_as_it_was_and_nothing_else_moves(repo):
    path, calls = repo
    good = _commit(path, 4921, [1, 2], "before")
    _commit(path, 8, [1, 2, 3], "the one that wiped it", extra=False)
    (path / "data" / "review" / "other" / "verified.jsonl").write_text('{"node_id": "x"}\n{"node_id": "y"}\n')

    sync.restore_document(path, "act-v114", good)

    review = path / "data" / "review" / "act-v114"
    assert len((review / "verified.jsonl").read_text().splitlines()) == 4921
    assert (review / "links.jsonl").exists(), "a file deleted since comes back"
    assert json.loads((path / "data" / "parsed" / "act-v114.json").read_text())["nodes"] == [1, 2]
    assert len((path / "data" / "review" / "other" / "verified.jsonl").read_text().splitlines()) == 2
    # Exported first, so no other document's work is lost; rebuilt after.
    assert calls == ["export", "import"]
    # Waiting for Push, not committed behind anyone's back.
    assert "data/review/act-v114/verified.jsonl" in _git(path, "status", "--porcelain")


def test_only_a_real_commit_is_restored(repo):
    path, _calls = repo
    _commit(path, 1, [1], "first")

    with pytest.raises(sync.SyncError):
        sync.restore_document(path, "act-v114", "not-a-sha")
    with pytest.raises(sync.SyncError):
        sync.restore_document(path, "act-v114", "0" * 40)


def test_the_parse_menu_offers_a_restore_for_each_version():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert "openRestore('${escapeHtml(v.slug)}')" in page and 'id="restore-modal"' in page
    assert "api/acts/${encodeURIComponent(slug)}/restore" in page
