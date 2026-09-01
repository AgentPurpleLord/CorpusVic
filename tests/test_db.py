"""Tests for ai_pipeline/db.py -- the SQLite-backed storage for verified
review state and the correction log (link annotations are covered by
tests/test_link_annotations.py, which exercises this same module through
ai_pipeline.link_annotations's re-export)."""
import pytest

from ai_pipeline import db
from conftest import make_node


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def test_load_verified_is_empty_for_an_act_with_no_data():
    assert db.load_verified("crimes-act") == []


def test_save_and_load_verified_round_trips_every_field():
    node = dict(
        make_node("subsection", "1", None, "text", verified_at="2024-01-01T00:00:00+00:00"),
        path={"section": "5", "subsection": "1"},
        history=[{"raw": "amended by No. 1/2000 s. 2"}],
        _source_node_index=7,
        _unit_end_index=3,
    )
    db.save_verified("crimes-act", [node])

    [loaded] = db.load_verified("crimes-act")
    assert loaded["type"] == "subsection"
    assert loaded["text"] == "text"
    assert loaded["verified_at"] == "2024-01-01T00:00:00+00:00"
    assert loaded["path"] == {"section": "5", "subsection": "1"}
    assert loaded["history"] == [{"raw": "amended by No. 1/2000 s. 2"}]
    assert loaded["_source_node_index"] == 7
    assert loaded["_unit_end_index"] == 3
    assert "needs_followup" not in loaded  # never set -- shouldn't appear as False


def test_save_verified_omits_optional_fields_when_absent():
    node = dict(make_node("section", "1", "Murder"), _source_node_index=0)
    db.save_verified("crimes-act", [node])

    [loaded] = db.load_verified("crimes-act")
    assert "path" not in loaded
    assert "history" not in loaded
    assert "verified_at" not in loaded
    assert "needs_followup" not in loaded
    assert "_unit_end_index" not in loaded


def test_flagged_node_carries_needs_followup_not_verified_at():
    node = dict(make_node("subsection", "1"), _source_node_index=0, needs_followup=True)
    db.save_verified("crimes-act", [node])

    [loaded] = db.load_verified("crimes-act")
    assert loaded["needs_followup"] is True
    assert "verified_at" not in loaded


def test_save_verified_replaces_this_acts_data_wholesale():
    first = dict(make_node("section", "1"), _source_node_index=0)
    db.save_verified("crimes-act", [first])
    assert len(db.load_verified("crimes-act")) == 1

    second = dict(make_node("section", "2"), _source_node_index=1)
    db.save_verified("crimes-act", [second])

    loaded = db.load_verified("crimes-act")
    assert len(loaded) == 1
    assert loaded[0]["number"] == "2"


def test_save_verified_keeps_different_acts_separate():
    db.save_verified("crimes-act", [dict(make_node("section", "1"), _source_node_index=0)])
    db.save_verified("evidence-act", [dict(make_node("section", "9"), _source_node_index=0)])

    assert len(db.load_verified("crimes-act")) == 1
    assert len(db.load_verified("evidence-act")) == 1
    assert db.load_verified("crimes-act")[0]["number"] == "1"


def test_load_verified_orders_by_source_node_index():
    nodes = [dict(make_node("section", str(i)), _source_node_index=i) for i in (3, 1, 2)]
    db.save_verified("crimes-act", nodes)
    assert [n["_source_node_index"] for n in db.load_verified("crimes-act")] == [1, 2, 3]


def test_base_dir_isolates_verified_data_from_the_current_directory(tmp_path):
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    db.save_verified("crimes-act", [dict(make_node("section", "1"), _source_node_index=0)], base_dir=other_dir)

    assert db.load_verified("crimes-act") == []  # nothing in the cwd-relative db
    assert len(db.load_verified("crimes-act", base_dir=other_dir)) == 1


def test_add_correction_and_stats():
    ai = {"type": "paragraph", "number": "a", "heading": None, "text": "orig"}
    human = {"type": "paragraph", "number": "a", "heading": None, "text": "fixed"}
    db.add_correction("crimes-act", ai_output=ai, human_output=human, changed=True)
    db.add_correction("crimes-act", ai_output=ai, human_output=ai, changed=False)

    assert db.stats() == {"total": 2, "changed": 1}


def test_load_examples_returns_changed_before_unchanged():
    ai = {"type": "paragraph", "number": "a", "heading": None, "text": "orig"}
    human = {"type": "paragraph", "number": "a", "heading": None, "text": "fixed"}
    db.add_correction("crimes-act", ai_output=ai, human_output=ai, changed=False)
    db.add_correction("crimes-act", ai_output=ai, human_output=human, changed=True)

    examples = db.load_examples(k=6)
    assert examples[0]["changed"] is True
    assert examples[0]["human_output"]["text"] == "fixed"


def test_stats_with_no_corrections_yet():
    assert db.stats() == {"total": 0, "changed": 0}
