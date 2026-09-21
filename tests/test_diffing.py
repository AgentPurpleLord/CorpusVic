"""Tests for corpus/diffing.py -- what changed in a provision between
two Authorised Versions of an Act.

An Act is reprinted every few weeks and each reprint restates the whole
thing, so "what changed" is nowhere in the documents: it has to be worked
out by comparing them. The three things that make that hard, and that
these check, are that a Section's substance lives in its child nodes, that
an inserted provision shifts every node after it, and that a reprint
repaginates text it hasn't amended."""
from corpus.domain.diffing import (
    build_timeline,
    diff_versions,
    is_reordering,
    label,
    provisions,
    word_diff,
)

from conftest import make_node


def _act(*sections) -> list[dict]:
    """A tiny Act: each argument is (number, heading, [child texts...])."""
    nodes = [make_node("part", "1", "Preliminary")]
    for number, heading, children in sections:
        nodes.append(make_node("section", number, heading, ""))
        for i, text in enumerate(children, start=1):
            nodes.append(make_node("subsection", str(i), None, text))
    return nodes


def ops(segments):
    return [(s["op"], s["text"]) for s in segments if s["op"] != "equal"]


# ---------------------------------------------------------------------
# Word-level diff
# ---------------------------------------------------------------------

def test_identical_text_is_all_equal():
    assert ops(word_diff("the accused must appear", "the accused must appear")) == []


def test_an_insertion_is_reported_as_the_words_added():
    segments = word_diff("a trial for a sexual offence", "a trial for a sexual offence or a family violence offence")

    assert ops(segments) == [("insert", "or a family violence offence")]


def test_a_deletion_is_reported_as_the_words_removed():
    segments = word_diff("a child under the age of 16 years", "a child under 16 years")

    assert ops(segments) == [("delete", "the age of")]


def test_a_replacement_reads_as_its_deletion_then_its_insertion():
    """Legislative amendment is overwhelmingly "omit X, insert Y", and
    showing that as the struck-out phrase beside its replacement is how
    the amending Act's own words describe it."""
    segments = word_diff("referred to in paragraph (a)", "referred to in paragraphs (a) to (d)")

    # "(a)" survives in both, so the insertion lands either side of it.
    assert ops(segments) == [("delete", "paragraph"), ("insert", "paragraphs"), ("insert", "to (d)")]


def test_a_run_of_changed_words_is_one_segment_not_one_per_word():
    # A rendered diff should be a handful of spans, not thirty.
    segments = word_diff("the court may make an order", "the tribunal shall issue a direction")

    assert len([s for s in segments if s["op"] == "insert"]) == 1


def test_the_segments_reconstruct_both_texts():
    old, new = "the accused must appear in person", "the accused may appear by video"
    segments = word_diff(old, new)

    assert " ".join(s["text"] for s in segments if s["op"] in ("equal", "delete")) == old
    assert " ".join(s["text"] for s in segments if s["op"] in ("equal", "insert")) == new


# ---------------------------------------------------------------------
# Finding the provisions to compare
# ---------------------------------------------------------------------

def test_a_provision_is_a_section_and_everything_under_it():
    """A Section's own text is frequently a lead-in or nothing at all,
    with the substance in its subsections -- comparing only the Section's
    own text would report almost every real amendment as no change."""
    found = provisions(_act(("1", "Purposes", ["The purposes are—", "to consolidate the law."])))
    section = next(p for p in found.values() if p["kind"] == "provision")

    assert section["text"] == "The purposes are— to consolidate the law."


def test_a_containers_own_amendment_is_a_provision_too():
    # "Ch. 8 Pt 8.2 Div. 5 (Heading) amended by No. 19/2017 s. 56" is a
    # real amendment the Act records in its own margin.
    nodes = [make_node("part", "8.2", "Appeals"), make_node("section", "1", "A section", "text")]
    found = provisions(nodes)

    assert [label(p) for p in found.values()] == ["Part 8.2", "section 1"]


def test_a_provision_needs_a_number_of_its_own():
    """Excludes bare topical headings and the synthetic node an Act's
    front matter lands in -- that one carries the version number and the
    as-at date, so it differs between every pair of versions without
    anything in the Act having changed."""
    nodes = [
        make_node("part", None, "Preliminary", "Authorised Version incorporating amendments as at 1 May 2026"),
        make_node("heading_group", None, "Authorised Version No. 113"),
        make_node("section", "1", "Purposes", "text"),
    ]
    found = provisions(nodes)

    assert [label(p) for p in found.values()] == ["section 1"]


