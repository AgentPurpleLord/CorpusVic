"""The corpus-wide rule check.

The lines it reads live in data/extracted/, which is not kept in git, so
the fixtures below build a small document of their own rather than
depending on a parse having been run.
"""
import json

import pytest

from corpus.parsing import check_rules


def a_line(text, x0, y0, *, size=12.0, bold=False):
    return {"text": text, "x0": x0, "x1": x0 + 200, "y0": y0, "y1": y0 + 12,
            "page_no": 1, "size": size, "bold": bold, "leading_bold_italic": None}


def a_node(node_type, number, y0, x0, text=""):
    return {"type": node_type, "number": number, "heading": None, "text": text,
            "rects": [{"page": 1, "x0": x0, "y0": y0, "x1": x0 + 200, "y1": y0 + 12}]}


@pytest.fixture
def document(tmp_path, monkeypatch):
    """One Act, in both halves: the lines as printed, and the parse."""
    parsed, extracted = tmp_path / "parsed", tmp_path / "extracted"
    parsed.mkdir(), extracted.mkdir()
    monkeypatch.setattr(check_rules, "PARSED_DIR", parsed)
    monkeypatch.setattr(check_rules, "EXTRACTED_DIR", extracted)

    def write(lines, nodes):
        (extracted / "act.json").write_text(json.dumps(
            [{"page_no": 1, "page_width": 595.0, "body_lines": lines}]))
        (parsed / "act.json").write_text(json.dumps({"act": "act", "nodes": nodes}))
    return write


def test_a_parse_the_rules_agree_with(document):
    document(
        [a_line("5 Meaning of harm", 163.7, 100, bold=True),
         a_line("(1) In this Act—", 190.2, 120),
         a_line("(a) harm includes psychological harm;", 216.2, 140)],
        [a_node("section", "5", 100, 163.7),
         a_node("subsection", "1", 120, 190.2),
         a_node("paragraph", "a", 140, 216.2)],
    )
    report = check_rules.check_all()
    assert report.checked == 3
    assert report.agreement == 1.0
    assert report.divergences == []


def test_a_divergence_names_the_line(document):
    document(
        [a_line("5 Meaning of harm", 163.7, 100, bold=True),
         a_line("(a) harm includes psychological harm;", 216.2, 120)],
        [a_node("section", "5", 100, 163.7),
         a_node("subparagraph", "a", 120, 216.2)],
    )
    report = check_rules.check_all()
    assert report.agreement == 0.5
    diverged = report.divergences[0]
    assert (diverged.parsed_as, diverged.rules_say) == ("subparagraph", "paragraph")
    assert "psychological harm" in diverged.line


def test_a_provision_with_no_line_of_its_own_is_counted_not_judged(document):
    # A node built from a table cell, whose rectangle matches no body line.
    document([a_line("5 Meaning of harm", 163.7, 100, bold=True)],
             [a_node("section", "5", 100, 163.7), a_node("paragraph", "a", 900, 216.2)])
    report = check_rules.check_all()
    assert (report.checked, report.unmatched) == (1, 1)


def test_a_rounded_rectangle_still_finds_its_line(document):
    document([a_line("5 Meaning of harm", 163.7, 100.4, bold=True)],
             [a_node("section", "5", 99.6, 163.7)])
    assert check_rules.check_all().checked == 1


def test_nothing_to_check_says_how_to_get_the_lines(document, monkeypatch, capsys):
    document([], [a_node("section", "5", 100, 163.7)])
    monkeypatch.setattr("sys.argv", ["check_rules"])
    with pytest.raises(SystemExit) as raised:
        check_rules.main()
    assert "run_pipeline" in str(raised.value)


def test_the_report_groups_divergences_by_class(document):
    document(
        [a_line("(a) one;", 216.2, 100), a_line("(b) two;", 216.2, 120)],
        [a_node("subparagraph", "a", 100, 216.2), a_node("subparagraph", "b", 120, 216.2)],
    )
    assert check_rules.check_all().classes() == {("subparagraph", "paragraph"): 2}
