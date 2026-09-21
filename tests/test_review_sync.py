"""Tests for corpus/review/review_sync.py -- the review work as text git can
merge, instead of as one binary file it cannot.

The question underneath all of these is the same one: does a day's
reviewing survive the trip? So the round trip is checked field by field
rather than by counting rows, and the cases that could lose work quietly
-- an export from a database that has not been built yet, a review write
nobody exported, a half-read file -- each get their own test.
"""
import json
import os
import subprocess

import pytest

from corpus.review import review_sync, sync
from corpus.storage import db
from conftest import make_node


@pytest.fixture(autouse=True)
def _fresh_connections(tmp_path, monkeypatch):
    """db caches connections by path, and these tests hand it several.

    chdir as well, and not only for tidiness: db's base_dir defaults to
    the working directory, so one call here that forgets to pass a base
    writes into the repository's own database. That is exactly what
    happened while this file was being written -- twenty-two rows of test
    data in the real correction log."""
    monkeypatch.chdir(tmp_path)
    db.close_connections()
    yield
    db.close_connections()


def _populate(base):
    """A small corpus with something in every kind of table: two acts,
    an autoincrement id, an application-generated one, and the one table
    that is not keyed by act."""
    db.save_verified("crimes-act", [
        dict(make_node("section", "3", "Murder", "A person who..."), _source_node_index=0, _node_id="s0"),
        dict(make_node("subsection", "1", None, "Whosoever"), _source_node_index=1, _node_id="s1"),
    ], base_dir=base)
    db.save_verified("family-violence-act", [
        dict(make_node("section", "5", "Meaning of family violence", "In this Act"),
             _source_node_index=0, _node_id="s0"),
    ], base_dir=base)
    db.add_correction("crimes-act", {"type": "section"}, {"type": "subsection"}, True,
                      base_dir=base)
    db.add_correction("crimes-act", {"type": "note"}, {"type": "note"}, False, base_dir=base)
    db.set_publication("crimes-act", True, base_dir=base)
    db.set_publication("family-violence-act", False, base_dir=base)
    conn = db._connect(base)
    with conn:
        conn.execute(
            "INSERT INTO links (id, act, node_id, node_index, start, end, text, label, "
            "target_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("link-abc", "crimes-act", "s0", 0, 3, 9, "Act", "Sentencing Act 1991",
             json.dumps({"act": "sentencing-act"}), "2024-01-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO orphaned_reviews (act, node_json, orphaned_at) VALUES (?, ?, ?)",
            ("crimes-act", json.dumps({"type": "section"}), "2024-01-01T00:00:00+00:00"))
    return conn


def _contents(conn):
    """Every row of every table, as the exporter would write it -- which
    is the comparison that matters, since that is what travels."""
    return {t.name: sorted(review_sync._line(t, row)
                           for row in conn.execute(f"SELECT * FROM {t.name}"))
            for t in review_sync.describe(conn)}


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------

def test_a_round_trip_keeps_every_field(tmp_path):
    """The one that matters. Everything else here is convenience; this is
    whether the review work survives being written down and read back."""
    source = tmp_path / "server"
    conn = _populate(source)
    before = _contents(conn)
    review_sync.export(source)

    # A second machine, with the text and nothing else -- a fresh clone.
    clone = tmp_path / "clone"
    (clone / "data").mkdir(parents=True)
    _copy_review(source, clone)
    db.close_connections()
    review_sync.import_(clone, backup=False)

    after = _contents(db._connect(clone))
    assert after == before


def _copy_review(source, target):
    import shutil

    shutil.copytree(review_sync.review_dir(source), review_sync.review_dir(target))


def test_check_says_identical_on_a_real_database(tmp_path):
    """The round trip, available on demand rather than only in a test --
    `python3 -m corpus.review.review_sync check` is what answers "would this
    lose anything?" against whatever is actually on the server."""
    _populate(tmp_path)

    report = review_sync.check(tmp_path)

    assert "identical" in report
    assert "LOST" not in report


# ---------------------------------------------------------------------------
# What the files look like, which is what git merges
# ---------------------------------------------------------------------------

def test_one_directory_per_act_and_one_line_per_row(tmp_path):
    _populate(tmp_path)
    review_sync.export(tmp_path)
    out = review_sync.review_dir(tmp_path)

    lines = (out / "crimes-act" / "verified.jsonl").read_text().splitlines()
    assert len(lines) == 2, "one line per provision"
    assert all(json.loads(line)["act"] == "crimes-act" for line in lines)
    assert (out / "family-violence-act" / "verified.jsonl").exists()
    # The one table not keyed by act sits at the top rather than being
    # scattered through every act's directory.
    assert (out / "publication.jsonl").exists()


