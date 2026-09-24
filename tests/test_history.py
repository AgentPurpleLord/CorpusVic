"""History review (corpus/history): the changes between versions, and
nothing in the public histories until a person has confirmed it."""
import json

from corpus.history.changes import HEADING, WHOLE, confirmed, gate, key_json, work_changes
from corpus.parsing.identity import annotate_ids

HIERARCHY = ["part", "section", "subsection", "paragraph"]
S2 = ("provision", None, "2")


def _nodes(sections: dict, heading="Appeals") -> list[dict]:
    nodes = [{"type": "part", "number": "1", "heading": "Preliminary", "text": ""}]
    for number, pieces in sections.items():
        nodes.append({"type": "section", "number": number, "heading": heading, "text": ""})
        for sub, text in pieces:
            nodes.append({"type": "subsection", "number": sub, "heading": None, "text": text})
    annotate_ids(nodes, HIERARCHY)
    return nodes


def test_each_changed_piece_is_its_own_change():
    changes = work_changes([
        (1, _nodes({"2": [("1", "a person may appeal"), ("2", "the same")]}), HIERARCHY),
        (2, _nodes({"2": [("1", "a person may appeal within 28 days"), ("2", "the same")]}), HIERARCHY),
    ])

    [change] = changes
    assert (change["key"], change["from"], change["to"], change["piece"], change["label"]) == (S2, 1, 2, "1", "(1)")
    assert '<ins class="d-ins">within 28 days</ins>' in change["new_html"]


def test_a_heading_and_a_whole_section_are_changes_too():
    changes = work_changes([
        (1, _nodes({"2": [("1", "x")]}, heading="Appeals"), HIERARCHY),
        (2, _nodes({"2": [("1", "x")], "3": [("1", "new")]}, heading="Appeals and reviews"), HIERARCHY),
    ])

    assert {(c["key"][2], c["piece"], c["op"]) for c in changes} == {("2", HEADING, "changed"), ("3", WHOLE, "insert")}


def _repealed_after_2() -> list[dict]:
    """Section 3 repealed: its row of stars printed after section 2, where
    attach_history numbers it and hangs its repeal note on it."""
    nodes = _nodes({"2": [("1", "x")]})
    nodes.append({"type": "repealed", "number": "3", "heading": None, "text": "* * * * *",
                  "history": [{"raw": "S. 3 repealed by No. 31/2024 s. 11.", "section": "3", "sub_path": []}]})
    annotate_ids(nodes, HIERARCHY)
    return nodes


def test_a_section_repealed_to_a_row_of_stars_is_one_repeal():
    """It read as section 3 removed and a piece added to section 2."""
    changes = work_changes([
        (1, _nodes({"2": [("1", "x")], "3": [("1", "gone soon")]}), HIERARCHY),
        (2, _repealed_after_2(), HIERARCHY),
    ])

    [change] = changes
    assert (change["key"][2], change["piece"], change["op"]) == ("3", WHOLE, "repeal")
    assert "gone soon" in change["old_html"] and "* * * * *" in change["new_html"]


def test_a_subsection_s_row_is_still_a_piece_of_its_section():
    """Only a row carrying its own section's note stands for a section."""
    nodes = _nodes({"2": [("1", "x")]})
    nodes.append({"type": "repealed", "number": "2", "heading": None, "text": "* * * * *",
                  "history": [{"raw": "S. 2(2) repealed by No. 31/2024 s. 11.", "section": "2", "sub_path": ["(2)"]}]})
    annotate_ids(nodes, HIERARCHY)

    changes = work_changes([(1, _nodes({"2": [("1", "x"), ("2", "y")]}), HIERARCHY), (2, nodes, HIERARCHY)])

    assert changes and all(c["key"][2] == "2" and c["op"] != "repeal" for c in changes)


