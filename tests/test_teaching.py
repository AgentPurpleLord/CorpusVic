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


# -- Stage 2: rules proposed from disagreements, applied once approved ------

from corpus.teaching import propose, rules


def _failures_like(seen, numbers, expected_type="subparagraph", act="act"):
    """Disagreements on lines that look like `seen`, one per number."""
    out = []
    for n in numbers:
        text = seen["text"].replace("(b)", f"({n})", 1)
        out.append({"id": f"ex-{n}", "act": act, "kind": "corrected", "parser": {"type": "paragraph", "number": n},
                    "expected": {"type": expected_type, "number": n}, "got": {"type": "paragraph", "number": n},
                    "seen": {**seen, "text": text}})
    return out


def test_a_line_shape_captures_the_number_you_gave_it():
    assert propose.shape("1.2 The court may", "1.2") == r"^(\d+(?:\.\d+)*[A-Z]*)(?:\s|$)"
    assert propose.shape("(iv) a thing", "iv") == r"^\(([a-z]+)\)(?:\s|$)"
    assert propose.shape("(3), admissible as if", None) == r"^\(\d+\),(?:\s|$)"
    assert propose.shape("Section 12 says", "12") is None, "a number past the opening word can't be read"


def test_three_alike_disagreements_make_a_rule_that_changes_the_parse(act):
    b = next(n for n in act if n["number"] == "b")
    assert propose.propose(_failures_like(b["seen"], ["b", "c"])) == [], "two is not a lesson"

    [rule] = propose.propose(_failures_like(b["seen"], ["b", "c", "d"]))
    assert rule["then"] == {"open": "subparagraph", "number": True}
    assert rule["scope"] == "act" and rule["count"] == 3
    assert "subparagraph" in rule["description"]
    assert rules.matches(rule, b["seen"])

    reread = next(n for n in parse_act([page(_lines())], learned=[rule]).nodes if n["number"] == "b")
    assert (reread["type"], reread.get("learned")) == ("subparagraph", rule["id"])
    assert reread["text"] == "the second thing."
    assert propose.propose(_failures_like(b["seen"], ["b", "c", "d"]), decided={rule["id"]}) == []


def test_a_learned_rule_can_start_an_example_or_carry_on_the_line_above():
    base = {"pattern": r"^\(b\)(?:\s|$)"}
    example = parse_act([page(_lines())], learned=[{"id": "r1", "when": base, "then": {"open": "example"}}]).nodes
    assert [n["text"] for n in example if n["type"] == "example"] == ["(b) the second thing."]

    carried = parse_act([page(_lines())], learned=[{"id": "r2", "when": base, "then": {"continue": True}}]).nodes
    assert not any(n["number"] == "b" for n in carried)
    assert next(n for n in carried if n["number"] == "a")["text"].endswith("(b) the second thing.")


def test_a_preview_shows_every_line_a_rule_would_change(act, tmp_path, monkeypatch):
    b = next(n for n in act if n["number"] == "b")
    _accept(act, **{b["id"]: {"type": "subparagraph", "number": "b"}})
    examples.update("act", tmp_path)
    monkeypatch.setattr(score, "body_pages", lambda a, base=None: [page(_lines())])
    [rule] = propose.propose(_failures_like(b["seen"], ["b", "c", "d"]))

    out = propose.preview(rule, tmp_path, acts=["act"])
    assert [(c["text"], c["before"]["type"], c["after"]["type"]) for c in out["changes"]] == \
           [("(b) the second thing.", "paragraph", "subparagraph")]
    assert [f["id"] for f in out["fixed"]] == [examples.example_id("act", b["seen"])] and out["broken"] == []


