"""Tests for corpus/reader.py -- the one assembly of each reader page,
shared by the dashboard, the static export and the public site.

Against a stand-in source rather than the real document lookups. What
this module does is gather and pass along, so what is worth pinning down
is exactly that: which lookups it asks for, what it does to the answers,
and which of its options reach the renderer. The renderers themselves are
covered by tests/test_html_view.py, and the real lookups by
tests/test_dashboard.py.
"""
from corpus import reader

from conftest import make_node


class FakeSource:
    """Everything corpus/reader.py reads a document through, and a record
    of what it asked for."""

    def __init__(self, nodes=None, endnotes=None, superseded=None,
                 crossrefs=None, timeline=None, version_urls=None,
                 mixed_parsers=False):
        self.nodes = nodes if nodes is not None else [
            make_node("part", "1", "Preliminary"),
            make_node("section", "3", "Definitions", "In this Act—"),
            make_node("section", "4", "Application", "This Act applies to—"),
        ]
        self.endnotes = endnotes
        self._superseded_value = superseded
        self._crossrefs = crossrefs or []
        self._timeline_entries = timeline or []
        self._version_urls = version_urls or {}
        self._mixed_parsers = mixed_parsers
        self.asked = []

    def _record(self, name):
        self.asked.append(name)

    def _act_title(self, slug):
        self._record("title")
        return "Test Act 2020"

    def _current_nodes(self, slug):
        self._record("nodes")
        return self.nodes, [], None

    def _amendments(self, slug):
        self._record("amendments")
        return {"endnotes": self.endnotes, "index": {}, "summary": None}

    def _act_version(self, slug):
        self._record("version")
        return {}

    def _superseded(self, slug):
        self._record("superseded")
        return self._superseded_value

    def _page_index(self, slug):
        self._record("page_index")
        return {"by_node_index": {1: "s3", 2: "s4"},
                "schedule_by_node_index": {},
                "by_key": {}}

    def _provision_timeline(self, slug, number, schedule, node_type):
        self._record("timeline_for_provision")
        return self._timeline_entries, self._version_urls

    def _section_crossrefs(self, slug, number, schedule=None):
        self._record("crossrefs")
        return self._crossrefs

    def _version_dates(self, slug):
        self._record("version_dates")
        return {}

    def _timeline(self, work):
        self._record("timeline")
        return {"mixed_parsers": self._mixed_parsers}


# ---------------------------------------------------------------------
# The pages themselves
# ---------------------------------------------------------------------


def test_the_contents_page_lists_the_documents_provisions():
    body = reader.contents_page(FakeSource(), "test-act", "/browse/test-act")
    assert "Definitions" in body
    assert "Application" in body


def test_a_section_page_renders_that_provision():
    body = reader.section_page(FakeSource(), "test-act", "/browse/test-act", "s3")
    assert "Definitions" in body


def test_a_section_that_does_not_exist_comes_back_as_none():
    """A 404 for a live server, and a page the static build skips. Both
    need to be able to tell "no such section" from "empty section"."""
    assert reader.section_page(FakeSource(), "test-act", "/browse/test-act", "s99") is None


def test_endnotes_are_none_for_a_document_without_any():
    """A Bill, an Explanatory Memorandum, or an Act parsed before
    corpus/endnotes.py existed."""
    assert reader.endnotes_page(FakeSource(), "test-act", "/browse/test-act") is None


# ---------------------------------------------------------------------
# The options that make one caller's pages differ from another's
# ---------------------------------------------------------------------


def test_the_notice_sits_above_the_contents_page_rather_than_inside_it():
    """The published site says a document is only part-checked before the
    reader starts reading it, not somewhere further down."""
    body = reader.contents_page(FakeSource(), "test-act", "/browse/test-act",
                                notice="<p id='n'>Only 2 of 9 checked.</p>")
    assert body.startswith("<p id='n'>Only 2 of 9 checked.</p>")


def test_a_section_page_carries_the_notice_it_is_given():
    body = reader.section_page(FakeSource(), "test-act", "/browse/test-act", "s3",
                               notice="<p>Nobody has checked this yet.</p>")
    assert "Nobody has checked this yet." in body


def test_no_notice_is_added_when_none_is_asked_for():
    body = reader.contents_page(FakeSource(), "test-act", "/browse/test-act")
    assert not body.startswith("<p")


def test_the_review_badge_is_the_callers_choice():
    """A reviewer wants to see how far through a document is. A reader is
    told something different, per provision, by the unverified notice --
    so the published site turns the badge off rather than telling them
    twice in two different vocabularies."""
    shown = reader.contents_page(FakeSource(), "test-act", "/browse/test-act",
                                 show_review_badge=True)
    hidden = reader.contents_page(FakeSource(), "test-act", "/browse/test-act",
                                  show_review_badge=False)
    assert "Not yet reviewed" in shown
    assert "Not yet reviewed" not in hidden


def test_rewrite_is_applied_to_every_address_the_source_hands_back():
    """The static build publishes an Act's newest version at the work's
    own address, so every URL the data carries has to be rewritten to
    match. Missing one produces a link into a version nobody published."""
    seen = []

    def rewrite(value):
        seen.append(value)
        return value

    source = FakeSource(
        superseded={"href": "/browse/test-act-v2/"},
        crossrefs=[{"href": "/browse/test-bill/section/c3", "label": "clause 3"}],
        version_urls={"v1": "/browse/test-act-v1/section/s3"},
    )
    reader.section_page(source, "test-act", "/browse/test-act", "s3", rewrite=rewrite)

    # The three addresses that come out of the document data rather than
    # out of the URL this page was asked for.
    assert {"href": "/browse/test-act-v2/"} in seen
    assert [{"href": "/browse/test-bill/section/c3", "label": "clause 3"}] in seen
    assert {"v1": "/browse/test-act-v1/section/s3"} in seen


def test_without_a_rewrite_addresses_are_left_alone():
    source = FakeSource(superseded={"href": "/browse/test-act-v2/"})
    body = reader.contents_page(source, "test-act", "/browse/test-act")
    assert body is not None  # the identity default is used, not None-crashed


# ---------------------------------------------------------------------
# What it asks the source for
# ---------------------------------------------------------------------


def test_commentary_is_not_looked_up_for_a_provision_that_cannot_have_any():
    """Bill/EM commentary is only ever matched against an ordinary
    numbered provision, never against a Part or a Schedule as a whole --
    looking it up anyway borrows the same-numbered section's commentary
    onto the wrong page."""
    source = FakeSource()
    reader.section_page(source, "test-act", "/browse/test-act", "s3")
    assert "crossrefs" in source.asked

    part_only = FakeSource(nodes=[make_node("part", "1", "Preliminary")])
    part_only._page_index = lambda slug: {
        "by_node_index": {0: "part-1"}, "schedule_by_node_index": {}, "by_key": {}}
    reader.section_page(part_only, "test-act", "/browse/test-act", "part-1")
    assert "crossrefs" not in part_only.asked
