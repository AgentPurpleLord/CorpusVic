"""Tests for extract_act_registry.py's pure logic -- short-title cleanup
and registry building from already-extracted rows. Row extraction itself
(extract_rows) needs a real PDF and is instead validated by running the
script against the actual em/List-of-Acts-in-chronological-order.pdf and
spot-checking known Acts, per this project's established practice for
anything that needs a real document to exercise meaningfully."""
from extract_act_registry import build_registry, short_title


def test_short_title_strips_a_trailing_archaic_citation():
    assert short_title("Roman Catholic Relief Act 1830 10 Geo. IV No. 9") == "Roman Catholic Relief Act 1830"


def test_short_title_leaves_a_modern_title_with_nothing_trailing_unchanged():
    assert short_title("Sentencing Act 1991") == "Sentencing Act 1991"


def test_short_title_handles_a_wrapped_two_line_title_already_joined():
    assert short_title("Wesleyan Methodists Independents and Baptists Act 1838 2 Vict. No. 7") == (
        "Wesleyan Methodists Independents and Baptists Act 1838"
    )


def test_short_title_returns_none_for_a_row_with_no_recognisable_title():
    assert short_title("some unparseable row fragment") is None


def test_build_registry_marks_a_repealed_act_correctly():
    rows = [{"year": "1841", "act_no": "14", "title": "Bank of Australasia Act 1841 5 Vict. No. 14", "repealed_by": "21/2007", "provision": "s. 4"}]
    registry = build_registry(rows)
    assert registry == {
        "Bank of Australasia Act 1841": {
            "year": "1841",
            "act_no": "14",
            "repealed_by": "21/2007",
            "repealed_provision": "s. 4",
            "in_force": False,
        }
    }


def test_build_registry_marks_an_unrepealed_act_as_in_force():
    rows = [{"year": "1991", "act_no": "49/1991", "title": "Sentencing Act 1991", "repealed_by": "", "provision": ""}]
    registry = build_registry(rows)
    assert registry["Sentencing Act 1991"]["in_force"] is True
    assert registry["Sentencing Act 1991"]["repealed_by"] is None
    assert registry["Sentencing Act 1991"]["repealed_provision"] is None


def test_build_registry_skips_rows_with_no_recognisable_title():
    rows = [{"year": "1900", "act_no": "1", "title": "garbled row text", "repealed_by": "", "provision": ""}]
    assert build_registry(rows) == {}
