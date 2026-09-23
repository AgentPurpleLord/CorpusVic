"""The review tool on one version of a work held in several: only what
differs from the version it is compared with is left to review."""
import json

import pytest

from corpus.parsing.identity import annotate_ids
from corpus.review import review
from corpus.review.review import AcceptRequest, accept_page
from corpus.storage import db

HIERARCHY = ["part", "section", "subsection"]


def _parse(tmp_path, slug, version, sections):
    nodes = [{"type": "part", "number": "1", "heading": "Preliminary", "text": ""}]
    for number, text in sections:
        nodes.append({"type": "section", "number": number, "heading": f"Heading {number}", "text": ""})
        nodes.append({"type": "subsection", "number": "1", "heading": None, "text": text})
    annotate_ids(nodes, HIERARCHY)
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / f"{slug}.json").write_text(json.dumps({
        "nodes": nodes, "hierarchy": HIERARCHY, "fingerprint": f"fp-{slug}",
        "version": {"version": version, "as_at_printed": f"{version} May 2026"},
    }))
    return nodes


@pytest.fixture
def two_versions(tmp_path, monkeypatch):
    """Version 2 is current and fully reviewed, with a typo fixed in s 1;
    version 1 differs from it only in s 2."""
    monkeypatch.chdir(tmp_path)
    _parse(tmp_path, "act-v1", 1, [("1", "the acused"), ("2", "old words")])
    current = _parse(tmp_path, "act-v2", 2, [("1", "the acused"), ("2", "new words")])
    rows = []
    for i, node in enumerate(current):
        row = {**node, "_node_id": node["id"], "_source_node_index": i, "verified_at": "2026-05-01"}
        if node["type"] == "subsection" and node["text"] == "the acused":
            row["text"] = "the accused"
        rows.append(row)
    rows[0]["_unit_end_index"], rows[2]["_unit_end_index"], rows[4]["_unit_end_index"] = 0, 1, 2
    db.save_verified("act-v2", rows)
    db.save_parse_fingerprint("act-v2", "fp-act-v2")
    review._load_state("act-v1")
    return tmp_path


def _unit(number):
    return next(u for u in review.get_meta()["units"] if u["number"] == number)


def test_an_unchanged_section_is_vouched_for_by_the_reviewed_version(two_versions):
    assert _unit("1")["status"] == "inherited"
    assert review.get_unit(_unit("1")["unit_no"])["lineage"]["source"] == 2


def test_only_the_changed_section_is_left_to_review(two_versions):
    info = review.get_meta()["version_info"]

    assert _unit("2")["status"] == "pending"
    assert info["to_review"] == 1 and info["reference"] == 2


def test_a_changed_section_shows_how_its_words_differ(two_versions):
    compare = review.get_unit(_unit("2")["unit_no"])["lineage"]["compare"]

    assert '<del class="d-del">old</del>' in compare
    assert '<ins class="d-ins">new</ins>' in compare


def test_the_browse_view_reads_an_inherited_section_as_reviewed(two_versions):
    from corpus.review import inheritance

    state = inheritance.work_review("act-v1")
    nodes = inheritance.effective_nodes("act-v1", state, 1)

    assert [n["text"] for n in nodes if n["type"] == "subsection"] == ["the accused", "old words"]


def test_a_lent_piece_is_drawn_as_such_and_left_out_of_accepting_a_page(two_versions, monkeypatch):
    unit = _unit("1")["unit_no"]
    lent = [i for i in review._units[unit]]
    assert {review._piece_status(i) for i in lent} == {"inherited"}

    class _Doc:
        page_count = 1
    monkeypatch.setattr(review, "_get_pdf_doc", lambda: _Doc())
    monkeypatch.setattr(review, "_indices_on_page", lambda page: lent)
    with pytest.raises(Exception, match="already been accepted"):
        accept_page(1, AcceptRequest(flagged=False))


def test_a_document_in_one_version_has_no_version_bar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _parse(tmp_path, "act-v1", 1, [("1", "words")])
    review._load_state("act-v1")

    assert review.get_meta()["version_info"] is None
    assert _unit("1")["status"] == "pending"


