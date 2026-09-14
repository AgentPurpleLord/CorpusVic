"""Tests for corpus/ai/assist.py's context-building and prompt
logic -- a fake backend stands in for OllamaBackend so these run with
no real model or network involved (see tests/test_llm_backend.py for
the backend's own status/error-message tests)."""
from corpus.ai.assist import build_suggestion
from conftest import make_node


class _FakeBackend:
    """Records the prompt it was asked and returns a fixed reply, so a
    test can assert on exactly what context ai_assist.py handed the
    model without needing a real one."""

    model = "qwen2.5:7b-instruct"

    def __init__(self, reply=None):
        self.reply = reply or {"answer": "an answer", "reasoning": "some reasoning", "confidence": "medium"}
        self.calls = []

    def ask(self, system_prompt, user_prompt, json_schema):
        self.calls.append({"system": system_prompt, "user": user_prompt, "schema": json_schema})
        return self.reply


def test_build_suggestion_returns_the_backends_answer_tagged_with_its_model():
    backend = _FakeBackend({"answer": "the second one", "reasoning": "text reads as a continuation", "confidence": "high"})
    nodes = [make_node("section", "5", None, "text")]
    finding = {"category": "duplicate-number", "message": "section '5' appears twice"}

    result = build_suggestion(finding, 0, nodes, backend=backend)

    assert result == {
        "answer": "the second one", "reasoning": "text reads as a continuation",
        "confidence": "high", "model": "qwen2.5:7b-instruct",
    }


def test_duplicate_number_context_includes_the_other_matching_node():
    backend = _FakeBackend()
    nodes = [
        make_node("section", "5", None, "first copy of section 5"),
        make_node("section", "6", None, "an unrelated section"),
        make_node("section", "5", None, "second copy of section 5"),
    ]
    finding = {"category": "duplicate-number", "message": "section '5' appears twice"}

    build_suggestion(finding, 0, nodes, backend=backend)

    prompt = backend.calls[0]["user"]
    assert "first copy of section 5" in prompt
    assert "second copy of section 5" in prompt
    assert "an unrelated section" not in prompt  # a different number -- not part of the question


def test_duplicate_number_context_ignores_a_different_type_with_the_same_number():
    """A section 5 and a subsection (5) sharing the digits "5" aren't
    the same collision diagnostics.py flagged -- only same-type matches
    belong in the question."""
    backend = _FakeBackend()
    nodes = [
        make_node("section", "5", None, "the flagged section"),
        make_node("subsection", "5", None, "an unrelated subsection"),
    ]
    finding = {"category": "duplicate-number", "message": "section '5' appears twice"}

    build_suggestion(finding, 0, nodes, backend=backend)

    assert "an unrelated subsection" not in backend.calls[0]["user"]


def test_history_low_confidence_context_includes_the_note_and_nested_provisions():
    backend = _FakeBackend()
    nodes = [
        make_node("section", "10", None, "lead-in text", history=[
            {"raw": "S. 10(3) amended by No. 1/2000 s. 4.", "confidence": "low"},
        ]),
        make_node("subsection", "1", None, "first nested subsection"),
        make_node("subsection", "2", None, "second nested subsection"),
        make_node("section", "11", None, "the next section, outside this one"),
    ]
    finding = {"category": "history-low-confidence", "message": "cited a more specific provision than was found"}

    build_suggestion(finding, 0, nodes, backend=backend)

    prompt = backend.calls[0]["user"]
    assert "S. 10(3) amended by No. 1/2000 s. 4." in prompt
    assert "first nested subsection" in prompt
    assert "second nested subsection" in prompt
    assert "the next section, outside this one" not in prompt  # past the boundary -- not nested under it


def test_unsupported_category_falls_back_to_a_generic_question():
    backend = _FakeBackend()
    nodes = [make_node("section", "1", None, "some text")]
    finding = {"category": "some-future-category", "message": "something diagnostics didn't have a specific handler for"}

    build_suggestion(finding, 0, nodes, backend=backend)

    prompt = backend.calls[0]["user"]
    assert "some text" in prompt
    assert "what (if anything) looks wrong" in prompt


def test_the_question_asks_for_honesty_rather_than_a_forced_guess():
    backend = _FakeBackend()
    build_suggestion(
        {"category": "duplicate-number", "message": "m"}, 0,
        [make_node("section", "1", None, "t")], backend=backend,
    )
    assert "aren't confident" in backend.calls[0]["system"] or "confidence" in backend.calls[0]["system"]
