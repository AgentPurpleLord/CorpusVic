"""A version kept as only what its margin notes say changed
(corpus/history/delta.py): which pieces a step changed, and a slim
version put back together from its neighbour."""
from corpus.history import delta
from corpus.parsing.identity import annotate_ids

from conftest import make_node

HIERARCHY = ["schedule", "chapter", "part", "division", "subdivision", "section",
             "subsection", "paragraph", "subparagraph", "sub_subparagraph"]


def _act(sections: dict, notes: "dict | None" = None, loose=()) -> dict:
    """{section number: [(subsection, text)]}, with margin notes by
    (section, subsection) -- None for the section itself."""
    nodes = []
    for number, subs in sections.items():
        nodes.append(make_node("section", number, f"Section {number}", ""))
        for sub, text in subs:
            nodes.append(make_node("subsection", sub, None, text))
    annotate_ids(nodes, HIERARCHY)
    for node in nodes:
        key = (node["number"], None) if node["type"] == "section" else (node["id"].split("/")[0][1:], node["number"])
        if notes and key in notes:
            node["history"] = [{"raw": r} for r in notes[key]]
        node["rects"] = [{"page": 1 + len(nodes) // 50, "x0": 0, "y0": 0, "x1": 1, "y1": 1}]
    return {"nodes": nodes, "unattached_notes": [{"raw": r, "section": s} for s, r in loose],
            "version": {"version": 1}, "fingerprint": "fp"}


OLD = _act({"1": [("1", "a"), ("2", "b")], "2": [("1", "c")], "3": [("1", "gone soon")]},
           {("1", "2"): ["S. 1(2) amended by No. 5/2018 s. 3."]})
NEW = _act({"1": [("1", "a"), ("2", "b as amended"), ("3", "a new one")], "2": [("1", "c, misread")]},
           {("1", "2"): ["S. 1(2) amended by Nos 5/2018 s. 3, 7/2026 s. 4."],
            ("1", "3"): ["S. 1(3) inserted by No. 7/2026 s. 5."]},
           loose=[("3", "S. 3 repealed by No. 7/2026 s. 6.")])


def test_a_step_changed_what_its_notes_newly_cite():
    changed = delta.changed_pieces(delta.notes_index(OLD["nodes"]), delta.notes_index(NEW["nodes"], NEW["unattached_notes"]))

    assert changed == {"s1/2": "changed", "s1/3": "inserted", "s3": "repealed"}, \
        "s 2(1) reads differently but no note says so: the parser's"


def test_a_slim_version_and_its_neighbour_make_the_whole():
    changed = delta.changed_pieces(delta.notes_index(OLD["nodes"]), delta.notes_index(NEW["nodes"], NEW["unattached_notes"]))
    kept = delta.slim(NEW, toward="act-v1", text=list(changed))

    assert "nodes" not in kept and [p["name"] for p in kept["slim"]["pieces"]] == ["s1/2", "s1/3"]
    assert kept["slim"]["removed"] == ["s3"]
    whole = delta.assemble(OLD["nodes"], kept["slim"])
    assert [(n["type"], n["number"], n["text"]) for n in whole] == [
        ("section", "1", ""), ("subsection", "1", "a"), ("subsection", "2", "b as amended"),
        ("subsection", "3", "a new one"), ("section", "2", ""), ("subsection", "1", "c")], \
        "the neighbour's words wherever no note says otherwise"
    assert whole[2]["rects"] and not whole[1]["rects"], "only its own pieces keep this reprint's boxes"


def test_a_piece_kept_for_its_pages_lends_them_to_the_neighbours_words():
    kept = delta.slim(NEW, toward="act-v1", text=[], pages_only=["s1/2"])

    [piece] = delta.assemble(OLD["nodes"], kept["slim"])[2:3]
    assert piece["text"] == "b" and piece["rects"] == NEW["nodes"][2]["rects"]


def test_discarded_pages_are_blank_and_keep_their_numbers(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for n in range(5):
        doc.new_page().insert_text((72, 72), f"page {n + 1}")
    doc.save(tmp_path / "act.pdf")

    delta.blank_pages(tmp_path / "act.pdf", [4], tmp_path / "slim.pdf")

    # The front matter stays: it is where the PDF says which version it is.
    slim = pymupdf.open(tmp_path / "slim.pdf")
    assert [p.get_text().strip() for p in slim] == ["page 1", "page 2", "", "page 4", ""]


def _write(tmp_path, slug, parse):
    import json

    path = tmp_path / "data" / "parsed" / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parse))
    return path


def test_a_slim_version_reads_whole_wherever_it_is_read(tmp_path, monkeypatch):
    """Built backward from the base: v2 is held whole, v1 keeps only what
    changed between them (its own older words), v0 only what changed
    between it and v1."""
    from corpus.review import review
    from corpus.storage import parsed

    monkeypatch.chdir(tmp_path)
    v0 = _act({"1": [("1", "a"), ("2", "b, first")]}, {("1", "2"): ["S. 1(2) amended by No. 5/2018 s. 3."]})
    v1 = _act({"1": [("1", "a"), ("2", "b")]}, {("1", "2"): ["S. 1(2) amended by Nos 5/2018 s. 3, 6/2020 s. 1."]})
    v2 = _act({"1": [("1", "a"), ("2", "b as amended"), ("3", "a new one")]},
              {("1", "2"): ["S. 1(2) amended by Nos 5/2018 s. 3, 6/2020 s. 1, 7/2026 s. 4."],
               ("1", "3"): ["S. 1(3) inserted by No. 7/2026 s. 5."]})
    _write(tmp_path, "act-v2", {**v2, "hierarchy": HIERARCHY})
    idx = {v: delta.notes_index(p["nodes"]) for v, p in ((0, v0), (1, v1), (2, v2))}
    _write(tmp_path, "act-v1", delta.slim({**v1, "hierarchy": HIERARCHY}, "act-v2",
                                          text=list(delta.changed_pieces(idx[1], idx[2]))))
    _write(tmp_path, "act-v0", delta.slim({**v0, "hierarchy": HIERARCHY}, "act-v1",
                                          text=list(delta.changed_pieces(idx[0], idx[1]))))

    for slug, want in (("act-v1", ["a", "b"]), ("act-v0", ["a", "b, first"])):
        nodes = review.load_parsed(slug)[0]
        assert [n["text"] for n in nodes if n["type"] == "subsection"] == want, slug
    assert len(parsed.chain(tmp_path / "data" / "parsed" / "act-v0.json")) == 3

    before = parsed.stamp(tmp_path / "data" / "parsed" / "act-v0.json")
    _write(tmp_path, "act-v2", {**v2, "hierarchy": HIERARCHY, "fingerprint": "re-parsed"})
    import os
    os.utime(tmp_path / "data" / "parsed" / "act-v2.json", ns=(1, 10**18))
    assert parsed.stamp(tmp_path / "data" / "parsed" / "act-v0.json") != before, "the base's re-parse reaches it"


def test_a_work_is_slimmed_to_its_base_and_stays_readable(tmp_path, monkeypatch):
    """v3 is the version reviewed; v1 and v2 keep only what their notes
    say changed, and read as before wherever they didn't."""
    import json
    import pymupdf
    from corpus.history import slim
    from corpus.review import review
    from corpus.storage import db

    monkeypatch.chdir(tmp_path)
    v1 = _act({"1": [("1", "a"), ("2", "b, first")], "2": [("1", "c")]},
              {("1", "2"): ["S. 1(2) amended by No. 5/2018 s. 3."]})
    v2 = _act({"1": [("1", "a"), ("2", "b")], "2": [("1", "c, misread")]},
              {("1", "2"): ["S. 1(2) amended by Nos 5/2018 s. 3, 6/2020 s. 1."]})
    v3 = _act({"1": [("1", "a"), ("2", "b")], "2": [("1", "c")]},
              {("1", "2"): ["S. 1(2) amended by Nos 5/2018 s. 3, 6/2020 s. 1."]})
    for v, p in ((1, v1), (2, v2), (3, v3)):
        _write(tmp_path, f"act-v{v}", {**p, "hierarchy": HIERARCHY})
    db.save_verified("act-v3", [{**n, "_node_id": n["id"], "verified_at": "2026-01-01"} for n in v3["nodes"]])
    db.save_verified("act-v2", [{**n, "_node_id": n["id"], "verified_at": "2026-01-01"} for n in v2["nodes"][:2]])
    pdf = tmp_path / "v1.pdf"
    doc = pymupdf.open()
    for n in range(3):
        doc.new_page().insert_text((72, 72), f"page {n + 1}")
    doc.save(pdf)

    report = slim.apply("act", tmp_path, pdf_for=lambda slug: pdf if slug == "act-v1" else None)

    assert report["base"] == 3 and slim.base_version("act", tmp_path) == 3, "the one reviewed, and remembered"
    assert set(report["versions"]) == {1, 2}
    for slug, want in (("act-v1", ["a", "b, first", "c"]), ("act-v2", ["a", "b", "c"])):
        data = json.loads((tmp_path / "data" / "parsed" / f"{slug}.json").read_text())
        assert "nodes" not in data and "slim" in data
        assert [n["text"] for n in review.load_parsed(slug)[0] if n["type"] == "subsection"] == want, \
            f"{slug}: its own words where its notes say so, v3's (reviewed) elsewhere -- 'c, misread' is gone"
    assert db.load_verified("act-v2") == [], "its rows were for words that are v3's now"
    assert sum(1 for p in pymupdf.open(pdf) if p.get_text().strip()) < 3, "its other pages blank"
    assert slim.apply("act", tmp_path)["versions"][1]["missing"] == [], "slimming again only filters what it kept"


def test_a_slim_version_is_not_parsed_again_from_its_blanked_pdf(tmp_path, monkeypatch):
    import corpus.web.dashboard as dashboard
    import pytest

    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "_list_cache", None)
    _write(tmp_path, "act-v1", {"slim": {"toward": "act-v2", "pieces": [], "notes": {}}, "version": {}})
    with pytest.raises(dashboard.HTTPException, match="kept slim"):
        dashboard.reparse_act("act-v1", kind="act", profile="", start_page="", end_page="", confirm="", discard="", mode="")


