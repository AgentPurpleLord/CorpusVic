"""Teaching the parser (corpus/teaching): a reviewer's decisions kept as
examples keyed by the printed line, and every parse checked against them."""
import json

import pytest

from corpus.parsing.identity import annotate_ids
from corpus.parsing.rule_parser import parse_act
from corpus.storage import db
from corpus.teaching import examples, score

from conftest import HEAD_X0, PARA_X0, WRAP_X0, line, page


def _lines(third="(b) the second thing."):
    return [
        line("Part 1—Preliminary", bold=True, size=16.0, x1=300, y0=100),
        line("1 Purpose", bold=True, x0=170, x1=250, y0=120),
        line("(1) The purpose of this Act is—", x0=HEAD_X0, x1=400, y0=140),
        line("(a) the first thing; and", x0=PARA_X0, x1=380, y0=160),
        line(third, x0=PARA_X0, x1=360, y0=180),
    ]


def _parse(lines):
    result = parse_act([page(lines)])
    annotate_ids(result.nodes, result.hierarchy)
    return result


def test_every_node_keeps_what_the_parser_saw():
    nodes = _parse(_lines()).nodes
    b = next(n for n in nodes if n["number"] == "b")

    assert b["seen"]["text"] == "(b) the second thing." and b["seen"]["y0"] == 180
    assert b["seen"]["above"]["text"].endswith("the first thing; and")
    assert b["seen"]["parent"]["type"] == "subsection"


@pytest.fixture
def act(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = _parse(_lines())
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "act.json").write_text(json.dumps(
        {"nodes": result.nodes, "hierarchy": result.hierarchy, "fingerprint": "fp"}))
    db.save_parse_fingerprint("act", "fp")
    return result.nodes


def _accept(nodes, **changes):
    rows = []
    for i, n in enumerate(nodes):
        row = {**n, "_node_id": n["id"], "_source_node_index": i}
        row.update(changes.get(n["id"], {}))
        rows.append(row)
    db.save_verified("act", rows)


def test_decisions_become_examples_keyed_by_the_printed_line(act, tmp_path):
    b = next(n for n in act if n["number"] == "b")
    _accept(act, **{b["id"]: {"type": "subparagraph", "number": "ii"}})

    summary = examples.update("act", tmp_path)
    stored = examples.load("act", tmp_path)

    assert summary["kinds"] == {"confirmed": len(act) - 1, "corrected": 1}
    [corrected] = [e for e in stored if e["kind"] == "corrected"]
    assert corrected["seen"]["text"] == "(b) the second thing."
    assert (corrected["parser"]["type"], corrected["expected"]) == ("paragraph", {"type": "subparagraph", "number": "ii"})


def test_the_parser_is_checked_against_every_example_and_a_regression_is_named(act, tmp_path):
    b = next(n for n in act if n["number"] == "b")
    _accept(act, **{b["id"]: {"type": "subparagraph", "number": "ii"}})
    examples.update("act", tmp_path)

    first = score.score("act", tmp_path, nodes=act)
    assert (first["total"], first["failed"]) == (len(act), 1), "the parser still says paragraph (b)"
    assert first["failures"][0]["expected"] == {"type": "subparagraph", "number": "ii"}

    # A later parser that no longer opens (a) as a provision breaks what passed.
    worse = [n for n in _parse(_lines()).nodes if n["number"] != "a"]
    second = score.score("act", tmp_path, nodes=worse)
    assert [f["text"] for f in second["newly_broken"]] == ["(a) the first thing; and"]


def test_a_line_the_parser_no_longer_opens_keeps_its_example(act, tmp_path):
    """A false split a reviewer merged away is exactly the line a fixed
    parser stops opening -- its example must outlive the node."""
    stored = [{"id": "x1", "act": "act", "kind": "removed", "node_id": "gone", "parser": {"type": "subsection", "number": "3"},
               "expected": None, "seen": {"page": 1, "y0": 400.0, "x0": 210.0, "text": "(3), admissible as if"}}]
    path = examples.examples_path("act", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored[0]) + "\n")
    _accept(act)

    examples.update("act", tmp_path)
    kept = [e for e in examples.load("act", tmp_path) if e["id"] == "x1"]
    assert kept and score.check(kept, act)[0]["passed"], "nothing opens there now, as the reviewer said"


def test_the_dashboard_checks_in_the_background(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import corpus.web.dashboard as dashboard

    class Now:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_DASHBOARD_USERNAME", None)
    monkeypatch.setattr(dashboard.threading, "Thread", Now)
    monkeypatch.setattr(dashboard, "_find_source_pdf", lambda slug: tmp_path / "act.pdf")
    monkeypatch.setattr("corpus.teaching.examples.update", lambda slug, base: {"examples": 3})
    monkeypatch.setattr("corpus.teaching.score.score", lambda slug, base: {"passed": 3, "failed": 0, "newly_broken": []})
    client = TestClient(dashboard.app)

    client.post("/api/teaching/act/check")
    check = client.get("/api/teaching/act").json()["check"]
    assert check["state"] == "done" and check["score"]["passed"] == 3
    assert client.get("/teaching/act/").status_code == 200