def test_the_lessons_page_approves_rejects_and_withdraws(act, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import corpus.web.dashboard as dashboard

    class Now:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    b = next(n for n in act if n["number"] == "b")
    last = tmp_path / "data" / "teaching" / ".last" / "act.json"
    last.parent.mkdir(parents=True)
    last.write_text(json.dumps({"passed": [], "failures": _failures_like(b["seen"], ["b", "c", "d"])}))
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_DASHBOARD_USERNAME", None)
    monkeypatch.setattr(dashboard.threading, "Thread", Now)
    monkeypatch.setattr(propose, "preview", lambda rule, base, progress=None: {"changes": [], "fixed": [], "broken": []})
    client = TestClient(dashboard.app)

    [c] = client.get("/api/lessons").json()["candidates"]
    assert client.post(f"/api/lessons/{c['id']}/preview").json()["state"] == "done"
    assert client.get("/api/lessons").json()["candidates"][0]["preview"]["state"] == "done"

    client.post(f"/api/lessons/{c['id']}/decide", json={"decision": "approve", "scope": "all"})
    listed = client.get("/api/lessons").json()
    assert listed["candidates"] == [] and [r["scope"] for r in listed["rules"]] == ["all"]
    assert rules.for_act("anything-else", tmp_path)[0]["id"] == c["id"]

    client.post(f"/api/lessons/rules/{c['id']}/withdraw")
    listed = client.get("/api/lessons").json()
    assert (listed["candidates"], listed["rules"], listed["rejected"]) == ([], [], 1), "withdrawn stays withdrawn"
    assert client.get("/lessons/").status_code == 200


# -- Stage 3: "Find others like this" in review ------------------------------

def _two_sections():
    def section(n, y):
        return [
            line(f"{n} Orders", bold=True, x0=170, x1=250, y0=y),
            line("(1) The court may make an order that is set out", x0=HEAD_X0, x1=400, y0=y + 20),
            line("in full under subsection", x0=WRAP_X0, x1=300, y0=y + 40),
            line("(2) and then stops.", x0=HEAD_X0, x1=300, y0=y + 60),
            line("(3) The court must—", x0=HEAD_X0, x1=320, y0=y + 80),
            line("(a) the first thing; and", x0=PARA_X0, x1=380, y0=y + 100),
            line("(b) the second thing.", x0=PARA_X0, x1=360, y0=y + 120),
        ]
    return [line("Part 1—Preliminary", bold=True, size=16.0, x1=300, y0=80)] + section(1, 100) + section(2, 300)


@pytest.fixture
def reviewing(tmp_path, monkeypatch):
    from corpus.review import review

    monkeypatch.chdir(tmp_path)
    result = _parse(_two_sections())
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "act.json").write_text(json.dumps(
        {"nodes": result.nodes, "hierarchy": result.hierarchy, "fingerprint": "fp"}))
    db.save_parse_fingerprint("act", "fp")
    review._load_state("act")
    return review, result.nodes


def _at(nodes, section, type_, number):
    """Index of the piece in the given section (1 or 2)."""
    return [i for i, n in enumerate(nodes) if (n["type"], n["number"]) == (type_, number)][section - 1]


def test_a_retype_finds_the_same_misreading_elsewhere_and_fixes_it(reviewing):
    from fastapi.testclient import TestClient

    review, nodes = reviewing
    client = TestClient(review.app)
    first, second = _at(nodes, 1, "paragraph", "b"), _at(nodes, 2, "paragraph", "b")
    client.post(f"/api/nodes/{first}/edit", json={"type": "subparagraph", "number": "b", "text": "the second thing."})

    found = client.get(f"/api/nodes/{first}/alike").json()
    # Not the (a)s: the same shape, but met with a subsection open, not a paragraph.
    assert [m["node_index"] for m in found["matches"]] == [second]
    assert found["matches"][0]["new_number"] == "b" and "subparagraph" in found["description"]

    out = client.post(f"/api/nodes/{first}/alike", json={"targets": [second]}).json()
    assert out == {"applied": [second], "skipped": []}
    assert review._current_node(second)["type"] == "subparagraph"
    assert client.get(f"/api/nodes/{first}/alike").json()["matches"] == [], "already reads that way"


def test_a_false_split_merged_away_finds_its_twin(reviewing):
    from fastapi.testclient import TestClient

    review, nodes = reviewing
    client = TestClient(review.app)
    split, twin = _at(nodes, 1, "subsection", "2"), _at(nodes, 2, "subsection", "2")
    client.post("/api/merge", json={"target_node_index": split - 1, "source_node_indices": [split]})

    found = client.get(f"/api/nodes/{split}/alike?merging=true").json()
    # Not (3): its line above finished a sentence.
    assert [m["node_index"] for m in found["matches"]] == [twin]

    client.post(f"/api/nodes/{split}/alike", json={"merging": True, "targets": [twin]})
    assert twin in review._merged_away
    assert review._current_node(twin - 1)["text"].endswith("and then stops.")