def test_a_schedules_clause_is_not_confused_with_the_body_section():
    nodes = [
        make_node("section", "11", "Place of hearing", "In the body."),
        make_node("schedule", "1", "Charges"),
        make_node("section", "11", "Statement of intent", "In the Schedule."),
    ]
    found = provisions(nodes)

    assert sorted(label(p) for p in found.values()) == [
        "Schedule 1", "Schedule 1 clause 11", "section 11",
    ]


def test_a_schedule_is_not_read_as_a_clause_of_itself():
    found = provisions([make_node("schedule", "3", "Persons who may witness", "1 A police officer.")])

    assert [label(p) for p in found.values()] == ["Schedule 3"]


def test_a_part_and_a_section_sharing_a_number_are_different_provisions():
    nodes = [make_node("part", "2", "Commencing"), make_node("section", "2", "Commencement", "text")]

    assert len(provisions(nodes)) == 2


# ---------------------------------------------------------------------
# Comparing two versions
# ---------------------------------------------------------------------

def test_an_unamended_provision_is_unchanged():
    old = _act(("1", "Purposes", ["The purposes are to consolidate the law."]))
    new = _act(("1", "Purposes", ["The purposes are to consolidate the law."]))

    result = diff_versions(old, new)

    assert result["unchanged"] == 2  # the Part, and section 1
    assert result["changed"] == []


def test_repagination_is_not_an_amendment():
    """A reprint wraps the same sentence at different points, and those
    wrap points are in the stored text as literal newlines. Comparing raw
    text reported a provision as amended because the page got a line
    longer."""
    old = _act(("1", "Purposes", ["The purposes of\nthis Act are to\nconsolidate the law."]))
    new = _act(("1", "Purposes", ["The purposes\nof this Act\nare to consolidate the law."]))

    assert diff_versions(old, new)["changed"] == []


def test_an_amended_provision_is_reported_with_its_diff():
    old = _act(("1", "Purposes", ["The purposes are to consolidate the law."]))
    new = _act(("1", "Purposes", ["The purposes are to consolidate and simplify the law."]))

    changed = diff_versions(old, new)["changed"]

    assert [label(p) for p in changed] == ["section 1"]
    assert ops(changed[0]["diff"]) == [("insert", "and simplify")]
    assert changed[0]["old_text"] == "The purposes are to consolidate the law."


def test_a_changed_heading_counts_as_a_change():
    old = _act(("1", "Purposes", ["text"]))
    new = _act(("1", "Purposes and objects", ["text"]))

    changed = diff_versions(old, new)["changed"]

    assert changed[0]["old_heading"] == "Purposes"
    assert changed[0]["heading"] == "Purposes and objects"


def test_an_inserted_provision_does_not_report_the_rest_of_the_act_as_rewritten():
    """The reason provisions are matched by identity and never by
    position: inserting section 26A shifts every node after it, so a
    positional comparison reports the whole remainder of the Act as
    changed."""
    old = _act(("26", "Notice", ["Notice text."]), ("27", "Service", ["Service text."]))
    new = _act(("26", "Notice", ["Notice text."]),
               ("26A", "New provision", ["Brand new."]),
               ("27", "Service", ["Service text."]))

    result = diff_versions(old, new)

    assert [label(p) for p in result["inserted"]] == ["section 26A"]
    assert result["changed"] == []
    assert result["unchanged"] == 3  # the Part, s 26, s 27


def test_a_provision_that_has_gone_is_reported_as_repealed():
    old = _act(("26", "Notice", ["text"]), ("27", "Service", ["text"]))
    new = _act(("26", "Notice", ["text"]))

    result = diff_versions(old, new)

    assert [label(p) for p in result["repealed"]] == ["section 27"]


def test_provisions_are_reported_in_the_order_the_act_prints_them():
    """The Crimes Act prints s 464, then 464AA, then 464AAB, then 464A --
    an order no rule read off the number reproduces. Ordering is taken
    from where each provision sits in the document, which already knows."""
    printed = ("464", "464AA", "464AAB", "464A", "464B")
    old = _act(*[(n, "H", ["before"]) for n in printed])
    new = _act(*[(n, "H", ["after"]) for n in printed])

    assert [p["number"] for p in diff_versions(old, new)["changed"]] == list(printed)


# ---------------------------------------------------------------------
# A provision's timeline across every version
# ---------------------------------------------------------------------

