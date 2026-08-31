"""Tests for the pure link-annotation data model (ai_pipeline/
link_annotations.py) -- validation and persistence, independent of
link_review.py's FastAPI layer entirely."""
import json

import pytest

from ai_pipeline.link_annotations import LinkError, add_link, delete_link, links_path, load_links, save_links


@pytest.fixture
def isolate_links(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_add_link_persists_and_returns_a_record(isolate_links):
    node_text = "see the Crimes Act 1958 for detail"
    start, end = node_text.index("Crimes Act 1958"), node_text.index("Crimes Act 1958") + len("Crimes Act 1958")
    record = add_link("crimes-act", node_index=5, start=start, end=end, label="act_citation", node_text=node_text)
    assert record["node_index"] == 5
    assert record["text"] == "Crimes Act 1958"
    assert record["label"] == "act_citation"
    assert record["id"]
    assert record["created_at"]

    on_disk = load_links("crimes-act")
    assert on_disk == [record]


def test_add_link_rejects_unknown_label(isolate_links):
    with pytest.raises(LinkError, match="Unknown label"):
        add_link("crimes-act", node_index=0, start=0, end=3, label="not-a-real-label", node_text="abc")
    assert load_links("crimes-act") == []


@pytest.mark.parametrize("start,end", [(-1, 3), (0, 0), (3, 2), (0, 100)])
def test_add_link_rejects_out_of_range_spans(isolate_links, start, end):
    with pytest.raises(LinkError, match="out of range"):
        add_link("crimes-act", node_index=0, start=start, end=end, label="other", node_text="short text")
    assert load_links("crimes-act") == []


def test_add_link_keeps_appending_to_the_same_act(isolate_links):
    add_link("crimes-act", node_index=0, start=0, end=3, label="other", node_text="one two")
    add_link("crimes-act", node_index=1, start=0, end=3, label="defined_term", node_text="two three")
    assert len(load_links("crimes-act")) == 2


def test_add_link_keeps_different_acts_separate(isolate_links):
    add_link("crimes-act", node_index=0, start=0, end=3, label="other", node_text="one two")
    add_link("evidence-act", node_index=0, start=0, end=3, label="other", node_text="one two")
    assert len(load_links("crimes-act")) == 1
    assert len(load_links("evidence-act")) == 1


def test_load_links_missing_file_returns_empty_list(isolate_links):
    assert load_links("nonexistent-act") == []


def test_delete_link_removes_the_matching_record(isolate_links):
    a = add_link("crimes-act", node_index=0, start=0, end=3, label="other", node_text="one two")
    b = add_link("crimes-act", node_index=1, start=0, end=3, label="defined_term", node_text="two three")
    assert delete_link("crimes-act", a["id"]) is True
    remaining = load_links("crimes-act")
    assert len(remaining) == 1
    assert remaining[0]["id"] == b["id"]


def test_delete_link_returns_false_for_unknown_id(isolate_links):
    add_link("crimes-act", node_index=0, start=0, end=3, label="other", node_text="one two")
    assert delete_link("crimes-act", "not-a-real-id") is False
    assert len(load_links("crimes-act")) == 1


def test_links_path_is_scoped_per_act(isolate_links):
    assert links_path("crimes-act") == links_path("crimes-act")
    assert links_path("crimes-act") != links_path("evidence-act")


def test_save_links_creates_parent_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    save_links("crimes-act", [{"id": "x"}])
    on_disk = json.loads((tmp_path / "data" / "links" / "crimes-act.json").read_text())
    assert on_disk == [{"id": "x"}]
