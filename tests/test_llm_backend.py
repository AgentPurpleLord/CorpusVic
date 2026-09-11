"""Tests for ai_pipeline/llm_backend.py's status checks and error
messages -- no real network calls or a running Ollama server involved;
shutil.which and urllib.request.urlopen are monkeypatched to simulate
each precondition on its own. See tests/test_ai_assist.py for the
triage logic that sits on top of this backend."""
import io
import json
import urllib.error

import pytest

from ai_pipeline.llm_backend import OllamaBackend, OllamaUnavailable, pull_model


def _fake_urlopen_tags(models: list[str]):
    def urlopen(url_or_req, timeout=None, **kwargs):
        body = json.dumps({"models": [{"name": m} for m in models]}).encode("utf-8")
        return io.BytesIO(body)
    return urlopen


def test_status_when_ollama_not_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    backend = OllamaBackend()
    assert backend.status() == {
        "installed": False, "running": False, "model_present": False,
        "model": backend.model, "host": backend.host,
    }


def test_status_when_installed_but_not_running(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")

    def urlopen(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    backend = OllamaBackend()
    status = backend.status()
    assert status["installed"] is True
    assert status["running"] is False
    assert status["model_present"] is False


def test_status_when_running_but_model_not_pulled(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_tags(["llama3.1:8b-instruct"]))

    backend = OllamaBackend(model="qwen2.5:7b-instruct")
    status = backend.status()
    assert status["running"] is True
    assert status["model_present"] is False


def test_status_when_model_is_pulled(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_tags(["qwen2.5:7b-instruct"]))

    backend = OllamaBackend(model="qwen2.5:7b-instruct")
    assert backend.status()["model_present"] is True


def test_has_model_matches_a_bare_name_against_any_tag(monkeypatch):
    """A caller that asked for just "qwen2.5" (no tag) is asking for
    whatever tag of it is present, not a specific one."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_tags(["qwen2.5:14b-instruct"]))

    backend = OllamaBackend(model="qwen2.5")
    assert backend.has_model() is True


def test_ensure_ready_names_the_install_command_when_not_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(OllamaUnavailable, match="ollama.com"):
        OllamaBackend().ensure_ready()


def test_ensure_ready_names_ollama_serve_when_not_running(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("x")))
    with pytest.raises(OllamaUnavailable, match="ollama serve"):
        OllamaBackend().ensure_ready()


def test_ensure_ready_names_install_ai_model_when_model_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_tags([]))
    with pytest.raises(OllamaUnavailable, match="install_ai_model.py"):
        OllamaBackend().ensure_ready()


def test_ensure_ready_passes_silently_once_everything_is_ready(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen_tags(["qwen2.5:7b-instruct"]))
    OllamaBackend(model="qwen2.5:7b-instruct").ensure_ready()  # no raise


def test_ask_raises_before_making_a_request_when_not_ready(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(OllamaUnavailable):
        OllamaBackend().ask("system", "user", {"type": "object"})


def test_ask_returns_the_parsed_json_content(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")
    calls = []

    def urlopen(req, timeout=None, **kwargs):
        calls.append(req)
        url = req.full_url if hasattr(req, "full_url") else req
        if url.endswith("/api/tags"):
            return io.BytesIO(json.dumps({"models": [{"name": "qwen2.5:7b-instruct"}]}).encode())
        reply = {"message": {"content": json.dumps({"answer": "a", "reasoning": "r", "confidence": "high"})}}
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    backend = OllamaBackend(model="qwen2.5:7b-instruct")
    result = backend.ask("system prompt", "user prompt", {"type": "object"})
    assert result == {"answer": "a", "reasoning": "r", "confidence": "high"}


def test_pull_model_reports_ollama_not_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    ok, log = pull_model("qwen2.5:7b-instruct")
    assert ok is False
    assert "ollama.com" in log


def test_pull_model_runs_the_ollama_cli(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ollama")

    captured = {}

    class FakeResult:
        returncode = 0
        stdout = "pulling manifest\nsuccess\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, log = pull_model("qwen2.5:7b-instruct")
    assert ok is True
    assert "success" in log
    assert captured["cmd"] == ["ollama", "pull", "qwen2.5:7b-instruct"]