def _version(n, nodes):
    return {"version": n, "as_at": f"2026-0{n - 109}-01", "as_at_printed": f"1 month{n} 2026", "nodes": nodes}


def test_a_provision_that_never_changed_has_no_timeline():
    """What lets the browse view show a timeline only where there is one,
    rather than a control on every provision that mostly says nothing
    happened."""
    steady = _act(("1", "Purposes", ["Unchanged throughout."]))
    timeline = build_timeline([_version(110, steady), _version(111, steady), _version(112, steady)])

    assert timeline == {}


def test_a_timeline_records_each_version_the_provision_changed_at():
    v110 = _act(("1", "Purposes", ["First wording."]))
    v111 = _act(("1", "Purposes", ["Second wording."]))
    v112 = _act(("1", "Purposes", ["Second wording."]))   # untouched this time
    v113 = _act(("1", "Purposes", ["Third wording."]))

    timeline = build_timeline([_version(n, v) for n, v in
                               ((110, v110), (111, v111), (112, v112), (113, v113))])

    entries = next(iter(timeline.values()))
    assert [(e["version"], e["change"]) for e in entries] == [(111, "changed"), (113, "changed")]
    assert ops(entries[0]["diff"]) == [("delete", "First"), ("insert", "Second")]


def test_a_timeline_is_built_in_version_order_however_the_versions_arrive():
    v110 = _act(("1", "Purposes", ["First."]))
    v111 = _act(("1", "Purposes", ["Second."]))
    v112 = _act(("1", "Purposes", ["Third."]))

    timeline = build_timeline([_version(112, v112), _version(110, v110), _version(111, v111)])

    assert [e["version"] for e in next(iter(timeline.values()))] == [111, 112]


def test_an_insertion_opens_a_provisions_timeline():
    timeline = build_timeline([
        _version(110, _act(("1", "Purposes", ["text"]))),
        _version(111, _act(("1", "Purposes", ["text"]), ("1A", "New", ["brand new"]))),
    ])

    inserted = [e for entries in timeline.values() for e in entries]
    assert [(e["change"], e["number"]) for e in inserted] == [("inserted", "1A")]


# ---------------------------------------------------------------------------
# What a reprint changes that the Act did not
#
# Each of these came out of comparing the five Authorised Versions of the
# Criminal Procedure Act actually held here. Before they were handled, 32
# of the 48 entries in that Act's timeline were artefacts of extraction --
# a timeline two thirds noise is worse than no timeline, because a reader
# who finds the first three entries say nothing stops opening them.
# ---------------------------------------------------------------------------


def test_a_second_space_is_not_an_amendment():
    """PyMuPDF put two spaces where the last reprint put one, in section 25
    of the Criminal Procedure Act at version 111. Comparing the characters
    made that an amendment; comparing the words does not."""
    v110 = _act(("25", "Failure to appear", ["the Magistrates' Court may issue a warrant."]))
    v111 = _act(("25", "Failure to appear", ["the Magistrates' Court may  issue a warrant."]))

    result = diff_versions(v110, v111)

    assert result["changed"] == []
    assert result["unchanged"] == 2


def test_a_line_that_wraps_differently_is_not_an_amendment():
    """The same sentence, broken across lines at a different point because
    the reprint repaginated."""
    v110 = _act(("25", "Failure to appear", ["the Magistrates' Court\nmay issue a warrant."]))
    v111 = _act(("25", "Failure to appear", ["the Magistrates' Court may issue\na warrant."]))

    assert diff_versions(v110, v111)["changed"] == []


def test_a_font_left_in_the_private_use_area_is_not_an_amendment():
    """Section 387M's example cites "Part 3.10 of the Evidence Act 2008".
    Version 110 embeds those four characters in a symbol font with no
    Unicode mapping and PyMuPDF hands back the raw code points; version 111
    prints the same words from a font that maps. Read as characters that is
    an amendment to a cross-reference, which is the kind of thing a reader
    would stop and check."""
    mangled = "Part \uf033\uf02e\uf031\uf030 of the Evidence Act 2008"
    v110 = _act(("387M", "Division is in addition", [mangled]))
    v111 = _act(("387M", "Division is in addition", ["Part 3.10 of the Evidence Act 2008"]))

    assert diff_versions(v110, v111)["changed"] == []