def test_a_moved_provision_can_be_recorded_as_carried_from_the_version_before(tmp_path, monkeypatch):
    from corpus.review.review import CarriedFromRequest, carried_from_endpoint

    monkeypatch.chdir(tmp_path)
    _parse(tmp_path, "act-v1", 1, [("1", "a"), ("2", "moved words")])
    _parse(tmp_path, "act-v2", 2, [("1", "a"), ("3", "moved words")])
    review._load_state("act-v2")
    unit = _unit("3")["unit_no"]
    candidates = review.get_unit(unit)["lineage"]["carry_candidates"]
    assert [c["label"] for c in candidates] == ["Section 2"]

    payload = carried_from_endpoint(unit, CarriedFromRequest(key=candidates[0]["key"]))

    assert payload["lineage"]["carried_from"] == candidates[0]["key"]
    assert db.load_provision_links("act-v2") == {("provision", None, "3"): ("provision", None, "2")}


# ---------------------------------------------------------------------
# A changed section is shown by its changed pieces, with the Act's own
# account of the change beside them.
# ---------------------------------------------------------------------

def _two_subsections(tmp_path, slug, version, first, note=None, heading="Appeals"):
    nodes = [{"type": "part", "number": "1", "heading": "Preliminary", "text": ""},
             {"type": "section", "number": "2", "heading": heading, "text": ""},
             {"type": "subsection", "number": "1", "heading": None, "text": first},
             {"type": "subsection", "number": "2", "heading": None, "text": "the same in both"}]
    if note:
        nodes[2]["history"] = [{"raw": note}]
    # Where each version prints (1), for the page set beside this one.
    nodes[2]["rects"] = [{"page": 2, "x0": 50.0, "y0": 60.0 + version, "x1": 150.0, "y1": 80.0}]
    nodes[3]["rects"] = [{"page": 2, "x0": 50.0, "y0": 90.0, "x1": 150.0, "y1": 110.0}]
    annotate_ids(nodes, HIERARCHY)
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    (parsed / f"{slug}.json").write_text(json.dumps({
        "nodes": nodes, "hierarchy": HIERARCHY, "fingerprint": f"fp-{slug}",
        "version": {"version": version, "as_at_printed": f"{version} May 2026"},
        "endnotes": {"amending_acts": [{
            "title": "Appeals Amendment Act 2026", "citation": "7/2026", "act_no": "7", "year": "2026",
            "fields": {"assent_date": "1.2.26", "commencement_date": "S. 3 on 1.4.26"}}]},
    }))


def _changed_section(tmp_path, monkeypatch, note):
    monkeypatch.chdir(tmp_path)
    _two_subsections(tmp_path, "act-v1", 1, "a person may appeal")
    _two_subsections(tmp_path, "act-v2", 2, "a person may appeal within 28 days", note)
    review._load_state("act-v1")
    return review.get_unit(_unit("2")["unit_no"])


def test_only_the_changed_piece_is_put_in_front_of_the_reviewer(tmp_path, monkeypatch):
    unit = _changed_section(tmp_path, monkeypatch, "S. 2(1) amended by No. 7/2026 s. 3.")
    lineage = unit["lineage"]

    [change] = lineage["changes"]
    assert change["label"] == "(1)"
    assert '<ins class="d-ins">within 28 days</ins>' in change["html"]
    assert "the same in both" not in change["html"]
    [piece] = [p for p in unit["pieces"] if p["node_index"] in lineage["changed_pieces"]]
    assert piece["text"] == "a person may appeal"


def test_the_margin_note_comes_with_its_endnote(tmp_path, monkeypatch):
    lineage = _changed_section(tmp_path, monkeypatch, "S. 2(1) amended by No. 7/2026 s. 3.")["lineage"]

    [note] = lineage["notes"]
    assert note["raw"] == "S. 2(1) amended by No. 7/2026 s. 3." and note["version"] == 2
    [act] = note["acts"]
    assert act["title"] == "Appeals Amendment Act 2026" and act["assent_date"] == "1.2.26"
    assert "S. 3 on 1.4.26" in act["described"]
    assert lineage["evidenced"] is True


def test_a_difference_no_note_accounts_for_is_called_the_parsers(tmp_path, monkeypatch):
    lineage = _changed_section(tmp_path, monkeypatch, None)["lineage"]

    assert lineage["notes"] == [] and lineage["evidenced"] is False


def test_both_versions_can_be_re_parsed_from_the_review(tmp_path, monkeypatch):
    lineage = _changed_section(tmp_path, monkeypatch, None)["lineage"]

    assert lineage["slugs"] == {"this": "act-v1", "reference": "act-v2"}