def test_a_piece_kept_for_its_pages_is_its_own_even_over_a_borrowed_neighbour():
    """The neighbour may be slim too, its nodes borrowed in turn: a piece
    this version prints is still this version's, not borrowed."""
    neighbour = [{**n, "_borrowed": True, "rects": []} for n in OLD["nodes"]]
    kept = delta.slim(NEW, toward="act-v1", text=[], pages_only=["s1/2"])

    whole = delta.assemble(neighbour, kept["slim"])
    piece = next(n for n in whole if n["id"] == "s1/2")
    assert "_borrowed" not in piece and piece["rects"] == NEW["nodes"][2]["rects"]
    assert all(n.get("_borrowed") for n in whole if n["id"] != "s1/2")


def test_a_version_keeps_the_pages_review_sets_a_change_beside():
    """The whole provision a changed piece is part of, and where a version
    hasn't got a provision, the ones either side of where it would be --
    kept for their pages, the words staying its neighbour's."""
    from corpus.history.slim import _around

    older = delta.notes_index(_act({"1": [("1", "a")], "2": [("1", "b")], "3": [("1", "c")]})["nodes"])
    newer = delta.notes_index(_act({"1": [("1", "a")], "2": [("1", "b")], "2A": [("1", "new")],
                                    "3": [("1", "c")]})["nodes"])

    assert _around(["s1/1"], older, newer) == {"s1"}
    assert _around(["s2a"], older, newer) == {"s2", "s3"}, "s 2A inserted: the sections it sits between"
    assert _around(["s2a/1"], newer, older) == {"s2a"}


