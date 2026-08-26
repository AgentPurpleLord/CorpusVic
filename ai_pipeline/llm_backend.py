"""
Pluggable backends that turn a chunk of clean Act text into structured
nodes. Both implement the same parse_chunk(chunk_text, system_prompt) ->
list[dict] contract, so structure.py doesn't need to know which is in use.

- OllamaBackend (default): talks to a local Ollama server over its REST API
  using JSON-schema-constrained output -- free, fully offline, good for fast
  iteration. Install Ollama (https://ollama.com), then pull a model, e.g.
      ollama pull qwen2.5:14b-instruct
  A 14B-class model is a good fit for an 8-16GB GPU; step down to
  qwen2.5:7b-instruct / llama3.1:8b-instruct on tighter hardware, or up to a
  32B model if you have 16GB+ VRAM to spare. Pass --model to override.

- AnthropicBackend: calls the Claude API, for a quality pass or spot-checks
  once you've iterated locally. Requires ANTHROPIC_API_KEY.
"""
import json
import urllib.error
import urllib.request

from .schema import NODE_SCHEMA

OLLAMA_HOST = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "qwen2.5:14b-instruct"
CLAUDE_DEFAULT_MODEL = "claude-opus-5"


class OllamaBackend:
    def __init__(self, model: str | None = None, host: str = OLLAMA_HOST):
        self.model = model or OLLAMA_DEFAULT_MODEL
        self.host = host.rstrip("/")

    def parse_chunk(self, chunk_text: str, system_prompt: str) -> list[dict]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Text to structure:\n\n{chunk_text}"},
            ],
            "format": NODE_SCHEMA,
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
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach Ollama at {self.host} ({e}). Is `ollama serve` "
                f"running, and has the model been pulled (`ollama pull {self.model}`)?"
            ) from e
        content = body["message"]["content"]
        return json.loads(content)["nodes"]


class AnthropicBackend:
    def __init__(self, model: str | None = None):
        import anthropic

        self.model = model or CLAUDE_DEFAULT_MODEL
        self._client = anthropic.Anthropic()

    def parse_chunk(self, chunk_text: str, system_prompt: str) -> list[dict]:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            output_config={"format": {"type": "json_schema", "schema": NODE_SCHEMA}},
            messages=[{"role": "user", "content": f"Text to structure:\n\n{chunk_text}"}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)["nodes"]


def get_backend(name: str, model: str | None = None):
    if name == "ollama":
        return OllamaBackend(model=model)
    if name == "claude":
        return AnthropicBackend(model=model)
    raise ValueError(f"Unknown backend: {name!r}")