def test_the_review_links_reach_the_dashboards_re_parse_menu():
    """Older reprints have no card on the dashboard, so this link is the
    only way to their re-parse menu."""
    from corpus import PROJECT_ROOT

    review_page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")
    dashboard = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert 'href="../../?reparse=${encodeURIComponent(slug)}&mode=keep"' in review_page
    assert "loadActs().then(openRequestedReparse);" in dashboard
    assert 'params.get("reparse")' in dashboard
    assert 'openParseMenu(s.work, [slug], params.get("mode"))' in dashboard


def test_the_same_words_waiting_for_review_are_not_a_change(tmp_path, monkeypatch):
    """The current version's queue is everything nobody has reviewed, most
    of it worded exactly as the version before. Each was headed "Differs
    from Version 1" with a comparison showing nothing."""
    monkeypatch.chdir(tmp_path)
    _two_subsections(tmp_path, "act-v1", 1, "a person may appeal")
    _two_subsections(tmp_path, "act-v2", 2, "a person may appeal")
    review._load_state("act-v2")

    lineage = review.get_unit(_unit("2")["unit_no"])["lineage"]

    assert lineage["status"] == "to_review" and lineage["changes"] == []


def test_each_side_of_a_change_reads_as_its_own_version(tmp_path, monkeypatch):
    [change] = _changed_section(tmp_path, monkeypatch, None)["lineage"]["changes"]

    assert (change["older"], change["newer"]) == (1, 2)
    assert "within 28 days" not in change["old_html"]
    assert '<ins class="d-ins">within 28 days</ins>' in change["new_html"]
    assert "<ins" not in change["old_html"] and "<del" not in change["new_html"]


def test_a_changed_heading_is_shown_on_the_section_itself(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _two_subsections(tmp_path, "act-v1", 1, "a person may appeal", heading="Appeals")
    _two_subsections(tmp_path, "act-v2", 2, "a person may appeal", heading="Appeals and reviews")
    review._load_state("act-v1")
    unit = review.get_unit(_unit("2")["unit_no"])

    [change] = unit["lineage"]["changes"]
    assert change["label"] == "Heading"
    assert change["node_index"] == unit["pieces"][0]["node_index"]
    assert '<ins class="d-ins">and reviews</ins>' in change["new_html"]


def test_the_two_wordings_sit_side_by_side_only_in_the_changes_view():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")

    assert "Differs from <strong>" not in page
    assert 'if (sidebarFilter === "changes" && l && l.changes)' in page
    assert "compareColumnsHtml(c, c.label === \"Heading\")" in page


def test_the_other_versions_page_opens_on_what_changed_there(tmp_path, monkeypatch):
    view = _changed_section(tmp_path, monkeypatch, None)["lineage"]["reference_view"]

    assert view["version"] == 2 and view["page"] == 2
    assert view["rects"] == [{"page": 2, "x0": 50.0, "y0": 62.0, "x1": 150.0, "y1": 80.0}], \
        "v2's (1), the piece that changed -- not (2), which didn't"


def test_the_other_versions_pdf_is_served_by_this_review(tmp_path, monkeypatch):
    import fitz

    _changed_section(tmp_path, monkeypatch, None)
    pdf = tmp_path / "v2.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=300)
    doc.new_page(width=210, height=310)
    doc.save(pdf)
    parsed = tmp_path / "data" / "parsed" / "act-v2.json"
    parsed.write_text(json.dumps({**json.loads(parsed.read_text()), "source": str(pdf)}))
    review._version_docs.clear()

    assert review.get_version_page(2, 2) == {"width": 210.0, "height": 310.0, "page_count": 2}
    image = review.get_version_page_image(2, 2)
    assert image.media_type == "image/png" and image.body[:4] == b"\x89PNG"


def test_the_pdf_panel_can_set_the_two_pages_side_by_side():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")

    assert 'id="pdf-compare-btn"' in page and 'id="pdf-ref"' in page
    assert "api/versions/${v}/pages/${pageNo}.png" in page
    assert 'classList.toggle("later", view.version > META.version_info.version)' in page


def test_one_parse_menu_per_act_parses_each_ticked_version_in_turn():
    """The card shows an Act's newest version only, and its Re-parse
    button reached no other: an older reprint could only be re-parsed
    through a link from its review."""
    from corpus import PROJECT_ROOT

    dashboard = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert "openParseMenu('${s.work}')\">Parse Menu</button>" in dashboard
    assert 'id="parse-versions"' in dashboard and "Re-parse\u2026" not in dashboard
    assert "for (const [n, v] of ticked.entries())" in dashboard
    assert "await fetch(`api/acts/${v.slug}/reparse`" in dashboard
    # One PDF's page numbers mean nothing in another's.
    assert 'getElementById("parse-pages").style.display = ticked.length === 1' in dashboard


def test_the_current_version_does_not_announce_what_is_reviewed():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")

    assert "Reviewed in this version." not in page
    assert 'if (l.status === "reviewed" || (current && l.status === "inherits"))' in page


# ---------------------------------------------------------------------
# The amending Act's own instruction beside the change it made
# ---------------------------------------------------------------------

def _instruction(tmp_path, **fields):
    ins = {"act": "7/2026", "provision": "s. 3", "target_act": "Appeals Act 2020", "schedule": None,
           "section": "2", "path": ["1"], "heading": False, "definition": None, "action": "insert_after",
           "old": "appeal", "new": "within 28 days", "number": None,
           "raw": 'In section 2(1) of the Appeals Act 2020, after "appeal" insert "within 28 days".', **fields}
    folder = tmp_path / "data" / "amending"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "2026-7.json").write_text(json.dumps([ins]))


