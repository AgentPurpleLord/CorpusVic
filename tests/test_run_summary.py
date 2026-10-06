"""The parse summary the pipelines end their log with, and the panel the
dashboard draws from it (corpus/parsing/run_summary.py)."""
from corpus import PROJECT_ROOT
from corpus.parsing import run_summary
from corpus.web import dashboard

from conftest import make_node


def test_the_summary_line_comes_out_of_the_log_as_data(capsys):
    run_summary.emit({"slug": "bail-act-v160", "provisions": 3})
    log = "Extracting text ...\n" + capsys.readouterr().out + "Done: bail-act-v160 parsed.\n"

    out = dashboard._parse_output(log)

    assert out["summary"] == {"slug": "bail-act-v160", "provisions": 3}
    assert run_summary.MARKER not in out["log"]
    assert out["log"].splitlines() == ["Extracting text ...", "Done: bail-act-v160 parsed."]


def test_a_log_without_one_has_no_summary():
    assert dashboard._parse_output("Traceback ...")["summary"] is None


def test_provisions_are_counted_as_the_site_counts_pages():
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purpose", "The purpose..."),
        make_node("section", "2", "Definitions", "In this Act—"),
        make_node("definition", None, "court", "means..."),
        make_node("note", None, None, "See section 3."),
        make_node("schedule", "1", "Offences"),
        make_node("item", "1", None, "Murder"),
    ]
    nodes[1]["history"] = [{"text": "S. 1 amended"}]

    counts = run_summary.node_counts(nodes)

    assert counts["provisions"] == 3
    assert counts["by_type"]["definition"] == 1
    assert counts["history_notes"] == 1


def test_a_remap_report_keeps_a_sample_not_every_label():
    report = {"matched": 40, "moved": 2, "text_changed": 12, "orphaned": 0,
              "changed": [f"s{i}" for i in range(12)], "orphans": []}

    summary = run_summary.remap_summary(report)

    assert summary["matched"] == 40 and summary["kept"] == 0
    assert summary["changed"]["count"] == 12
    assert len(summary["changed"]["items"]) < 12


def test_the_dashboard_draws_the_summary_rather_than_a_log_box():
    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert '<pre id="parse-log">' not in page and '<pre id="vic-log"' not in page
    assert page.count("renderParseResult(") >= 4
    # The raw log is a button away, in a viewer of its own.
    assert 'id="log-modal"' in page and "View full log" in page


def test_every_dialog_can_be_closed_without_cancel():
    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert 'document.querySelectorAll(".overlay > .modal")' in page and '"modal-close"' in page
    assert 'e.key !== "Escape"' in page


def _report(*findings):
    from corpus.parsing.diagnostics import DiagnosticsReport, Finding
    return DiagnosticsReport(lines_total=1, lines_consumed=1, findings=[Finding(*f[:3], short=f[3]) for f in findings])


def test_note_warnings_come_as_one_plain_group():
    report = _report(
        ("warning", "history-low-confidence", "Note 'S. 4(2)(b) amended by ...' cited a more specific ...",
         "S. 4(2)(b) → section 4 (p. 32)"),
        ("warning", "history-low-confidence", "Note 'S. 9(1) amended by ...' cited a more specific ...",
         "S. 9(1) → section 9 (p. 40)"),
        ("info", "empty-node", "no body text", ""),
        ("error", "completeness", "3 line(s) were never consumed", ""),
    )

    groups = run_summary.issue_groups(report)

    assert [g["category"] for g in groups] == ["completeness", "history-low-confidence"]
    notes = groups[1]
    assert notes["count"] == 2
    assert notes["title"] == "2 amendment note(s) put on the nearest provision"
    assert notes["examples"]["items"] == ["S. 4(2)(b) → section 4 (p. 32)", "S. 9(1) → section 9 (p. 40)"]


def test_a_notes_short_form_is_what_it_cites():
    from corpus.parsing.diagnostics import _cited

    assert _cited("Notes to s. 4 amended by Nos 67/2013 s. 649(Sch. 9 item 17), 22/2020 s. 18.") == "Notes to s. 4"
    assert _cited("S. 116(1)(2) repealed by No. 14/2015 s. 73(1).") == "S. 116(1)(2)"


def test_the_last_parse_is_kept_and_can_be_read_back(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    run_summary_line = run_summary.MARKER + '{"slug": "bail-act-v160", "provisions": 3}'
    dashboard._parse_output(f"Extracting ...\n{run_summary_line}\nDone", "bail-act-v160", True)
    client = TestClient(dashboard.app)

    rows = client.get("/api/parses/last", params=[("slug", "bail-act-v160"), ("slug", "crimes-act-v1")]).json()
    assert list(rows) == ["bail-act-v160"] and rows["bail-act-v160"]["ok"] is True

    record = client.get("/api/acts/bail-act-v160/last-parse").json()
    assert record["summary"] == {"slug": "bail-act-v160", "provisions": 3}
    assert record["log"] == "Extracting ...\nDone"
    assert client.get("/api/acts/crimes-act-v1/last-parse").status_code == 404
