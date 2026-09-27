"""corpus/review/report.py: what was flagged and denied for one Act, with
what is needed to see why the parser read it wrong, as Markdown."""
import json

from corpus.parsing.identity import annotate_ids
from corpus.review import report
from corpus.storage import db

HIERARCHY = ["section", "subsection", "paragraph"]


def _setup(tmp_path):
    nodes = [{"type": "section", "number": "11", "heading": "Place of hearing", "text": "", "page_start": 3},
             {"type": "subsection", "number": "1", "heading": None, "text": "A proceeding must be heard (a) here",
              "page_start": 3, "rects": [{"page": 3, "x0": 200, "y0": 300, "x1": 400, "y1": 314}]},
             {"type": "subsection", "number": "2", "heading": None, "text": "The court may move it.", "page_start": 3}]
    annotate_ids(nodes, HIERARCHY)
    for folder in ("parsed", "extracted", "diagnostics"):
        (tmp_path / "data" / folder).mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "act-v2.json").write_text(json.dumps({"nodes": nodes, "hierarchy": HIERARCHY}))
    line = lambda y, text, bold=False: {"text": text, "x0": 210.0, "x1": 400.0, "y0": y, "y1": y + 12,
                                        "page_no": 3, "size": 12.0, "bold": bold}
    (tmp_path / "data" / "extracted" / "act-v2.json").write_text(json.dumps([{"page_no": 3, "body_lines": [
        line(100, "a line far above"), line(270, "11 Place of hearing", True), line(286, "the line before"),
        line(301, "(1) A proceeding must be heard (a) here"), line(318, "(2) The court may move it."),
        line(334, "the line after that"), line(600, "a line far below")]}]))
    (tmp_path / "data" / "diagnostics" / "act-v2.json").write_text(json.dumps(
        [{"severity": "warning", "category": "list", "message": "(a) inline", "node_index": 1}]))
    db.save_verified("act-v2", [{**nodes[1], "_node_id": nodes[1]["id"], "_source_node_index": 1,
                                 "verified_at": "2026-01-01", "needs_followup": True}], tmp_path)
    db.save_review_notes("act-v2", {nodes[1]["id"]: "(a) should be its own paragraph"}, tmp_path)
    return nodes


def test_the_report_carries_what_is_needed_to_see_why(tmp_path):
    nodes = _setup(tmp_path)
    denied = {"provision": '["provision", null, "11"]', "section": "s 11", "piece": "1", "label": "(1)",
              "from": 1, "to": 2, "op": "changed", "decision": "denied", "note": "the same words, re-wrapped",
              "old_html": "<del>heard</del>", "new_html": "heard <ins>(a) here</ins>", "notes": ["S. 11 amended"],
              "instructions": [], "old_at": None, "new_at": {"page": 3, "rects": nodes[1]["rects"], "text": ""}}
    md = report.build("act", ["act-v1", "act-v2"], tmp_path, [denied], {}, {}, code_version="abc123", base=2)

    flagged, history = md.split("## Denied in History review")
    assert "(a) should be its own paragraph" in flagged and "s 11(1)" in flagged
    assert "A proceeding must be heard (a) here" in flagged, "the parser's reading"
    assert "(a) inline" in flagged, "the diagnostics"
    assert "the line before" in flagged and "the line after that" in flagged and "far above" not in flagged, \
        "the printed lines it came from, and a couple either side"
    assert "v1 → v2" in history and "the same words, re-wrapped" in history and "heard (a) here" in history
    assert "abc123" in md and "base v2" in md


def test_a_denial_is_noted_and_travels_without_rewriting_old_lines(tmp_path):
    from corpus.review import review_sync

    db.save_history_decision("act", "k", 1, 2, "whole", "denied", tmp_path, note="parser split it")
    db.save_history_decision("act", "k", 2, 3, "whole", "confirmed", tmp_path)
    assert db.load_history_notes("act", tmp_path) == {("k", 1, 2, "whole"): "parser split it"}
    review_sync.export(tmp_path)
    lines = (tmp_path / "data" / "review" / "act" / "history_decisions.jsonl").read_text().splitlines()
    assert sum('"note"' in l for l in lines) == 1, "an empty note is left out, not written as null"
