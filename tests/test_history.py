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


def _two_versions(tmp_path, monkeypatch, *, noted_s3=False):
    """s 2(1) amended with its margin note; s 3(1) read differently with
    none -- the parser's, not Parliament's."""
    import corpus.web.dashboard as dashboard

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_act_title", lambda slug: "Appeals Act 2020")
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True, exist_ok=True)
    for version, s2, s3 in ((1, "a person may appeal", "the court may"), (2, "a person may appeal within 28 days", "the court rnay")):
        nodes = _nodes({"2": [("1", s2)], "3": [("1", s3)]})
        if version == 2:
            nodes[2]["history"] = [{"raw": "S. 2(1) amended by No. 7/2026 s. 3."}]
            if noted_s3:
                nodes[4]["history"] = [{"raw": "S. 3(1) amended by No. 7/2026 s. 4."}]
        (parsed / f"act-v{version}.json").write_text(json.dumps({
            "nodes": nodes, "hierarchy": HIERARCHY, "fingerprint": f"fp{version}", "version": {"version": version}}))
    return dashboard


def test_only_a_change_with_a_margin_note_is_up_for_review(tmp_path, monkeypatch):
    """The Act prints a note beside every official change of wording; a
    change without one is taken to be the parser reading the two versions
    differently."""
    dashboard = _two_versions(tmp_path, monkeypatch)

    items = dashboard.history_items("act")["items"]
    assert {(i["section"], i["noted"]) for i in items} == {("s 2", True), ("s 3", False)}


def test_each_step_is_worked_out_once_until_its_versions_change(tmp_path, monkeypatch):
    """A work with many versions recomputed every step on every load, and
    again after every restart."""
    from corpus.history import changes as history_changes

    dashboard = _two_versions(tmp_path, monkeypatch)
    calls = []
    real = history_changes.work_changes
    monkeypatch.setattr(history_changes, "work_changes", lambda versions: calls.append(1) or real(versions))

    dashboard.history_items("act")
    monkeypatch.setattr(dashboard, "_history_steps", {})   # a restart: only the file on disk is left
    dashboard.history_items("act")
    assert len(calls) == 1 and (tmp_path / "data" / ".cache" / "history-act.json").exists()

    _two_versions(tmp_path, monkeypatch, noted_s3=True)   # a re-parse of version 2
    import os
    os.utime(tmp_path / "data" / "parsed" / "act-v2.json", ns=(1, 10**18))
    assert all(i["noted"] for i in dashboard.history_items("act")["items"]) and len(calls) == 2


def test_every_version_not_held_is_fetched_in_one_job(tmp_path, monkeypatch):
    import corpus.web.dashboard as dashboard

    class Now:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(dashboard, "_held", lambda work: ["act-v110"])
    monkeypatch.setattr(dashboard, "_site_versions", lambda work: [
        {"version": v, "pdf_url": f"https://x/{v}.pdf" if v != 112 else None} for v in (109, 110, 111, 112, 113)])
    fetched = []
    monkeypatch.setattr(dashboard, "_fetch_version", lambda work, v: fetched.append(v) or {"ok": v != 113, "slug": f"act-v{v}"})
    monkeypatch.setattr(dashboard.threading, "Thread", Now)

    job = dashboard.fetch_all_work_versions("act")

    assert fetched == [109, 111, 113], "oldest first, skipping the held one and the one with no single PDF"
    assert job["state"] == "done" and [d["version"] for d in job["done"]] == [109, 111]
    assert [f["version"] for f in job["failed"]] == [113]


def test_keep_versions_slim_is_in_the_header_not_the_fetch_panel():
    """It was drawn inside the Fetch versions panel, which is only built
    once legislation.vic.gov.au answers -- so it read as missing."""
    from corpus import PROJECT_ROOT

    page = (PROJECT_ROOT / "static" / "history.html").read_text(encoding="utf-8")
    header = page[page.index("<header"):page.index("</header>")]
    assert 'id="slim-all"' in header
    assert page.count('id="slim-all"') == 1, "and only there"
    assert "#slim" in (PROJECT_ROOT / "static" / "dashboard.html").read_text(encoding="utf-8")


def test_history_says_which_version_is_the_base_and_which_are_slim(tmp_path, monkeypatch):
    dashboard = _two_versions(tmp_path, monkeypatch)
    import json
    path = tmp_path / "data" / "parsed" / "act-v1.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "slim": {"toward": "act-v2", "pieces": [], "notes": {}}}))

    body = dashboard.history_items("act")
    assert body["slim"] == [1] and body["base"] in (1, 2)