def test_the_same_words_in_a_different_order_are_set_aside_not_reported():
    """A table read column-first in one reprint and row-first in the next --
    section 7A of the Criminal Procedure Act, whose two-column table of
    former sexual offences comes out interleaved differently each time."""
    v110 = _act(("7A", "Time limits removed", ["An offence against a child under the age of 16 be available under"]))
    v111 = _act(("7A", "Time limits removed", ["An offence against a be available under child under the age of 16"]))

    result = diff_versions(v110, v111)

    assert result["changed"] == []
    assert [p["number"] for p in result["reordered"]] == ["7A"]


def test_a_reordering_stays_out_of_the_timeline():
    """Set aside, so the browse view offers a timeline only where there is
    something to read in it."""
    v110 = _act(("7A", "Time limits removed", ["alpha beta gamma"]))
    v111 = _act(("7A", "Time limits removed", ["gamma alpha beta"]))

    assert build_timeline([_version(110, v110), _version(111, v111)]) == {}


def test_a_reordering_that_also_changes_a_word_is_an_amendment():
    """The test is that the words are the same, not that they moved. One
    new word among them and it is an amendment, however much else shifted."""
    v110 = _act(("7A", "Time limits removed", ["alpha beta gamma"]))
    v111 = _act(("7A", "Time limits removed", ["gamma alpha beta delta"]))

    assert [p["number"] for p in diff_versions(v110, v111)["changed"]] == ["7A"]


def test_a_changed_heading_is_an_amendment_even_where_the_words_only_moved():
    """The Act's own margin notes record "(Heading) amended by No. 19/2017"
    as a change in its own right, so a heading is never set aside as a
    reordering of the body beneath it."""
    v110 = _act(("7A", "Time limits removed", ["alpha beta"]))
    v111 = _act(("7A", "Time limits abolished", ["beta alpha"]))

    result = diff_versions(v110, v111)

    assert [p["number"] for p in result["changed"]] == ["7A"]
    assert result["reordered"] == []


def test_is_reordering_needs_something_to_have_moved():
    """An all-equal diff is not a reordering; it is no change at all. Left
    to itself the multiset test would call two empty multisets equal."""
    assert not is_reordering(word_diff("alpha beta", "alpha beta"))
    assert is_reordering(word_diff("alpha beta", "beta alpha"))
    assert not is_reordering(word_diff("alpha beta", "alpha gamma"))


# ---------------------------------------------------------------------------
# The amending Act named alongside a change
#
# diffing can see that a provision's wording moved, but not by what: the
# Act's own margin notes say that, and a note present in the new version
# but not the old is the amendment this diff just found. Every one of the
# 13 changes in the Criminal Procedure Act's real timeline was corroborated
# by exactly such a note.
# ---------------------------------------------------------------------------


def _act_with_history(number, heading, children, history):
    nodes = [make_node("part", "1", "Preliminary")]
    nodes.append(make_node("section", number, heading, "", history=history))
    for i, text in enumerate(children, start=1):
        nodes.append(make_node("subsection", str(i), None, text))
    return nodes


def test_a_changed_provision_carries_the_note_that_is_new_since_last_version():
    old_history = [{"raw": "New s. 366 inserted by No. 68/2009 s. 50."}]
    new_history = old_history + [{"raw": "S. 366(1)(ac) inserted by No. 1/2026 s. 74."}]
    v110 = _act_with_history("366", "Application", ["alpha"], old_history)
    v111 = _act_with_history("366", "Application", ["alpha beta"], new_history)

    result = diff_versions(v110, v111)

    assert len(result["changed"]) == 1
    assert result["changed"][0]["new_history"] == ["S. 366(1)(ac) inserted by No. 1/2026 s. 74."]


def test_an_unchanged_note_is_not_repeated_as_new():
    history = [{"raw": "New s. 366 inserted by No. 68/2009 s. 50."}]
    v110 = _act_with_history("366", "Application", ["alpha"], history)
    v111 = _act_with_history("366", "Application", ["alpha beta"], history)

    result = diff_versions(v110, v111)

    assert result["changed"][0]["new_history"] == []


def test_an_inserted_provision_carries_its_whole_history_as_new():
    v110 = _act(("1", "Purposes", ["text"]))
    history = [{"raw": "New s. 1A inserted by No. 1/2026 s. 5."}]
    v111 = _act(("1", "Purposes", ["text"])) + _act_with_history("1A", "New", ["brand new"], history)[1:]

    result = diff_versions(v110, v111)

    inserted = next(p for p in result["inserted"] if p["number"] == "1A")
    assert inserted["new_history"] == ["New s. 1A inserted by No. 1/2026 s. 5."]
