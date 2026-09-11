"""
A local, open-source model backend for the assistive-triage feature (see
ai_pipeline/ai_assist.py) -- never a required part of parsing itself,
just an optional second opinion a reviewer can ask for on a piece
diagnostics has already flagged as uncertain.

Talks to a local Ollama server (https://ollama.com) over its REST API.
Ollama is the only backend on purpose: every model it serves is an
open-source model file running entirely on this machine, with nothing
sent anywhere else, and it comes with its own model registry and
version management rather than this project tracking GGUF file URLs
itself. See install_ai_model.py for setting one up.

The previous version of this feature (removed -- see run_pipeline.py's
own docstring) also had a cloud Claude backend and crashed with a raw
connection error when the local one wasn't reachable. Neither mistake
is repeated here: there is no cloud backend at all, and every call
checks readiness first and raises OllamaUnavailable with the exact next
command to run, not a stack trace from urllib.
"""
import json
import shutil
import subprocess
import urllib.error
import urllib.request

OLLAMA_HOST = "http://localhost:11434"

# A 7-8B instruction-tuned model is a reasonable default for the kind of
# short, narrow question this feature actually asks (see
# ai_pipeline/ai_assist.py) -- it runs on modest hardware (8GB of RAM is
# enough) without a GPU, and this task has never needed a larger model's
# extra reasoning depth. install_ai_model.py and OllamaBackend both take
# --model/model= to use a different one already pulled.
DEFAULT_MODEL = "qwen2.5:7b-instruct"


class OllamaUnavailable(RuntimeError):
    """Ollama itself, or the model this feature needs, isn't ready yet.
    Raised with a message naming the exact next command to run -- see
    install_ai_model.py, which checks for and fixes all three of these
    causes up front, so hitting this here should be rare rather than a
    surprise in the middle of a review session."""


class OllamaBackend:
    def __init__(self, model: str | None = None, host: str = OLLAMA_HOST):
        self.model = model or DEFAULT_MODEL
        self.host = host.rstrip("/")

    def is_installed(self) -> bool:
        return shutil.which("ollama") is not None

    def _get_json(self, path: str, timeout: float = 5.0) -> dict:
        with urllib.request.urlopen(f"{self.host}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def is_running(self) -> bool:
        try:
            self._get_json("/api/tags")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def has_model(self) -> bool:
        """Whether `self.model` has actually been pulled, checked against
        the server's own list rather than assumed from is_installed --
        Ollama can be running fine with a different (or no) model present."""
        try:
            tags = self._get_json("/api/tags")
        except (urllib.error.URLError, OSError, ValueError):
            return False
        names = {m.get("name") for m in tags.get("models", [])}
        # Ollama's own list includes the tag ("qwen2.5:7b-instruct"); a
        # caller that asked for just "qwen2.5" (no tag, meaning "whatever
        # tag is present") still counts as a match on the name alone.
        return self.model in names or any(n.split(":")[0] == self.model for n in names)

    def status(self) -> dict:
        """{"installed", "running", "model_present", "model", "host"} --
        the one call both install_ai_model.py and dashboard.py's /api/ai/
        status endpoint need, so the three checks are only ever written
        once."""
        installed = self.is_installed()
        running = installed and self.is_running()
        model_present = running and self.has_model()
        return {
            "installed": installed, "running": running, "model_present": model_present,
            "model": self.model, "host": self.host,
        }

    def ensure_ready(self) -> None:
        """Raises OllamaUnavailable naming exactly what's missing, or
        returns silently once every precondition holds. Called at the
        start of ask() so a reviewer sees this message instead of a raw
        connection error the moment they click "Ask local AI"."""
        st = self.status()
        if not st["installed"]:
            raise OllamaUnavailable(
                "Ollama isn't installed. Install it from https://ollama.com, then run "
                f"`python install_ai_model.py` to fetch the model this feature uses ({self.model})."
            )
        if not st["running"]:
            raise OllamaUnavailable(
                "Ollama is installed but not running. Start it with `ollama serve` "
                "(or open the Ollama app), then try again."
            )
        if not st["model_present"]:
            raise OllamaUnavailable(
                f"The model {self.model!r} hasn't been pulled yet. Run `python install_ai_model.py` "
                "to fetch it (a one-time download)."
            )

    def ask(self, system_prompt: str, user_prompt: str, json_schema: dict) -> dict:
        """One request/response round trip, constrained to `json_schema`
        so the reply is exactly the shape the caller asked for -- no
        free-text parsing of a model's own prose to get wrong. Raises
        OllamaUnavailable (see ensure_ready) before ever making the
        request, and again if the request itself fails partway (Ollama
        can still drop a connection mid-generation on a long reply)."""
        self.ensure_ready()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": json_schema,
            "stream": False,
            "options": {"temperature": 0},
        }
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise OllamaUnavailable(f"Could not reach Ollama at {self.host} ({e}).") from e
        return json.loads(body["message"]["content"])


def pull_model(model: str, host: str = OLLAMA_HOST) -> tuple[bool, str]:
    """Runs `ollama pull <model>`, returning (ok, combined output).

    Shells out to the real `ollama` CLI rather than the server's own
    /api/pull streaming endpoint: the CLI already shows a progress bar
    and handles a resumed/interrupted download correctly, which
    reimplementing over the raw API would only get wrong in some corner
    this project has no way to test against every platform. host is
    only used to check the server's already running first -- `ollama
    pull` itself always talks to the local server, wherever it's set up
    to run."""
    if shutil.which("ollama") is None:
        return False, "`ollama` isn't on PATH. Install it from https://ollama.com first."
    try:
        result = subprocess.run(
            ["ollama", "pull", model], capture_output=True, text=True, timeout=3600,
        )
    except subprocess.TimeoutExpired as e:
        return False, f"Timed out after an hour.\n{e.stdout or ''}\n{e.stderr or ''}"
    return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