def test_only_confirmed_decisions_count():
    decisions = {(key_json(S2), 1, 2, "1"): "confirmed", (key_json(S2), 1, 2, "2"): "denied",
                 (key_json(("provision", None, "3")), 1, 2, WHOLE): "confirmed"}

    assert confirmed(decisions) == {2: {"text": {S2}, "whole": {("provision", None, "3")}}}


def _wording(versions, absent=False, key=S2):
    w = {"absent": absent, "versions": versions, "from": {"version": versions[0]}, "to": {"version": versions[-1]}}
    if not absent:
        w.update(key=key, provision={"heading": "x"})
    return w


def test_an_unconfirmed_arrival_or_repeal_is_not_in_the_history():
    chain = {"wordings": [_wording([1], absent=True), {**_wording([2, 3]), "ended_by": {"version": 4}},
                          _wording([4], absent=True)]}

    assert [w["versions"] for w in gate(chain, {})["wordings"]] == [[2, 3]]
    assert [w["versions"] for w in gate(chain, {2: {S2}, 4: {S2}})["wordings"]] == [[1], [2, 3], [4]]
    assert "ended_by" in chain["wordings"][1], "the cached chain is left alone"


def test_a_gap_the_parser_made_closes_up():
    """A provision missing from one reprint, back in the next: unless its
    going and coming back are confirmed, it was never gone."""
    chain = {"wordings": [_wording([1]), _wording([2], absent=True), _wording([3])]}

    [wording] = gate(chain, {})["wordings"]
    assert wording["versions"] == [1, 3] and wording["from"]["version"] == 1


def test_the_public_history_waits_for_confirmation(tmp_path, monkeypatch):
    import corpus.web.dashboard as dashboard
    from corpus.storage import db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True)
    for version, text in ((1, "a person may appeal"), (2, "a person may appeal within 28 days")):
        (parsed / f"act-v{version}.json").write_text(json.dumps({
            "nodes": _nodes({"2": [("1", text)]}), "hierarchy": HIERARCHY, "fingerprint": f"fp{version}",
            "version": {"version": version}}))

    def chains():
        dashboard._timeline_cache.clear()
        dashboard._lineage_cache.clear()
        return [c for c in dashboard._timeline("act")["chains"] if len(c["wordings"]) > 1]

    assert chains() == [], "found by the parser, not yet confirmed: not public"
    db.save_history_decision("act", key_json(S2), 1, 2, "1", "confirmed", tmp_path)
    [chain] = chains()
    assert [w["versions"] for w in chain["wordings"]] == [[1], [2]]


def test_history_review_lists_each_change_with_its_evidence_and_records_decisions(tmp_path, monkeypatch):
    import corpus.web.dashboard as dashboard
    from corpus.web.dashboard import HistoryDecision, history_decide, history_items

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_act_title", lambda slug: "Appeals Act 2020")
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True)
    for version, text, notes in ((1, "a person may appeal", []),
                                 (2, "a person may appeal within 28 days", ["S. 2(1) amended by No. 7/2026 s. 3."])):
        nodes = _nodes({"2": [("1", text)]})
        nodes[-1]["history"] = [{"raw": n} for n in notes]
        (parsed / f"act-v{version}.json").write_text(json.dumps({
            "nodes": nodes, "hierarchy": HIERARCHY, "fingerprint": f"fp{version}", "version": {"version": version},
            "endnotes": {"amending_acts": [{"title": "Appeals Amendment Act 2026", "citation": "7/2026",
                                             "act_no": "7", "year": "2026"}]}}))
    amending = tmp_path / "data" / "amending"
    amending.mkdir(parents=True)
    (amending / "2026-7.json").write_text(json.dumps([{
        "act": "7/2026", "provision": "s. 3", "target_act": "Appeals Act 2020", "schedule": None, "section": "2",
        "path": ["1"], "heading": False, "definition": None, "action": "insert_after", "old": "appeal",
        "new": "within 28 days", "number": None, "raw": 'In section 2(1), after "appeal" insert "within 28 days".'}]))

    [item] = history_items("act")["items"]
    assert (item["section"], item["label"], item["from"], item["to"], item["decision"]) == ("s 2", "(1)", 1, 2, None)
    assert [(a["act"], a["status"], a["here"]) for a in item["instructions"]] == [("7/2026", "matched", True)]
    assert item["notes"] == ["S. 2(1) amended by No. 7/2026 s. 3."]

    history_decide("act", HistoryDecision(provision=item["provision"], from_version=1, to_version=2,
                                          piece=item["piece"], decision="denied"))
    assert history_items("act")["items"][0]["decision"] == "denied"


