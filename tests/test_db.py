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


def test_get_ai_suggestion_is_none_when_nothing_recorded():
    assert db.get_ai_suggestion("crimes-act", 5) is None


def test_save_and_get_ai_suggestion_round_trips():
    saved = db.save_ai_suggestion(
        "crimes-act", 5, answer="Likely the mis-numbered one.",
        reasoning="Its own text reads as a continuation of the previous paragraph.",
        confidence="medium", model="qwen2.5:7b-instruct",
    )
    assert saved["requested_at"]  # stamped

    loaded = db.get_ai_suggestion("crimes-act", 5)
    assert loaded["answer"] == "Likely the mis-numbered one."
    assert loaded["confidence"] == "medium"
    assert loaded["model"] == "qwen2.5:7b-instruct"


def test_save_ai_suggestion_overwrites_rather_than_accumulating():
    db.save_ai_suggestion(
        "crimes-act", 5, answer="first answer", reasoning="r1", confidence="low", model="m1",
    )
    db.save_ai_suggestion(
        "crimes-act", 5, answer="second answer", reasoning="r2", confidence="high", model="m2",
    )
    loaded = db.get_ai_suggestion("crimes-act", 5)
    assert loaded["answer"] == "second answer"
    assert loaded["model"] == "m2"


def test_load_ai_scan_findings_is_empty_for_an_act_with_none():
    assert db.load_ai_scan_findings("crimes-act") == []


def test_save_and_load_ai_scan_finding_round_trips():
    saved = db.save_ai_scan_finding("crimes-act", 5, severity="warning", message="heading reads as a sentence", model="qwen2.5:7b-instruct")
    assert saved["scanned_at"]  # stamped

    [loaded] = db.load_ai_scan_findings("crimes-act")
    assert loaded["node_index"] == 5
    assert loaded["severity"] == "warning"
    assert loaded["message"] == "heading reads as a sentence"
    assert loaded["model"] == "qwen2.5:7b-instruct"


def test_save_ai_scan_finding_overwrites_rather_than_accumulating():
    db.save_ai_scan_finding("crimes-act", 5, severity="warning", message="first", model="m1")
    db.save_ai_scan_finding("crimes-act", 5, severity="clean", message="", model="m2")
    [loaded] = db.load_ai_scan_findings("crimes-act")
    assert loaded["severity"] == "clean"
    assert loaded["model"] == "m2"


def test_load_ai_scan_findings_is_ordered_by_node_index():
    db.save_ai_scan_finding("crimes-act", 9, severity="clean", message="", model="m")
    db.save_ai_scan_finding("crimes-act", 2, severity="clean", message="", model="m")
    assert [row["node_index"] for row in db.load_ai_scan_findings("crimes-act")] == [2, 9]


def test_ai_scan_progress_with_nothing_scanned_yet():
    assert db.ai_scan_progress("crimes-act") == {"scanned": 0, "concerns": 0}


def test_ai_scan_progress_counts_scanned_units_and_only_non_clean_ones_as_concerns():
    db.save_ai_scan_finding("crimes-act", 0, severity="clean", message="", model="m")
    db.save_ai_scan_finding("crimes-act", 1, severity="warning", message="looks wrong", model="m")
    db.save_ai_scan_finding("crimes-act", 2, severity="info", message="worth a look", model="m")
    assert db.ai_scan_progress("crimes-act") == {"scanned": 3, "concerns": 2}


def test_ai_scan_progress_is_scoped_to_one_act():
    db.save_ai_scan_finding("crimes-act", 0, severity="warning", message="x", model="m")
    db.save_ai_scan_finding("evidence-act", 0, severity="clean", message="", model="m")
    assert db.ai_scan_progress("crimes-act") == {"scanned": 1, "concerns": 1}
    assert db.ai_scan_progress("evidence-act") == {"scanned": 1, "concerns": 0}


def test_clear_ai_scan_findings_removes_only_that_acts_rows():
    db.save_ai_scan_finding("crimes-act", 0, severity="clean", message="", model="m")
    db.save_ai_scan_finding("evidence-act", 0, severity="clean", message="", model="m")
    removed = db.clear_ai_scan_findings("crimes-act")
    assert removed == 1
    assert db.load_ai_scan_findings("crimes-act") == []
    assert len(db.load_ai_scan_findings("evidence-act")) == 1


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


# ---------------------------------------------------------------------
# Re-filing a document under a new slug
# ---------------------------------------------------------------------