def test_a_provision_kept_for_its_pages_keeps_the_words_changed_inside_it():
    kept = delta.slim(NEW, toward="act-v1", text=["s1/2"], pages_only=["s1"])

    assert [(p["name"], p["words"]) for p in kept["slim"]["pieces"]] == [("s1/2", True), ("s1", False)]
    whole = delta.assemble(OLD["nodes"], kept["slim"])
    assert [n["text"] for n in whole if n["type"] == "subsection"][:2] == ["a", "b as amended"]
    assert all(n["rects"] for n in whole[:3]), "all of s 1 printed where this version prints it"


def test_a_schedule_named_by_an_older_parser_is_the_same_schedule():
    """A Schedule's entries were sections to the parser once ("sch2/s5")
    and are clauses now ("sch2/cl5"). The base is often the older parse:
    matched by name as written, a newer version's changed clause went in
    beside the base's instead of in place of it."""
    neighbour = [{"type": "schedule", "number": "2", "_node_id": "sch2", "text": ""},
                 {"type": "section", "number": "5", "_node_id": "sch2/s5", "text": "old words"},
                 {"type": "section", "number": "6", "_node_id": "sch2/s6", "text": "same"}]
    mine = {"type": "clause", "number": "5", "_node_id": "sch2/cl5", "text": "new words"}
    part = {"pieces": [{"name": "sch2/cl5", "words": True, "after": ["sch2"], "nodes": [mine]}], "removed": []}

    assert [n["text"] for n in delta.assemble(neighbour, part)] == ["", "new words", "same"]
    assert delta.canonical("sch2/s5/1") == delta.canonical("sch2/cl5/1") == "sch2/cl5/1"
    assert delta.canonical("s5") == "s5" and delta.canonical("sch2") == "sch2", "only a Schedule's own entries"