def test_history_review_fetches_the_ticked_versions_one_at_a_time():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "history.html").read_text(encoding="utf-8")

    assert 'fetch(API + "/versions/available")' in page
    assert "for (const [n, { v, again }] of ticked.entries())" in page
    assert '`${API}/versions/fetch/${v}${again ? "?replace=true" : ""}`' in page, "a held version ticked is fetched again"
    assert 'while (job.state === "running")' in page, "a parse outlasts a request, so the job is asked after"
    assert "res.json()" not in page, "every reply read through jsonOf, which reports one that isn't JSON"


def test_an_instruction_found_under_another_piece_offers_to_put_it_right(tmp_path, monkeypatch):
    """The Act names (2); v2's parse has the change under (1)."""
    import corpus.web.dashboard as dashboard

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_act_title", lambda slug: "Appeals Act 2020")
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True)
    for version, text in ((1, "a person may appeal"), (2, "a person may appeal within 28 days")):
        nodes = _nodes({"2": [("1", text), ("2", "the same")]})
        nodes[-2]["history"] = [{"raw": "S. 2(1) amended by No. 7/2026 s. 3."}] if version == 2 else []
        (parsed / f"act-v{version}.json").write_text(json.dumps({
            "nodes": nodes, "hierarchy": HIERARCHY,
            "fingerprint": f"fp{version}", "version": {"version": version},
            "endnotes": {"amending_acts": [{"title": "Appeals Amendment Act 2026", "citation": "7/2026",
                                             "act_no": "7", "year": "2026"}]}}))
    amending = tmp_path / "data" / "amending"
    amending.mkdir(parents=True)
    (amending / "2026-7.json").write_text(json.dumps([{
        "act": "7/2026", "provision": "s. 3", "target_act": "Appeals Act 2020", "schedule": None, "section": "2",
        "path": ["2"], "heading": False, "definition": None, "action": "insert_after", "old": "appeal",
        "new": "within 28 days", "number": None, "raw": 'In section 2(2), after "appeal" insert "within 28 days".'}]))

    [item] = dashboard.history_items("act")["items"]
    [ins] = item["instructions"]
    assert (ins["status"], ins["place_as"]) == ("elsewhere", "(2)")
    assert ins["node_id"] == _nodes({"2": [("1", "x")]})[-1]["id"], "v2's (1), where the parse has the change"

    page = (dashboard.STATIC_DIR / "history.html").read_text(encoding="utf-8")
    assert "/api/named/${encodeURIComponent(btn.dataset.node)}/place" in page


def test_a_piece_with_no_boxes_is_found_on_its_page_by_its_words(tmp_path, monkeypatch):
    """The CPA's older parses predate boxes: its words mark where it is."""
    import pymupdf
    import corpus.web.dashboard as dashboard

    pdf = tmp_path / "v1.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((40, 100), "Something else entirely.", fontsize=10)
    page.insert_text((40, 200), "A person may appeal within 28 days.", fontsize=10)
    doc.save(pdf)
    monkeypatch.setattr(dashboard, "_find_source_pdf", lambda slug: pdf)
    dashboard._page_docs.clear()

    [rect] = dashboard.pdf_page_find("act-v1", 1, "A person may appeal within 28 days.")["rects"]
    assert rect["page"] == 1 and 185 < rect["y1"] < 205
    assert dashboard.pdf_page_find("act-v1", 1, "nothing printed here at all")["rects"] == []
    small, large = (dashboard.pdf_page_image("act-v1", 1, zoom=z).body for z in (1, 2))
    assert pymupdf.open("png", large)[0].rect.width == 2 * pymupdf.open("png", small)[0].rect.width