def test_rename_act_moves_every_kind_of_stored_row(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.save_verified("cpa", [{"type": "section", "number": "1", "_source_node_index": 0}])
    db.add_link("cpa", 0, 0, 4, "act_citation", "text here")
    db.add_correction("cpa", parser_output={"type": "section"}, human_output={"type": "section"}, changed=False)
    db.save_blind_review("cpa", 0, guessed_type="section", guessed_number="1", guessed_heading=None,
                         reasoning="reads like a section", matched_type=True, matched_number=True)
    db.save_ai_suggestion("cpa", 0, answer="a", reasoning="r", confidence="low", model="m")
    db.save_ai_scan_finding("cpa", 0, severity="warning", message="looks wrong", model="m")
    db.save_parse_fingerprint("cpa", "abc123")
    db.add_custom_type("cpa", "penalty")

    moved = db.rename_act("cpa", "cpa-v114")

    assert set(moved) == {
        "verified", "links", "corrections", "blind_reviews", "ai_suggestions", "ai_scan_findings",
        "parse_state", "custom_types",
    }
    assert db.load_verified("cpa") == []
    assert len(db.load_verified("cpa-v114")) == 1
    assert db.load_parse_fingerprint("cpa-v114") == "abc123"
    assert db.load_custom_types("cpa-v114") == ["penalty"]
    assert db.load_links("cpa-v114")[0]["node_index"] == 0
    assert db.get_ai_suggestion("cpa-v114", 0)["answer"] == "a"
    assert db.load_ai_scan_findings("cpa-v114")[0]["message"] == "looks wrong"


def test_rename_act_refuses_to_merge_into_a_slug_that_already_has_work(tmp_path, monkeypatch):
    # Two documents' review work interleaved by node position would be
    # worse than either alone, and unrecoverable afterwards.
    monkeypatch.chdir(tmp_path)
    db.save_verified("cpa", [{"type": "section", "number": "1", "_source_node_index": 0}])
    db.save_verified("cpa-v114", [{"type": "section", "number": "9", "_source_node_index": 0}])

    with pytest.raises(ValueError, match="refusing to merge"):
        db.rename_act("cpa", "cpa-v114")

    assert len(db.load_verified("cpa")) == 1  # nothing moved


def test_rename_act_on_a_slug_with_nothing_stored_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert db.rename_act("never-parsed", "never-parsed-v1") == {}


def test_rename_act_overwrites_the_destinations_own_parse_fingerprint(tmp_path, monkeypatch):
    """The fingerprint is what the pipeline recorded about the
    destination's parse, not anything a person did -- it has to give way,
    or the moved review rows would claim to belong to a parse they were
    never reviewed against. It must also not block the move: writing it is
    the first thing the pipeline does for a newly-parsed version."""
    monkeypatch.chdir(tmp_path)
    db.save_verified("cpa", [{"type": "section", "number": "1", "_source_node_index": 0}])
    db.save_parse_fingerprint("cpa", "reviewed-against-this")
    db.save_parse_fingerprint("cpa-v114", "freshly-parsed")

    db.rename_act("cpa", "cpa-v114")

    assert db.load_parse_fingerprint("cpa-v114") == "reviewed-against-this"


def _one_of_everything(act: str) -> None:
    db.save_verified(act, [{"type": "section", "number": "1", "_source_node_index": 0}])
    db.add_link(act, 0, 0, 4, "act_citation", "text here")
    db.add_correction(act, parser_output={"type": "section"}, human_output={"type": "note"}, changed=True)
    db.save_blind_review(act, 0, guessed_type="section", guessed_number="1", guessed_heading=None,
                         reasoning="reads like a section", matched_type=True, matched_number=True)
    db.save_ai_suggestion(act, 0, answer="a", reasoning="r", confidence="low", model="m")
    db.save_ai_scan_finding(act, 0, severity="warning", message="looks wrong", model="m")
    db.add_orphaned_reviews(act, [{"type": "section", "number": "99"}])
    db.save_parse_fingerprint(act, "abc123")
    db.add_custom_type(act, "penalty")


def test_clear_act_review_drops_everything_keyed_to_a_node_position(tmp_path, monkeypatch):
    """Re-anchoring a document's decisions onto a new parse is the right
    default, but not after a parser change big enough that they describe
    provisions that no longer exist in that shape. Then the only
    alternative to this is resetting them one at a time."""
    monkeypatch.chdir(tmp_path)
    _one_of_everything("cpa")

    cleared = db.clear_act_review("cpa")

    assert set(cleared) == {
        "verified", "links", "blind_reviews", "orphaned_reviews",
        "ai_suggestions", "ai_scan_findings", "parse_state",
    }
    assert db.load_verified("cpa") == []
    assert db.load_links("cpa") == []
    assert db.get_blind_review("cpa", 0) is None
    assert db.get_ai_suggestion("cpa", 0) is None
    assert db.load_ai_scan_findings("cpa") == []
    assert db.load_orphaned_reviews("cpa") == []
    assert db.load_parse_fingerprint("cpa") is None


def test_clear_act_review_keeps_what_isnt_tied_to_a_position(tmp_path, monkeypatch):
    """A correction is the only record of what a parser said and what a
    human said instead; a custom type is the reviewer's own vocabulary.
    Neither belongs to a node position, and neither stops a document being
    reviewed again from scratch."""
    monkeypatch.chdir(tmp_path)
    _one_of_everything("cpa")

    db.clear_act_review("cpa")

    from ai_pipeline.examples_store import stats

    assert stats()["total"] == 1
    assert db.load_custom_types("cpa") == ["penalty"]


def test_clear_act_review_leaves_other_documents_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _one_of_everything("cpa")
    _one_of_everything("interpretation")

    db.clear_act_review("cpa")

    assert db.load_verified("cpa") == []
    assert len(db.load_verified("interpretation")) == 1
    assert db.load_parse_fingerprint("interpretation") == "abc123"
