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
    # The raw log is still there, folded under the panel.
    assert 'class="pr-log"' in page