def test_a_step_that_fails_is_named_and_the_rest_still_load(tmp_path, monkeypatch):
    """One unreadable version must not take the work's whole review down,
    and what went wrong is said on the page, not as a bare 500."""
    from corpus import PROJECT_ROOT

    dashboard = _two_versions(tmp_path, monkeypatch)
    (tmp_path / "data" / "parsed" / "act-v3.json").write_text(json.dumps({
        "nodes": _nodes({"2": [("1", "a person may appeal within 28 days")]}), "hierarchy": HIERARCHY,
        "fingerprint": "fp3", "version": {"version": 3}}))
    real = dashboard._history_step

    def step(older, newer, acts):
        if newer[0] == 3:
            raise KeyError("nodes")
        return real(older, newer, acts)

    monkeypatch.setattr(dashboard, "_history_step", step)
    data = dashboard.history_items("act")
    assert {(i["from"], i["to"]) for i in data["items"]} == {(1, 2)}
    assert data["errors"] == [{"from": 2, "to": 3, "error": "KeyError: 'nodes'"}]
    page = (PROJECT_ROOT / "static" / "history.html").read_text(encoding="utf-8")
    assert "DATA.errors" in page


def test_an_unexpected_error_says_what_it_was(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    dashboard = _two_versions(tmp_path, monkeypatch)
    monkeypatch.setattr(dashboard, "_history_items", lambda work, errors=None: 1 / 0)
    res = TestClient(dashboard.app, raise_server_exceptions=False).get("/api/works/act/history")
    assert res.status_code == 500
    assert res.json() == {"detail": "ZeroDivisionError: division by zero"}


def _six_versions(tmp_path, monkeypatch):
    """v6 held whole, v1-v5 slim: each amends s 1(2) again."""
    import corpus.web.dashboard as dashboard
    from corpus.history import slim
    from corpus.storage import db, parsed
    from test_delta import HIERARCHY as H, _act

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_act_title", lambda slug: "Appeals Act 2020")
    for cache in ("_current_nodes_cache", "_lineage_cache", "_instructions_cache", "_history_steps"):
        monkeypatch.setattr(dashboard, cache, {})
    monkeypatch.setattr(parsed, "_loaded", {})
    for v in range(1, 7):
        acts = ", ".join(f"{k}/2020 s. 1" for k in range(1, v + 1))
        parse = _act({"1": [("1", "a"), ("2", f"b as amended {v} times")], "2": [("1", "c")]},
                     {("1", "2"): [f"S. 1(2) amended by Nos {acts}."]})
        parse["version"] = {"version": v}
        (tmp_path / "data" / "parsed").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "parsed" / f"act-v{v}.json").write_text(json.dumps({**parse, "hierarchy": H}))
    db.save_verified("act-v6", [{**n, "_node_id": n["id"], "verified_at": "2026-01-01"}
                                for n in json.loads((tmp_path / "data" / "parsed" / "act-v6.json").read_text())["nodes"]],
                     tmp_path)
    slim.apply("act", tmp_path, base=6)
    return dashboard


def test_every_version_is_put_back_together_about_once(tmp_path, monkeypatch):
    """A slim version is built from its neighbour toward the base. Loaded
    without keeping any, a work of a hundred versions rebuilt each whole
    chain again for every version, and History review took minutes."""
    from corpus.amending import load as amending_load
    from corpus.history import delta

    dashboard = _six_versions(tmp_path, monkeypatch)
    built = []
    real = delta.assemble
    monkeypatch.setattr(delta, "assemble", lambda *a: built.append(1) or real(*a))

    items = dashboard.history_items("act")["items"]
    assert {(i["from"], i["to"]) for i in items} == {(v, v + 1) for v in range(1, 6)}
    assert len(built) <= 10, f"{len(built)} rebuilds of 5 slim versions"

    # A review edit in the base: only the step touching it again, and not
    # every version read again for the amending Acts.
    scoped = []
    monkeypatch.setattr(amending_load, "acts_between", lambda *a: scoped.append(1) or [])
    from corpus.storage import db
    rows = db.load_verified("act-v6", tmp_path)
    db.save_verified("act-v6", [{**rows[0], "text": "edited"}, *rows[1:]], tmp_path)
    built.clear()
    steps = []
    real_step = dashboard._history_step
    monkeypatch.setattr(dashboard, "_history_step", lambda o, n, a: steps.append((o[0], n[0])) or real_step(o, n, a))
    dashboard.history_items("act")
    assert steps == [(5, 6)] and not scoped


def _printed(nodes: list[dict], skip=()) -> list[dict]:
    """Each node on a page of its own, bar the numbers in `skip`, which
    the parse drew no box round."""
    for k, node in enumerate(nodes):
        if node.get("number") not in skip:
            node["rects"] = [{"page": 10 + k, "x0": 0, "y0": 0, "x1": 1, "y1": 1}]
    return nodes