def test_rows_are_written_in_the_order_the_act_is_in(tmp_path):
    """Sorted by the table's own key, so that a diff shows what changed
    rather than a reshuffle -- and so two reviewers working in different
    parts of an Act are working in different parts of the file."""
    db.save_verified("crimes-act", [
        dict(make_node("section", str(n)), _source_node_index=n, _node_id=f"s{n}") for n in (5, 1, 3)
    ], base_dir=tmp_path)
    review_sync.export(tmp_path)

    written = (review_sync.review_dir(tmp_path) / "crimes-act" / "verified.jsonl").read_text()
    indexes = [json.loads(line)["source_node_index"] for line in written.splitlines()]
    assert indexes == [1, 3, 5]


def test_a_second_export_of_unchanged_work_rewrites_nothing(tmp_path):
    """This runs on every status poll. Rewriting files that have not
    changed would churn mtimes and make git report work where there is
    none."""
    _populate(tmp_path)
    first = review_sync.export(tmp_path)
    assert first["written"] == first["files"]

    again = review_sync.export(tmp_path)

    assert again["written"] == 0
    assert again["files"] == first["files"]


def test_an_acts_review_being_cleared_removes_its_files(tmp_path):
    """A stale file left behind would be re-imported later and quietly
    bring back work somebody deleted."""
    _populate(tmp_path)
    review_sync.export(tmp_path)
    db.clear_act_review("family-violence-act", base_dir=tmp_path)

    review_sync.export(tmp_path)

    assert not (review_sync.review_dir(tmp_path) / "family-violence-act").exists()
    assert (review_sync.review_dir(tmp_path) / "crimes-act").exists()


def test_a_table_nobody_named_is_exported_anyway(tmp_path):
    """The registry is read from the database rather than written down,
    so a table added to db.py starts syncing without anyone remembering
    to add it here. A table left out is review work that silently stops
    travelling, which is the failure this module exists to remove."""
    conn = db._connect(tmp_path)
    with conn:
        conn.execute("CREATE TABLE later_addition (act TEXT NOT NULL, note TEXT)")
        conn.execute("INSERT INTO later_addition (act, note) VALUES ('crimes-act', 'hello')")

    review_sync.export(tmp_path, conn=conn)

    written = review_sync.review_dir(tmp_path) / "crimes-act" / "later_addition.jsonl"
    assert json.loads(written.read_text()) == {"act": "crimes-act", "note": "hello"}


# ---------------------------------------------------------------------------
# Identity: which ids are real and which are sqlite's
# ---------------------------------------------------------------------------

def test_an_autoincrement_id_is_not_written_down(tmp_path):
    """corrections.id is allocated by whichever machine wrote the row.
    Writing it into the file would mean two machines both claiming 1726
    for different rows, and a merge that looks clean while being wrong."""
    _populate(tmp_path)
    review_sync.export(tmp_path)

    written = (review_sync.review_dir(tmp_path) / "crimes-act" / "corrections.jsonl").read_text()
    for line in written.splitlines():
        assert "id" not in json.loads(line)


def test_two_machines_ids_do_not_collide_on_import(tmp_path):
    """Both sides numbered their first correction 1. Re-assigned on
    import, so both survive -- which is only safe because the id was
    never written down to be merged on."""
    out = review_sync.review_dir(tmp_path)
    (out / "crimes-act").mkdir(parents=True)
    (out / "crimes-act" / "corrections.jsonl").write_text(
        json.dumps({"act": "crimes-act", "ts": 1.0, "changed": 1,
                    "ai_output_json": "{}", "human_output_json": '{"from": "server"}'}) + "\n" +
        json.dumps({"act": "crimes-act", "ts": 2.0, "changed": 1,
                    "ai_output_json": "{}", "human_output_json": '{"from": "laptop"}'}) + "\n")

    review_sync.import_(tmp_path, backup=False)

    rows = db._connect(tmp_path).execute(
        "SELECT id, human_output_json FROM corrections ORDER BY ts").fetchall()
    assert [r["id"] for r in rows] == [1, 2]
    assert ['"server"' in r["human_output_json"] for r in rows] == [True, False]


def test_an_application_generated_id_is_kept(tmp_path):
    """links.id is a TEXT id this code makes up and then refers to -- a
    real identity, unlike a rowid. Re-assigning it would break every
    reference to it."""
    _populate(tmp_path)
    review_sync.export(tmp_path)

    written = (review_sync.review_dir(tmp_path) / "crimes-act" / "links.jsonl").read_text()
    assert json.loads(written)["id"] == "link-abc"


