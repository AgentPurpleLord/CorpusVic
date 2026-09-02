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
    parsed = {"type": "paragraph", "number": "a", "heading": None, "text": "orig"}
    human = {"type": "paragraph", "number": "a", "heading": None, "text": "fixed"}
    db.add_correction("crimes-act", parser_output=parsed, human_output=human, changed=True)
    db.add_correction("crimes-act", parser_output=parsed, human_output=parsed, changed=False)

    assert db.stats() == {"total": 2, "changed": 1}


def test_stats_with_no_corrections_yet():
    assert db.stats() == {"total": 0, "changed": 0}


def test_get_blind_review_is_none_when_nothing_recorded():
    assert db.get_blind_review("crimes-act", 5) is None


def test_save_and_get_blind_review_round_trips():
    saved = db.save_blind_review(
        "crimes-act", 5, guessed_type="paragraph", guessed_number="a", guessed_heading=None,
        reasoning="Looked like a lettered sub-item under (2).", matched_type=True, matched_number=False,
    )
    assert saved["reviewed_at"]  # stamped

    loaded = db.get_blind_review("crimes-act", 5)
    assert loaded["guessed_type"] == "paragraph"
    assert loaded["guessed_number"] == "a"
    assert loaded["reasoning"] == "Looked like a lettered sub-item under (2)."
    assert loaded["matched_type"] is True
    assert loaded["matched_number"] is False


def test_save_blind_review_overwrites_rather_than_accumulating():
    db.save_blind_review(
        "crimes-act", 5, guessed_type="paragraph", guessed_number="a", guessed_heading=None,
        reasoning="first guess", matched_type=False, matched_number=False,
    )
    db.save_blind_review(
        "crimes-act", 5, guessed_type="subparagraph", guessed_number="i", guessed_heading=None,
        reasoning="reconsidered", matched_type=True, matched_number=True,
    )
    loaded = db.get_blind_review("crimes-act", 5)
    assert loaded["guessed_type"] == "subparagraph"
    assert loaded["reasoning"] == "reconsidered"


def test_blind_review_stats_aggregates_matches_and_narrows_by_act():
    db.save_blind_review(
        "crimes-act", 1, guessed_type="paragraph", guessed_number="a", guessed_heading=None,
        reasoning="r1", matched_type=True, matched_number=True,
    )
    db.save_blind_review(
        "crimes-act", 2, guessed_type="paragraph", guessed_number="b", guessed_heading=None,
        reasoning="r2", matched_type=True, matched_number=False,
    )
    db.save_blind_review(
        "other-act", 1, guessed_type="section", guessed_number="1", guessed_heading=None,
        reasoning="r3", matched_type=False, matched_number=False,
    )

    assert db.blind_review_stats("crimes-act") == {"total": 2, "type_matched": 2, "number_matched": 1}
    assert db.blind_review_stats("other-act") == {"total": 1, "type_matched": 0, "number_matched": 0}
    assert db.blind_review_stats() == {"total": 3, "type_matched": 2, "number_matched": 1}


def test_blind_review_stats_with_nothing_recorded_yet():
    assert db.blind_review_stats() == {"total": 0, "type_matched": 0, "number_matched": 0}


def test_load_custom_types_is_empty_for_an_act_with_none():
    assert db.load_custom_types("crimes-act") == []


def test_custom_types_are_scoped_to_one_act():
    db.add_custom_type("crimes-act", "penalty")
    db.add_custom_type("evidence-act", "caution")
    assert db.load_custom_types("crimes-act") == ["penalty"]
    assert db.load_custom_types("evidence-act") == ["caution"]


def test_add_custom_type_is_idempotent():
    db.add_custom_type("crimes-act", "penalty")
    db.add_custom_type("crimes-act", "penalty")
    assert db.load_custom_types("crimes-act") == ["penalty"]


def test_rename_custom_type_keeps_its_place_in_the_list():
    # Renaming re-inserts the row, so it would sort to the end unless the
    # original created_at is carried across -- which is what keeps the
    # relabel dropdown from reshuffling under a reviewer.
    db.add_custom_type("crimes-act", "aaa")
    db.add_custom_type("crimes-act", "zzz")
    db.rename_custom_type("crimes-act", "aaa", "mmm")
    assert db.load_custom_types("crimes-act") == ["mmm", "zzz"]


def test_rename_custom_type_is_a_no_op_for_an_unknown_name():
    db.add_custom_type("crimes-act", "penalty")
    db.rename_custom_type("crimes-act", "nope", "something")
    assert db.load_custom_types("crimes-act") == ["penalty"]


def test_delete_custom_type_removes_only_that_one():
    db.add_custom_type("crimes-act", "penalty")
    db.add_custom_type("crimes-act", "transitional")
    db.delete_custom_type("crimes-act", "penalty")
    assert db.load_custom_types("crimes-act") == ["transitional"]