def _with_instructions(tmp_path, monkeypatch, **fields):
    monkeypatch.chdir(tmp_path)
    _instruction(tmp_path, **fields)
    _two_subsections(tmp_path, "act-v1", 1, "a person may appeal")
    _two_subsections(tmp_path, "act-v2", 2, "a person may appeal within 28 days",
                     "S. 2(1) amended by No. 7/2026 s. 3.")
    review._load_state("act-v1")
    # No source PDF here to read the Act's title from.
    monkeypatch.setattr(review, "_act_title", "Appeals Act 2020")
    return review.get_unit(_unit("2")["unit_no"])


def test_the_act_that_made_a_change_is_shown_on_the_piece_it_made_it_to(tmp_path, monkeypatch):
    unit = _with_instructions(tmp_path, monkeypatch)

    [ins] = unit["lineage"]["instructions"]
    piece = next(p for p in unit["pieces"] if p["text"] == "a person may appeal")
    assert (ins["act"], ins["provision"], ins["status"], ins["target"]) == ("7/2026", "s. 3", "matched", "s 2(1)")
    assert ins["node_index"] == piece["node_index"], "this version's own piece, though the change is v2's"
    assert unit["lineage"]["unexplained"] == []


def test_a_change_no_fetched_act_made_is_listed_as_unexplained(tmp_path, monkeypatch):
    unit = _with_instructions(tmp_path, monkeypatch, old="hearing", new="and review")

    [ins] = unit["lineage"]["instructions"]
    piece = next(p for p in unit["pieces"] if p["text"] == "a person may appeal")
    assert ins["status"] == "not found"
    assert unit["lineage"]["unexplained"] == [piece["node_index"]]


def test_without_fetched_acts_the_margin_notes_are_all_there_is(tmp_path, monkeypatch):
    lineage = _changed_section(tmp_path, monkeypatch, "S. 2(1) amended by No. 7/2026 s. 3.")["lineage"]

    assert "instructions" not in lineage and lineage["evidenced"] is True


def test_the_dashboard_fetches_amending_acts_for_a_work(tmp_path, monkeypatch):
    import corpus.amending.fetch as fetch_module
    import corpus.web.dashboard as dashboard

    ran, killed = [], []
    monkeypatch.setattr(dashboard, "discover_slugs", lambda: ["act-v1", "act-v2", "other-act"])
    monkeypatch.setattr(dashboard, "_kill_review_process", killed.append)
    monkeypatch.setattr(fetch_module, "fetch",
                        lambda slug, base, log: (ran.append(slug), log("7/2026: fetched"), {"7/2026": {}})[2])

    result = dashboard.fetch_amending_acts("act")

    assert ran == ["act-v2"], "from the newest version, which sees every reprint held"
    assert result["ok"] and "7/2026: fetched" in result["log"]
    assert killed == ["act-v1", "act-v2"], "open reviews read the instructions once, when they start"


def test_the_pages_show_the_acts_and_fetch_them():
    from corpus import PROJECT_ROOT

    review_page = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")
    dashboard = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")

    assert "made.map(instructionHtml)" in review_page and "No amending Act made this change" in review_page
    assert 'onclick="fetchAmendingActs()"' in dashboard and "/amending`, { method: \"POST\" }" in dashboard
