"""The dashboard's Updates panel: GitHub's pull requests merged from it,
and the server updated (corpus/web/github_updates.py, dashboard)."""
import io
import json
import urllib.error

import pytest

from corpus import PROJECT_ROOT
from corpus.review import sync
from corpus.web import dashboard, github_updates


def test_the_repository_is_read_off_either_form_of_remote():
    assert github_updates.repo_of("https://github.com/AgentPurpleLord/CorpusVic.git") == "AgentPurpleLord/CorpusVic"
    assert github_updates.repo_of("git@github.com:AgentPurpleLord/CorpusVic.git") == "AgentPurpleLord/CorpusVic"
    assert github_updates.repo_of("https://x-token:abc@github.com/o/n") == "o/n"
    assert github_updates.repo_of("/srv/git/corpus") is None


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _github(routes, seen):
    def opener(req, timeout=None):
        seen.append(req)
        key = (req.get_method(), req.full_url.split("api.github.com", 1)[1].split("?", 1)[0])
        answer = routes[key]
        if isinstance(answer, Exception):
            raise answer
        return _Response(json.dumps(answer).encode())
    return opener


def test_open_pull_requests_come_with_whether_they_can_go_in(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    seen = []
    routes = {
        ("GET", "/repos/o/n/pulls"): [{"number": 7, "title": "Bail Act schedules", "head": {"ref": "claude/x", "sha": "abc"},
                                       "user": {"login": "me"}, "updated_at": "2026-10-06T01:00:00Z",
                                       "html_url": "https://github.com/o/n/pull/7"}],
        ("GET", "/repos/o/n/pulls/7"): {"mergeable": True, "mergeable_state": "clean"},
        ("GET", "/repos/o/n/commits/abc/check-runs"): {"check_runs": [{"conclusion": "success"}, {"conclusion": None}]},
        ("GET", "/repos/o/n/commits/abc/status"): {"statuses": []},
    }

    [pull] = github_updates.open_pulls("o/n", "main", opener=_github(routes, seen))

    assert (pull["number"], pull["sha"], pull["mergeable"], pull["checks"]) == (7, "abc", True, "pending")
    assert all(r.get_header("Authorization") == "Bearer secret-token" for r in seen)
    assert "secret-token" not in json.dumps(pull)


def test_a_merge_names_the_head_it_was_shown_and_passes_on_a_refusal(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    seen = []
    refused = urllib.error.HTTPError("u", 405, "no", {}, io.BytesIO(b'{"message": "Pull Request is not mergeable"}'))

    assert github_updates.merge("o/n", 7, "abc", opener=_github({("PUT", "/repos/o/n/pulls/7/merge"): {"merged": True}}, seen))["merged"]
    assert json.loads(seen[0].data) == {"merge_method": "merge", "sha": "abc"}
    with pytest.raises(github_updates.UpdateError, match="not mergeable"):
        github_updates.merge("o/n", 7, "abc", opener=_github({("PUT", "/repos/o/n/pulls/7/merge"): refused}, []))


def test_without_a_token_the_server_half_still_answers(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sync, "status", lambda repo: {"remote": "https://github.com/o/n.git", "branch": "main", "behind": 3, "error": None})
    monkeypatch.setattr(sync, "head", lambda repo: "abc1234")

    state = dashboard.updates()

    assert state["configured"] is False and state["behind"] == 3 and state["pulls"] == []


def _run_update(monkeypatch, pulled):
    calls = []
    monkeypatch.setattr(dashboard, "_tasks", {})
    monkeypatch.setattr(sync, "commit", lambda repo, message: calls.append("save") or {"committed": True, "message": "Committed 3 change(s)."})
    monkeypatch.setattr(sync, "pull", lambda repo: calls.append("pull") or pulled)
    monkeypatch.setattr(sync, "push", lambda repo, message: calls.append("send") or {"pushed": True, "message": "Pushed."})

    class Done:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(dashboard.subprocess, "run", lambda cmd, **kw: calls.append("install") or Done())
    monkeypatch.setattr(dashboard, "public_service_restart", lambda: calls.append("public") or {"message": "ok"})
    monkeypatch.setattr(dashboard, "_restart_capability", lambda: (True, ""))
    monkeypatch.setattr(dashboard.os, "_exit", lambda code: calls.append("exit"))
    monkeypatch.setattr(dashboard.time, "sleep", lambda s: None)
    job = {"state": "running", "notes": [], "steps": [{"key": k, "label": label, "state": "pending", "message": ""}
                                                      for k, label in dashboard._UPDATE_STEPS]}
    dashboard._apply_update(job, dashboard._task("update", "Updating", cancellable=False))
    return calls, job


def test_an_update_pulls_installs_only_what_changed_and_restarts_both(monkeypatch):
    calls, job = _run_update(monkeypatch, {"message": "Pulled 2 commits.", "changed": ["corpus/x.py"]})
    # Unpushed review work is saved, merged with what arrives, and sent --
    # it used to stop the update, and a terminal pull loaded none of it.
    assert calls == ["save", "pull", "send", "public", "exit"]
    assert [s["state"] for s in job["steps"]] == ["done", "done", "done", "skipped", "done", "running"]

    calls, job = _run_update(monkeypatch, {"message": "Pulled.", "changed": ["requirements.txt", "deploy/public.service"]})
    assert calls == ["save", "pull", "send", "install", "public", "exit"]
    assert any("daemon-reload" in n for n in job["notes"])


def test_an_update_stops_at_a_refused_pull(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "_tasks", {})

    def refuse(repo):
        raise sync.SyncError("There are 2 uncommitted change(s) under data/ that a pull would overwrite.")
    monkeypatch.setattr(sync, "commit", lambda repo, message: {"committed": False, "message": "Nothing to commit."})
    monkeypatch.setattr(sync, "pull", refuse)
    monkeypatch.setattr(dashboard, "public_service_restart", lambda: calls.append("public"))
    monkeypatch.setattr(dashboard.os, "_exit", lambda code: calls.append("exit"))
    job = {"state": "running", "notes": [], "steps": [{"key": k, "label": label, "state": "pending", "message": ""}
                                                      for k, label in dashboard._UPDATE_STEPS]}

    dashboard._apply_update(job, dashboard._task("update", "Updating", cancellable=False))

    assert calls == [] and job["state"] == "failed" and "uncommitted" in job["steps"][1]["message"]


def test_the_dashboard_has_the_updates_panel():
    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="updates-btn"' in page and 'id="updates-modal"' in page
    assert "GITHUB_TOKEN=" in page and "async function pollUpdate()" in page


def test_neither_service_weights_the_machine():
    """CPUWeight 500 on the public site carried its page renderer with it:
    re-rendering every page after a deploy, it starved the dashboard to
    502s and a crawl. Equal shares, background work niced on both sides."""
    for unit in ("dashboard.service", "public.service"):
        text = (PROJECT_ROOT / "deploy" / unit).read_text(encoding="utf-8")
        assert not any(line.startswith(("CPUWeight=", "IOWeight=")) for line in text.splitlines()), unit


def test_the_dashboard_does_not_ask_github_on_every_load():
    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert "\nloadUpdates(false);" not in page
    sync = page[page.index("function renderSync(s)"):]
    assert 'getElementById("updates-dot").hidden' in sync[:600]


def test_an_import_that_cannot_record_itself_says_so(tmp_path, monkeypatch):
    """After a `git pull` run as root, the record of the last import was
    root's, an import could not overwrite it, and said nothing -- so the
    "files have changed" refusal stayed after the fix that should clear it."""
    from corpus.review import review_sync

    (tmp_path / "data" / "review" / "act").mkdir(parents=True)
    (tmp_path / "data" / "review" / "act" / "verified.jsonl").write_text("")
    monkeypatch.setattr(review_sync.Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("denied")))

    said = review_sync._record_state(tmp_path)

    assert "denied" in said and "chown -R dashboard:dashboard" in said
