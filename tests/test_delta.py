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
    for n in range(3):
        doc.new_page().insert_text((72, 72), f"page {n + 1}")
    doc.save(tmp_path / "act.pdf")

    delta.blank_pages(tmp_path / "act.pdf", [2], tmp_path / "slim.pdf")

    slim = pymupdf.open(tmp_path / "slim.pdf")
    assert [p.get_text().strip() for p in slim] == ["", "page 2", ""]
