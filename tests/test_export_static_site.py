"""Tests for export_static_site.py's own pure logic (which documents get
a public page) and the html_view.py link-prefixing it depends on. The
actual page-writing loop isn't unit-tested here, for the same reason
dashboard.py's routes aren't (see review.py's and dashboard.py's own
module docstrings): it's a thin wrapper over already-tested render
functions, checked end to end instead by actually running the script
against real data (see the project's own manual smoke-testing convention)."""
from ai_pipeline.html_view import _legislation_href, _site_prefix
from export_static_site import select_published_slugs


def _status(parsed=True, review_status="reviewed"):
    return {"parsed": parsed, "review_status": review_status}


def test_select_published_slugs_skips_an_unparsed_document():
    statuses = {"crimes-act": _status(parsed=False)}
    assert select_published_slugs(statuses) == []


def test_select_published_slugs_includes_a_reviewed_unversioned_document():
    statuses = {"crimes-act": _status(review_status="reviewed")}
    assert select_published_slugs(statuses) == ["crimes-act"]


def test_select_published_slugs_excludes_a_document_not_fully_reviewed():
    statuses = {
        "crimes-act": _status(review_status="in-progress"),
        "evidence-act": _status(review_status="not-started"),
    }
    assert select_published_slugs(statuses) == []


def test_select_published_slugs_publishes_only_the_newest_version_of_a_work():
    statuses = {
        "criminal-procedure-act-v110": _status(review_status="reviewed"),
        "criminal-procedure-act-v114": _status(review_status="reviewed"),
    }
    assert select_published_slugs(statuses) == ["criminal-procedure-act-v114"]


def test_select_published_slugs_excludes_a_work_whose_newest_version_isnt_reviewed():
    """Even though an older version was fully reviewed, that's not what
    would be shown -- browsing this work always means its newest version,
    so nothing is published until *that* one is ready."""
    statuses = {
        "criminal-procedure-act-v110": _status(review_status="reviewed"),
        "criminal-procedure-act-v114": _status(review_status="in-progress"),
    }
    assert select_published_slugs(statuses) == []


def test_select_published_slugs_treats_independent_works_independently():
    statuses = {
        "crimes-act": _status(review_status="reviewed"),
        "evidence-act": _status(review_status="not-started"),
    }
    assert select_published_slugs(statuses) == ["crimes-act"]


def test_site_prefix_is_empty_when_base_url_has_no_browse_segment():
    assert _site_prefix("") == ""
    assert _site_prefix("something-else") == ""


def test_site_prefix_is_empty_for_the_live_dashboards_own_base_url():
    assert _site_prefix("/browse/crimes-act") == ""


def test_site_prefix_is_the_part_before_browse_for_a_project_pages_site():
    assert _site_prefix("/vic-legislation-parser/browse/crimes-act") == "/vic-legislation-parser"


def test_legislation_href_has_no_prefix_by_default():
    assert _legislation_href({"act_no": "68", "year": 2009}) == "/legislation/68-2009"


def test_legislation_href_omits_the_year_when_not_known():
    assert _legislation_href({"act_no": "68", "year": None}) == "/legislation/68"


def test_legislation_href_carries_the_site_prefix_from_base_url():
    href = _legislation_href({"act_no": "68", "year": 2009}, "/vic-legislation-parser/browse/crimes-act")
    assert href == "/vic-legislation-parser/legislation/68-2009"