# ---------------------------------------------------------------------------
# Refusing rather than half-importing
# ---------------------------------------------------------------------------

def test_a_malformed_file_leaves_the_database_exactly_as_it_was(tmp_path):
    """Half-importing is how you end up with a corpus that is neither
    what was on disk nor what was in the database, with no way to tell
    which rows made it."""
    _populate(tmp_path)
    review_sync.export(tmp_path)
    before = _contents(db._connect(tmp_path))
    broken = review_sync.review_dir(tmp_path) / "crimes-act" / "verified.jsonl"
    broken.write_text(broken.read_text() + "{not json at all\n")

    with pytest.raises(review_sync.ImportError_) as excinfo:
        review_sync.import_(tmp_path, backup=False)

    assert "verified.jsonl:3" in str(excinfo.value), "it says which line"
    db.close_connections()
    assert _contents(db._connect(tmp_path)) == before


def test_a_field_the_table_does_not_have_is_refused(tmp_path):
    out = review_sync.review_dir(tmp_path)
    (out / "crimes-act").mkdir(parents=True)
    (out / "crimes-act" / "verified.jsonl").write_text(
        json.dumps({"act": "crimes-act", "node_id": "s0", "source_node_index": 0, "type": "section",
                    "invented": "yes"}) + "\n")

    with pytest.raises(review_sync.ImportError_, match="invented"):
        review_sync.import_(tmp_path, backup=False)


def test_a_file_naming_an_unknown_table_is_refused(tmp_path):
    out = review_sync.review_dir(tmp_path)
    (out / "crimes-act").mkdir(parents=True)
    (out / "crimes-act" / "nonsense.jsonl").write_text('{"act": "crimes-act"}\n')

    with pytest.raises(review_sync.ImportError_, match="nonsense"):
        review_sync.import_(tmp_path, backup=False)


def test_an_import_keeps_a_copy_of_what_it_replaced(tmp_path):
    """The database is meant to be derived, but "meant to be" is not a
    guarantee, and this is the one operation that could discard a review
    nobody had exported yet."""
    _populate(tmp_path)
    review_sync.export(tmp_path)

    result = review_sync.import_(tmp_path)

    assert result["backup"] and os.path.exists(result["backup"])


def test_a_row_missing_a_column_added_later_still_imports(tmp_path):
    """A column added to the schema is absent from every file written
    before it. Filling it with NULL would fail against the NOT NULL
    DEFAULT it was added with, and the file would stop importing for a
    reason that has nothing to do with the review work in it."""
    out = review_sync.review_dir(tmp_path)
    (out / "crimes-act").mkdir(parents=True)
    (out / "crimes-act" / "verified.jsonl").write_text(
        json.dumps({"act": "crimes-act", "node_id": "s0", "source_node_index": 0, "type": "section"}) + "\n")

    review_sync.import_(tmp_path, backup=False)

    [row] = db._connect(tmp_path).execute("SELECT * FROM verified").fetchall()
    assert (row["text"], row["needs_followup"]) == ("", 0), "the defaults applied"


# ---------------------------------------------------------------------------
# The two ways this arrangement could lose work quietly
# ---------------------------------------------------------------------------

def test_an_export_from_a_database_nobody_built_is_refused(tmp_path):
    """A fresh clone has the text and no database. Asking the database
    anything creates an empty one, and an export from *that* writes
    nothing and then removes every file it did not write -- so the first
    status poll after a clone would delete the whole corpus's review
    work, report success, and leave a clean tree."""
    source = tmp_path / "server"
    _populate(source)
    review_sync.export(source)
    clone = tmp_path / "clone"
    (clone / "data").mkdir(parents=True)
    _copy_review(source, clone)
    db.close_connections()

    with pytest.raises(review_sync.Unloaded, match="import"):
        review_sync.export(clone)

    assert list(review_sync.review_dir(clone).rglob("*.jsonl")), "the files are still there"
    assert review_sync.unloaded(clone), "and the dashboard can say so"

    review_sync.import_(clone, backup=False)
    assert review_sync.unloaded(clone) is None
    assert review_sync.export(clone)["written"] == 0, "and then it is an ordinary export"


