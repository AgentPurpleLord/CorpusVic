"""Tests for ai_pipeline/structure.py -- what an insert, a delete and a
move mean for a document's reading order."""
import pytest

from ai_pipeline import structure
from ai_pipeline.structure import DOCUMENT_START, StructureError


def edit(after=None, deleted=False, node=None):
    return {"after": after, "deleted": deleted, "node": node}


def test_no_edits_is_the_parse_order():
    assert structure.document_order(4, {}) == [0, 1, 2, 3]


def test_a_deleted_node_is_left_out():
    assert structure.document_order(4, {2: edit(deleted=True)}) == [0, 1, 3]


def test_an_inserted_node_follows_its_anchor():
    edits = {4: edit(after=1, node={"type": "subsection"})}
    assert structure.document_order(4, edits) == [0, 1, 4, 2, 3]


def test_an_inserted_node_can_go_at_the_very_start():
    edits = {4: edit(after=DOCUMENT_START, node={"type": "part"})}
    assert structure.document_order(4, edits) == [4, 0, 1, 2, 3]


def test_a_moved_node_leaves_its_old_place_and_takes_the_new_one():
    assert structure.document_order(4, {0: edit(after=2)}) == [1, 2, 0, 3]


def test_moving_a_node_backwards_works_the_same_way():
    assert structure.document_order(4, {3: edit(after=0)}) == [0, 3, 1, 2]


def test_several_nodes_anchored_to_one_keep_a_stable_order():
    """Ascending index, so the order two inserts after the same piece
    come out in is the order they were made in rather than whatever the
    dict happens to iterate."""
    edits = {
        5: edit(after=1, node={"type": "note"}),
        4: edit(after=1, node={"type": "note"}),
    }
    assert structure.document_order(3, edits) == [0, 1, 4, 5, 2]


def test_a_chain_of_inserts_stays_in_chain_order():
    edits = {
        3: edit(after=0, node={"type": "a"}),
        4: edit(after=3, node={"type": "b"}),
        5: edit(after=4, node={"type": "c"}),
    }
    assert structure.document_order(3, edits) == [0, 3, 4, 5, 1, 2]


def test_a_node_anchored_to_a_deleted_one_still_has_a_place():
    """Deleting the piece something was hung off must not take that
    something with it -- the anchor is a position, not a dependency."""
    edits = {
        1: edit(deleted=True),
        3: edit(after=1, node={"type": "note"}),
    }
    assert structure.document_order(3, edits) == [0, 3, 2]


def test_a_long_chain_does_not_hit_the_recursion_limit():
    """A reviewer moving a long run of provisions one at a time builds a
    chain as long as the run."""
    edits = {i: edit(after=i - 1) for i in range(1, 3000)}
    assert structure.document_order(3000, edits) == list(range(3000))


def test_two_nodes_placed_after_each_other_is_refused():
    with pytest.raises(StructureError, match="after itself"):
        structure.document_order(4, {0: edit(after=1), 1: edit(after=0)})


def test_a_node_placed_after_itself_is_refused():
    with pytest.raises(StructureError, match="after itself"):
        structure.document_order(4, {2: edit(after=2)})


def test_a_node_anchored_to_nothing_that_exists_lands_at_the_end():
    """Not reachable through any endpoint, but a provision appearing
    somewhere odd is a visible problem and a silently dropped one is
    not."""
    edits = {9: edit(after=400, node={"type": "note"})}
    assert structure.document_order(3, edits) == [0, 1, 2, 9]


def test_next_index_never_reuses_a_name():
    assert structure.next_index(10, {}) == 10
    assert structure.next_index(10, {10: edit(node={})}) == 11
    # ...including one since deleted: an index is a name, and every
    # stored link span and finding still keys on the old one.
    assert structure.next_index(10, {10: edit(node={}, deleted=True)}) == 11


def test_is_live():
    edits = {5: edit(node={"type": "x"}), 2: edit(deleted=True)}
    assert structure.is_live(0, 4, edits)
    assert structure.is_live(5, 4, edits)
    assert not structure.is_live(2, 4, edits)
    assert not structure.is_live(6, 4, edits)
    assert not structure.is_live(-1, 4, edits)


def test_with_edit_does_not_touch_the_original():
    """A caller tries an edit against document_order before keeping it,
    so the attempt must not be able to half-apply itself."""
    edits = {1: edit(after=0)}
    candidate = structure.with_edit(edits, 1, deleted=True)
    assert candidate[1] == {"after": 0, "deleted": True, "node": None}
    assert edits[1]["deleted"] is False


def test_with_edit_starts_a_new_entry_from_the_neutral_state():
    candidate = structure.with_edit({}, 7, after=3, node={"type": "note"})
    assert candidate == {7: {"after": 3, "deleted": False, "node": {"type": "note"}}}


# ---------------------------------------------------------------------
# place_after -- splicing rather than just writing an anchor
# ---------------------------------------------------------------------

def test_place_after_puts_a_node_directly_after_its_anchor():
    """The bug this exists for: something was already anchored where the
    node was being moved to, both claimed the same place, and the tie
    went to whichever had the lower index -- so moving a definition up
    one, behind a definition already anchored there, did nothing at all."""
    edits = {1: edit(after=0), 5: edit(after=1, node={"type": "definition"})}
    order = structure.document_order(4, edits)
    assert order == [0, 1, 5, 2, 3]

    moved = structure.place_after(edits, 5, 0, order)

    assert structure.document_order(4, moved) == [0, 5, 1, 2, 3]


def test_place_after_can_move_a_node_past_the_one_that_follows_it():
    """X then Y, with Y anchored to X. Moving X after Y has to work, and
    naively writing the anchor makes each point at the other."""
    edits = {2: edit(after=1)}
    order = structure.document_order(4, edits)
    assert order == [0, 1, 2, 3]

    moved = structure.place_after(edits, 1, 2, order)

    assert structure.document_order(4, moved) == [0, 2, 1, 3]


def test_place_after_leaves_a_moved_nodes_followers_behind():
    """A piece inserted after s 5 belongs after s 5, not wherever s 5
    subsequently goes."""
    edits = {4: edit(after=1, node={"type": "note"})}
    order = structure.document_order(4, edits)
    assert order == [0, 1, 4, 2, 3]

    moved = structure.place_after(edits, 1, 3, order)

    assert structure.document_order(4, moved) == [0, 4, 2, 3, 1]


def test_place_after_an_insert_pushes_the_previous_insert_down():
    """Two inserts below the same piece come out in the order they were
    made, each one going directly below it."""
    first = structure.place_after({}, 4, 1)
    first[4]["node"] = {"type": "note"}
    order = structure.document_order(4, first)
    second = structure.place_after(first, 5, 1, order)
    second[5]["node"] = {"type": "note"}

    assert structure.document_order(4, second) == [0, 1, 5, 4, 2, 3]


def test_place_after_does_not_touch_the_original():
    edits = {1: edit(after=0)}
    structure.place_after(edits, 1, 2, [0, 1, 2])
    assert edits[1]["after"] == 0


def test_place_after_at_the_document_start():
    moved = structure.place_after({}, 3, DOCUMENT_START, [0, 1, 2, 3])
    assert structure.document_order(4, moved) == [3, 0, 1, 2]
