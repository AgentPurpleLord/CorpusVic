"""Background jobs: listed, stoppable between steps, and run at low
priority so the dashboard keeps answering (corpus/web/dashboard.py)."""

import pytest
from fastapi.testclient import TestClient

from corpus import PROJECT_ROOT
from corpus.web import dashboard


@pytest.fixture
def tasks(monkeypatch):
    monkeypatch.setattr(dashboard, "_tasks", {})
    return TestClient(dashboard.app)


def test_fetching_every_version_stops_between_versions_and_slims_once(tasks, monkeypatch):
    fetched, slimmed = [], []
    task = dashboard._task("fetch-all", "Fetching every version of Bail Act 1977", "bail-act")

    def fetch(work, version, replace=False, then_slim=True):
        fetched.append((version, then_slim))
        if version == 2:
            task["cancel_requested"] = True   # Stop pressed while v2 parses
        return {"ok": True, "slug": f"bail-act-v{version}"}
    monkeypatch.setattr(dashboard, "_fetch_version", fetch)
    monkeypatch.setattr(dashboard, "_slim_work", lambda work, job=None: slimmed.append(work))
    job = {"version": 1, "all": [1, 2, 3, 4], "done": [], "failed": [], "state": "running"}

    dashboard._fetch_all_job("bail-act", job, task)

    # v2 finished -- never cut off mid-parse -- and nothing after it began.
    assert fetched == [(1, False), (2, False)]
    assert job["state"] == "cancelled" and task["state"] == "cancelled"
    assert slimmed == []


def test_a_whole_run_slims_once_at_the_end(tasks, monkeypatch):
    slimmed = []
    monkeypatch.setattr(dashboard, "_fetch_version", lambda work, v, replace=False, then_slim=True: {"ok": True, "slug": f"a-v{v}"})
    monkeypatch.setattr(dashboard, "_slim_work", lambda work, job=None: slimmed.append(work))
    job = {"version": 1, "all": [1, 2, 3], "done": [], "failed": [], "state": "running"}

    dashboard._fetch_all_job("a", job, dashboard._task("fetch-all", "a", "a"))

    assert slimmed == ["a"] and job["state"] == "done"


def test_only_a_running_stoppable_task_can_be_stopped(tasks):
    stoppable = dashboard._task("fetch-all", "Fetching", "a")
    single = dashboard._task("fetch", "Fetching v3", "a", cancellable=False)

    assert tasks.post(f"/api/tasks/{single['id']}/cancel").status_code == 409
    assert tasks.post(f"/api/tasks/{stoppable['id']}/cancel").json()["cancel_requested"] is True
    dashboard._task_finished(stoppable, "done")
    assert tasks.post(f"/api/tasks/{stoppable['id']}/cancel").status_code == 409
    listed = tasks.get("/api/tasks").json()
    assert [t["state"] for t in listed] == ["running", "cancelled"]


def test_a_background_parse_runs_at_low_priority(monkeypatch):
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(cmd, **kwargs):
        seen.setdefault("calls", []).append((cmd, kwargs.get("preexec_fn")))
        return Done()
    monkeypatch.setattr(dashboard.subprocess, "run", run)
    monkeypatch.setattr(dashboard.shutil, "which", lambda name: "/usr/bin/ionice" if name == "ionice" else None)

    dashboard._run_parse_subprocess(["python", "-m", "corpus.parsing.run_pipeline", "x.pdf"])
    thread = dashboard._in_background(dashboard._run_parse_subprocess, ["python", "-m", "corpus.parsing.run_pipeline", "x.pdf"])
    thread.join()

    (fore_cmd, fore_pre), (back_cmd, back_pre) = seen["calls"]
    assert fore_cmd[0] == "python" and fore_pre is None
    assert back_cmd[:3] == ["/usr/bin/ionice", "-c", "3"] and back_pre is dashboard._low_priority


def test_the_dashboard_lists_running_tasks():
    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert 'id="tasks-btn"' in page and 'id="tasks-modal"' in page
    assert "api/tasks/${encodeURIComponent(id)}/cancel" in page