def test_an_export_is_refused_when_the_files_are_newer_than_the_database(tmp_path):
    """The one that cost real work. A `git pull` brings newer review text
    and leaves the database exactly as it was; the database is then older
    than the files, and an export writes it back over them, removing
    every row and every file it did not itself produce. One pull and one
    commit an hour apart destroyed 193 verified provisions, 207
    corrections and 9 link annotations.

    unloaded() does not catch it -- that refuses only an *empty*
    database, and this one is full and healthy-looking."""
    _populate(tmp_path)
    review_sync.export(tmp_path)
    out = review_sync.review_dir(tmp_path)

    # Exactly what a pull does: the files change, the database does not.
    arrived = out / "crimes-act" / "verified.jsonl"
    arrived.write_text(arrived.read_text() + json.dumps(
        {"act": "crimes-act", "node_id": "s99", "source_node_index": 99, "type": "section",
         "text": "arrived in the pull"}, sort_keys=True) + "\n")
    (out / "crimes-act" / "links.jsonl").write_text(json.dumps(
        {"act": "crimes-act", "id": "abc", "node_id": "s1", "node_index": 1, "start": 0, "end": 3,
         "label": "act_citation", "created_at": "2026-01-01T00:00:00+00:00"}) + "\n")

    with pytest.raises(review_sync.Unloaded, match="import"):
        review_sync.export(tmp_path)

    assert "arrived in the pull" in arrived.read_text(), "left exactly as it was"
    assert (out / "crimes-act" / "links.jsonl").exists(), "and not deleted"


def test_loading_what_arrived_lets_the_export_run_again(tmp_path):
    _populate(tmp_path)
    review_sync.export(tmp_path)
    arrived = review_sync.review_dir(tmp_path) / "crimes-act" / "verified.jsonl"
    arrived.write_text(arrived.read_text() + json.dumps(
        {"act": "crimes-act", "node_id": "s99", "source_node_index": 99, "type": "section",
         "text": "arrived in the pull"}, sort_keys=True) + "\n")

    review_sync.import_(tmp_path, backup=False)

    assert review_sync.export(tmp_path)["removed"] == 0
    assert "arrived in the pull" in arrived.read_text()


def test_adopting_is_the_deliberate_way_past_it(tmp_path):
    """For the case the database really is the newer of the two -- after
    resolving a merge by hand. Its own command rather than a flag on
    export, because choosing which version of somebody's review work
    survives is not a thing to do in passing."""
    _populate(tmp_path)
    review_sync.export(tmp_path)
    arrived = review_sync.review_dir(tmp_path) / "crimes-act" / "verified.jsonl"
    arrived.write_text(arrived.read_text() + "{}\n")

    review_sync.adopt(tmp_path)

    review_sync.export(tmp_path)  # no longer refused
    assert "{}" not in arrived.read_text(), "the database won, as asked"


def test_an_ordinary_review_and_export_is_not_refused(tmp_path):
    """The guard has to be invisible in the normal case: reviewing makes
    the database newer than the files, which is the whole point of an
    export and must never look like the dangerous direction."""
    _populate(tmp_path)
    review_sync.export(tmp_path)

    db.save_verified("crimes-act", [
        dict(make_node("section", "9", "Later"), _source_node_index=9, _node_id="s9")], base_dir=tmp_path)

    assert review_sync.export(tmp_path)["written"] >= 1


def test_a_checkout_from_before_the_guard_is_adopted_rather_than_blocked(tmp_path):
    """No record means "first export since an upgrade" far more often
    than it means trouble, and refusing would stop every existing
    checkout working."""
    _populate(tmp_path)
    review_sync.export(tmp_path)
    review_sync._state_path(tmp_path).unlink()

    review_sync.export(tmp_path)  # does not raise
    assert review_sync.files_are_ahead(tmp_path) is None


def test_review_work_with_no_export_still_shows_up_as_pending(tmp_path):
    """The load-bearing one. The database is gitignored, so a day's
    reviewing leaves no pending change at all until an export runs -- and
    a dashboard saying "everything is pushed" over unpushed work would be
    a quieter failure than any this replaced. So sync.status exports
    before it asks git what has changed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "--initial-branch=main")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "T")
    (repo / ".gitignore").write_text(
        "data/legislation.db\ndata/legislation.db-wal\ndata/legislation.db-shm\n")
    _populate(repo)
    review_sync.export(repo)
    _run(repo, "add", ".")
    _run(repo, "commit", "-m", "First")
    assert sync.pending_changes(repo) == []

    # A reviewer verifies one more provision, and nothing else happens.
    db.save_verified("crimes-act", [
        dict(make_node("section", "4", "Manslaughter", "Whosoever"), _source_node_index=2, _node_id="s2"),
    ], base_dir=repo)

    assert sync.pending_changes(repo) == [], "git cannot see a database it is not tracking"
    assert "crimes-act/verified.jsonl" in " ".join(sync.status(repo)["pending"])


def _run(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=True)