def test_every_change_is_set_beside_both_versions_pages():
    """A change is judged with both printed versions beside it. A piece
    one version hasn't got is shown where it would be, marked so; one the
    parse drew no box round, on its neighbour's page, found by its words."""
    older = _printed(_nodes({"2": [("1", "a person may appeal")]}))
    newer = _printed(_nodes({"2": [("1", "a person may appeal"), ("2", "within 28 days")], "3": [("1", "new")]}))
    changes = {(c["key"][2], c["piece"]): c for c in work_changes([(1, older, HIERARCHY), (2, newer, HIERARCHY)])}

    inserted = changes[("2", "2")]
    assert inserted["old_at"] == {**inserted["old_at"], "page": older[2]["rects"][0]["page"], "absent": True}
    assert inserted["old_at"]["rects"] == [] and inserted["new_at"]["page"] == newer[3]["rects"][0]["page"]
    section = changes[("3", WHOLE)]
    assert section["old_at"]["page"] == older[2]["rects"][0]["page"] and section["old_at"]["absent"], \
        "a section inserted: where it would follow, the end of the one before it"

    unboxed = _printed(_nodes({"2": [("1", "a person may appeal"), ("2", "within 21 days")]}), skip={"2"})
    boxed = _printed(_nodes({"2": [("1", "a person may appeal"), ("2", "within 28 days")]}))
    [change] = work_changes([(1, unboxed, HIERARCHY), (2, boxed, HIERARCHY)])
    assert change["old_at"]["page"] == unboxed[2]["rects"][0]["page"]
    assert change["old_at"]["text"].startswith("within 21 days") and not change["old_at"].get("absent")


def test_a_slim_version_keeps_its_own_pages_under_its_neighbours_reviewed_words(tmp_path, monkeypatch):
    """A provision a slim version borrows from its neighbour, reviewed,
    is still printed where this version prints it: a piece kept for its
    pages was kept for exactly that. And not with the neighbour's source
    index, which read against this version's parse drew another node."""
    import corpus.web.dashboard as dashboard
    from corpus.history import delta
    from corpus.storage import db, parsed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    for cache in ("_current_nodes_cache", "_lineage_cache"):
        monkeypatch.setattr(dashboard, cache, {})
    monkeypatch.setattr(parsed, "_loaded", {})
    folder = tmp_path / "data" / "parsed"
    folder.mkdir(parents=True)
    v2 = _nodes({"2": [("1", "a person may appeal")], "3": [("1", "c")]})
    (folder / "act-v2.json").write_text(json.dumps({"nodes": v2, "hierarchy": HIERARCHY, "fingerprint": "f2"}))
    db.save_verified("act-v2", [{**n, "_node_id": n["id"], "verified_at": "2026-01-01"} for n in v2], tmp_path)
    v1 = _printed(_nodes({"2": [("1", "a person may appeal")], "3": [("1", "c")]}))
    (folder / "act-v1.json").write_text(json.dumps(delta.slim(
        {"nodes": v1, "hierarchy": HIERARCHY, "fingerprint": "f1"}, "act-v2", text=[], pages_only=["pt1/s2"])))

    assert "_source_node_index" in dashboard._current_nodes("act-v2")[0][2], "the neighbour's own index"
    nodes = {n["id"]: n for n in dashboard._current_nodes("act-v1")[0]}
    assert nodes["pt1/s2/1"]["rects"] == v1[2]["rects"] and "_source_node_index" not in nodes["pt1/s2/1"]
    assert nodes["pt1/s3/1"]["rects"] == [], "not kept: this version's page for it is gone"


def test_a_slim_version_missing_pages_it_now_needs_is_fetched_again(monkeypatch):
    """Cut down by a narrower rule, a version let go of pages History
    review now sets beside a change. It can always be fetched again."""
    import corpus.web.dashboard as dashboard
    from corpus.history import slim

    calls = []

    def apply(work, base_dir=None, pdf_for=None, dry_run=False, base=None):
        calls.append("dry run" if dry_run else "slim")
        return {"versions": {3: {"missing": ["s2"]}, 4: {"missing": []}}}

    monkeypatch.setattr(slim, "apply", apply)
    monkeypatch.setattr(dashboard, "_fetch_version",
                        lambda work, v, replace=False, then_slim=True: calls.append((v, replace, then_slim)) or {"ok": True})
    report = dashboard._slim_work("act")

    assert calls == ["dry run", (3, True, False), "slim"]
    assert report["fetched_again"] == [3] and report["could_not_fetch"] == []


def test_the_report_downloads_as_markdown(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    dashboard = _two_versions(tmp_path, monkeypatch)
    res = TestClient(dashboard.app).get("/api/works/act/report.md")

    assert res.status_code == 200 and res.headers["content-type"].startswith("text/markdown")
    assert 'attachment; filename="act-report-' in res.headers["content-disposition"]
    assert res.text.startswith("# Review report: act")