def test_an_accepted_piece_keeps_the_boxes_its_parse_drew(monkeypatch):
    import corpus.web.dashboard as dashboard

    box = {"page": 3, "x0": 1, "y0": 2, "x1": 3, "y1": 4}
    monkeypatch.setattr(dashboard, "_parse_field", lambda slug, key: [{"rects": []}, {"rects": [box]}])

    accepted, inserted = dashboard._with_rects("act-v1", [{"text": "x", "_source_node_index": 1}, {"text": "y"}])
    assert accepted["rects"] == [box] and "rects" not in inserted


def test_each_version_shows_its_pages_scrolled_to_the_piece():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "history.html").read_text(encoding="utf-8")

    assert 'showPdf("old", i.from, i.old_at);' in page and 'showPdf("new", i.to, i.new_at);' in page
    assert "/pages/${p}/find?q=" in page and "scroll.scrollTop =" in page
    assert "aspect-ratio: ${size.width} / ${size.height}" in page


def test_the_page_images_are_reached_through_the_router(tmp_path, monkeypatch):
    """The size route once took every ".png" request and refused it, so the
    panes drew blank pages; calling the functions directly never showed it."""
    import pymupdf
    from fastapi.testclient import TestClient
    import corpus.web.dashboard as dashboard

    pdf = tmp_path / "v1.pdf"
    doc = pymupdf.open()
    doc.new_page(width=300, height=400).insert_text((40, 200), "A person may appeal.", fontsize=10)
    doc.save(pdf)
    monkeypatch.setattr(dashboard, "_find_source_pdf", lambda slug: pdf)
    monkeypatch.setattr(dashboard, "_DASHBOARD_USERNAME", None)
    dashboard._page_docs.clear()
    client = TestClient(dashboard.app)

    assert client.get("/api/docs/act-v1/pages/1").json()["page_count"] == 1
    image = client.get("/api/docs/act-v1/pages/1.png?zoom=1.5")
    assert image.status_code == 200 and image.content[:4] == b"\x89PNG"
    assert client.get("/api/docs/act-v1/pages/1/find", params={"q": "A person may appeal."}).json()["rects"]


def test_every_act_can_fetch_versions_and_accepted_repeals_are_not_grey(tmp_path, monkeypatch):
    """An Act held under its plain name has no second version yet, which
    is exactly when fetching one is wanted."""
    import corpus.web.dashboard as dashboard
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")
    assert '${s.kind === "act" && s.parsed\n          ? `<a class="btn small" href="history/' in page
    assert 'href="history/${encodeURIComponent(s.work)}/#fetch"' in page
    history = (PROJECT_ROOT / "static" / "history.html").read_text(encoding="utf-8")
    assert 'location.hash === "#fetch"' in history

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_act_title", lambda slug: "Appeals Act 2020")
    (tmp_path / "data" / "parsed").mkdir(parents=True)
    (tmp_path / "data" / "parsed" / "appeals-act.json").write_text(json.dumps({
        "nodes": _nodes({"2": [("1", "x")]}), "hierarchy": HIERARCHY, "fingerprint": "fp"}))
    assert dashboard.history_items("appeals-act")["items"] == []

    review = (PROJECT_ROOT / "static" / "admin" / "review.css").read_text(encoding="utf-8")
    assert '.piece[data-type="repealed"]:not(.piece-accepted):not(.piece-flagged) { background: var(--repealed-bg); }' in review


def test_the_page_says_repealed():
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "history.html").read_text(encoding="utf-8")
    assert 'repeal: "repealed"' in page and "OP_WORDS[i.op]" in page
