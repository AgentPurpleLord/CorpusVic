"""Tests for ai_pipeline/ai_scan.py's batch-formatting and reply-parsing
logic -- a fake backend stands in for OllamaBackend, same style as
tests/test_ai_assist.py, so these run with no real model or network
involved."""
from ai_pipeline.ai_scan import MAX_UNIT_CHARS, _format_batch, _unit_text, iter_unit_batches, scan_batch, unit_root
from conftest import make_node


class _FakeBackend:
    model = "qwen2.5:7b-instruct"

    def __init__(self, reply=None):
        self.reply = reply if reply is not None else {"concerns": []}
        self.calls = []

    def ask(self, system_prompt, user_prompt, json_schema):
        self.calls.append({"system": system_prompt, "user": user_prompt, "schema": json_schema})
        return self.reply


def test_unit_root_is_the_units_first_node():
    assert unit_root([5, 6, 7]) == 5


def test_unit_text_includes_type_number_heading_and_joined_body():
    nodes = [
        make_node("section", "5", "Definitions", "lead-in text"),
        make_node("subsection", "1", None, "first nested subsection"),
    ]
    text = _unit_text(nodes, [0, 1])
    assert "type='section'" in text
    assert "number='5'" in text
    assert "heading='Definitions'" in text
    assert "lead-in text" in text
    assert "first nested subsection" in text


def test_unit_text_truncates_a_very_long_body():
    nodes = [make_node("section", "1", None, "word " * 500)]
    text = _unit_text(nodes, [0])
    assert len(text) < 500 * 5
    assert text.rstrip("'\"").endswith("...")


def test_unit_text_treats_a_missing_text_field_as_empty():
    nodes = [make_node("heading_group", None, "Part 1", text=None)]
    text = _unit_text(nodes, [0])
    assert "text=''" in text


def test_format_batch_numbers_units_from_one():
    nodes = [make_node("section", "1", None, "first"), make_node("section", "2", None, "second")]
    formatted = _format_batch(nodes, [[0], [1]])
    assert formatted.startswith("1. ")
    assert "\n\n2. " in formatted


def test_scan_batch_returns_only_flagged_units_zero_indexed():
    backend = _FakeBackend({"concerns": [{"index": 2, "concern": "heading reads as a sentence", "severity": "warning"}]})
    nodes = [make_node("section", str(i), None, "text") for i in range(1, 4)]
    units = [[0], [1], [2]]

    flagged = scan_batch(nodes, units, backend=backend)

    assert flagged == {1: {"severity": "warning", "concern": "heading reads as a sentence"}}


def test_scan_batch_ignores_an_out_of_range_index():
    """A model naming an index outside the batch it was actually shown is
    a malformed reply, not a real finding about some other unit."""
    backend = _FakeBackend({"concerns": [{"index": 99, "concern": "bogus", "severity": "warning"}]})
    nodes = [make_node("section", "1", None, "text")]

    flagged = scan_batch(nodes, [[0]], backend=backend)

    assert flagged == {}


def test_scan_batch_defaults_a_missing_severity_to_info():
    backend = _FakeBackend({"concerns": [{"index": 1, "concern": "looks odd"}]})
    nodes = [make_node("section", "1", None, "text")]

    flagged = scan_batch(nodes, [[0]], backend=backend)

    assert flagged[0]["severity"] == "info"


def test_scan_batch_returns_nothing_when_the_model_flags_nothing():
    backend = _FakeBackend({"concerns": []})
    nodes = [make_node("section", "1", None, "text")]

    assert scan_batch(nodes, [[0]], backend=backend) == {}


def test_iter_unit_batches_chunks_by_batch_size():
    units = [[i] for i in range(25)]
    batches = list(iter_unit_batches(units, batch_size=10))
    assert [start for start, _ in batches] == [0, 10, 20]
    assert [len(b) for _, b in batches] == [10, 10, 5]
