"""Giving the rows written before node_id existed their names.

Every fixture builds its own parse and its own database under tmp_path,
so nothing here reads the real review work.
"""
import json
import sqlite3

import pytest

from corpus.storage import db, node_names


def node(node_type, number=None, heading=None, text=""):
    return {"type": node_type, "number": number, "heading": heading, "text": text}


@pytest.fixture
def act(tmp_path, monkeypatch):
    """A parsed Act on disk, and an empty database beside it."""
    monkeypatch.chdir(tmp_path)
    db.close_connections()
    parsed = tmp_path / "data" / "parsed"
    parsed.mkdir(parents=True)

    def write(nodes, slug="act"):
        (parsed / f"{slug}.json").write_text(json.dumps({"act": slug, "nodes": nodes}))
    write([node("section", "97", "Purposes"),
           node("paragraph", "d", text="to ensure a fair trial, by—"),
           node("subparagraph", "i", text="ensuring that the prosecution case")])
    yield write
    db.close_connections()


def add_verified(conn, act_slug, index):
    conn.execute("INSERT INTO verified (act, source_node_index, type) VALUES (?, ?, 'section')",
                 (act_slug, index))
    conn.commit()


# --- naming rows ---------------------------------------------------------

def test_a_row_is_named_after_the_provision_it_points_at(act):
    conn = db._connect()
    add_verified(conn, "act", 2)
    node_names.name_rows(write=True)
    assert conn.execute("SELECT node_id FROM verified").fetchone()[0] == "s97/d/i"


def test_reporting_changes_nothing(act):
    conn = db._connect()
    add_verified(conn, "act", 0)
    report = node_names.name_rows(write=False)
    assert report["named"] == {"verified": 1}
    assert conn.execute("SELECT node_id FROM verified").fetchone()[0] is None


def test_running_it_again_leaves_the_names_alone(act):
    conn = db._connect()
    add_verified(conn, "act", 0)
    node_names.name_rows(write=True)
    assert node_names.name_rows(write=True)["named"] == {}
    assert conn.execute("SELECT node_id FROM verified").fetchone()[0] == "s97"


def test_a_reparse_does_not_rename_what_is_already_named(act):
    conn = db._connect()
    add_verified(conn, "act", 0)
    node_names.name_rows(write=True)
    # The same provision, now two positions further down.
    act([node("part", "2", "Committal"), node("division", "1", "General"),
         node("section", "97", "Purposes")])
    node_names.name_rows(write=True)
    assert conn.execute("SELECT node_id FROM verified").fetchone()[0] == "s97"


# --- what it refuses to guess -------------------------------------------

def test_a_position_past_the_end_of_the_parse_is_left_alone(act):
    conn = db._connect()
    add_verified(conn, "act", 900)
    report = node_names.name_rows(write=True)
    assert report["unnamed"] == {"verified": 1}
    assert conn.execute("SELECT node_id FROM verified").fetchone()[0] is None


def test_an_act_with_no_parse_on_disk_is_reported(act):
    conn = db._connect()
    add_verified(conn, "never-parsed", 0)
    report = node_names.name_rows(write=True)
    assert report["missing_parse"] == ["never-parsed"]
    assert report["unnamed"] == {"verified": 1}


# --- provisions a reviewer added ----------------------------------------

def insert_edit(conn, act_slug, index, after, node_dict):
    conn.execute(
        "INSERT INTO structure_edits (act, node_index, after_index, node_json, created_at) "
        "VALUES (?, ?, ?, ?, '')",
        (act_slug, index, after, json.dumps(node_dict)))
    conn.commit()


def test_an_inserted_provision_is_named_against_what_it_follows(act):
    conn = db._connect()
    insert_edit(conn, "act", 3, 1, node("note", "1AA"))
    node_names.name_rows(write=True)
    assert conn.execute("SELECT node_id FROM structure_edits").fetchone()[0] == "s97/d+note-1aa"


def test_a_row_attached_to_an_inserted_provision_gets_that_name(act):
    conn = db._connect()
    insert_edit(conn, "act", 3, 1, node("note", "1AA"))
    add_verified(conn, "act", 3)
    report = node_names.name_rows(write=True)
    assert report["inserted"] == {"verified": 1, "structure_edits": 1}
    assert conn.execute("SELECT node_id FROM verified").fetchone()[0] == "s97/d+note-1aa"


def test_an_insert_after_an_insert_is_named_against_it(act):
    conn = db._connect()
    insert_edit(conn, "act", 3, 1, node("note", "1AA"))
    insert_edit(conn, "act", 4, 3, node("note", "2"))
    node_names.name_rows(write=True)
    names = [r[0] for r in conn.execute(
        "SELECT node_id FROM structure_edits ORDER BY node_index")]
    assert names == ["s97/d+note-1aa", "s97/d+note-1aa+note-2"]


def test_two_identical_inserts_after_one_provision_are_told_apart(act):
    conn = db._connect()
    insert_edit(conn, "act", 3, 1, node("note", "1AA"))
    insert_edit(conn, "act", 4, 1, node("note", "1AA"))
    node_names.name_rows(write=True)
    names = [r[0] for r in conn.execute(
        "SELECT node_id FROM structure_edits ORDER BY node_index")]
    assert len(set(names)) == 2


def test_an_insert_at_the_front_of_the_document_still_gets_a_name(act):
    conn = db._connect()
    insert_edit(conn, "act", 3, -1, node("section", "96", "New"))
    node_names.name_rows(write=True)
    assert conn.execute("SELECT node_id FROM structure_edits").fetchone()[0] == "inserted+s96"


# --- the column itself ---------------------------------------------------

def test_a_database_written_before_node_id_existed_gains_it(tmp_path):
    path = tmp_path / "data" / "legislation.db"
    path.parent.mkdir(parents=True)
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE verified (act TEXT NOT NULL, source_node_index INTEGER NOT NULL, "
                "type TEXT NOT NULL, PRIMARY KEY (act, source_node_index))")
    old.execute("INSERT INTO verified VALUES ('act', 0, 'section')")
    old.commit()
    old.close()

    db.close_connections()
    conn = db._connect(tmp_path)
    try:
        assert "node_id" in {r[1] for r in conn.execute("PRAGMA table_info(verified)")}
        # And the row that was already there is still there.
        assert conn.execute("SELECT COUNT(*) FROM verified").fetchone()[0] == 1
    finally:
        db.close_connections()


def test_every_node_keyed_table_has_the_column(tmp_path):
    db.close_connections()
    conn = db._connect(tmp_path)
    try:
        for table in node_names.POSITION_COLUMN:
            assert "node_id" in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}, table
    finally:
        db.close_connections()
