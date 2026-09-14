"""Tests for tree.py's annotate_paths -- the per-node path breadcrumb that
review.py's compute_unit_labels, akn_export.py, and history-note
attachment (attach_history) all read back to know what a node nests
under."""
from corpus.hierarchy import HIERARCHY_ORDER
from corpus.extract import PageText
from corpus.tree import annotate_paths, attach_history

from conftest import make_node


def _page_with_notes(notes: list[str], page_no: int = 1) -> PageText:
    """A page carrying only margin notes -- attach_history reads nothing
    else off the page, so the body can stay empty."""
    return PageText(page_no=page_no, body="", margin_notes=list(notes))


def test_annotate_paths_tracks_the_current_number_at_each_level():
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "1", "Murder"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[3]["path"]["part"] == "I"
    assert nodes[3]["path"]["section"] == "1"
    assert nodes[3]["path"]["subsection"] == "1"
    assert nodes[3]["path"]["paragraph"] == "a"


def test_annotate_paths_resets_deeper_levels_on_a_new_section():
    nodes = [
        make_node("section", "1", "Murder"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
        make_node("section", "2", "Manslaughter"),
        make_node("subsection", "1", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[4]["path"]["section"] == "2"
    assert nodes[4]["path"]["paragraph"] is None  # not still "a" from section 1


def test_annotate_paths_uses_the_heading_for_a_definition_not_a_number():
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("definition", None, "injury"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[2]["path"]["definition"] == "injury"
    # a definition has no number of its own to occupy the subsection slot
    assert nodes[2]["path"]["subsection"] is None


def test_annotate_paths_does_not_leak_a_definition_into_a_later_section():
    """Regression: path["definition"] used to have no reset rule of its
    own (see annotate_paths's own docstring) because "definition" is
    aliased onto subsection's rank rather than holding a literal slot in
    HIERARCHY_ORDER, so the deeper-levels reset loop never named it. A
    Definitions section anywhere in the Act would leak its last term into
    every later section's own subsections/paragraphs for the rest of the
    document."""
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("definition", None, "injury"),
        make_node("section", "4", "Meaning of consent"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[3]["path"]["definition"] is None
    assert nodes[4]["path"]["definition"] is None


def test_annotate_paths_does_not_leak_a_definition_into_a_later_subsection_in_the_same_section():
    # A section that opens with some definitions and later switches to
    # genuinely numbered subsections (rarer, but not impossible) -- the
    # numbered subsection's own paragraphs shouldn't still be attributed
    # to whichever term was last defined before it.
    nodes = [
        make_node("section", "3", "Definitions"),
        make_node("definition", None, "injury"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[3]["path"]["definition"] is None
    assert nodes[3]["path"]["subsection"] == "1"


def test_annotate_paths_respects_a_custom_hierarchy_order():
    custom = ["volume", *HIERARCHY_ORDER]
    nodes = [make_node("volume", "1", "Preliminary"), make_node("section", "1", "Purposes")]
    annotate_paths(nodes, custom)
    assert nodes[1]["path"]["volume"] == "1"


def test_annotate_paths_places_schedule_and_sub_subparagraph_by_default():
    """Both new default levels (see hierarchy.py) participate in the same
    path-tracking with no special-casing needed -- "schedule" resets
    every deeper level the same way "section" does, and
    "sub_subparagraph" is just one level past "subparagraph"."""
    nodes = [
        make_node("section", "1", "Purposes"),
        make_node("schedule", "1", "Forms"),
        make_node("section", "1", "Form of charge-sheet"),
        make_node("subsection", "1", None, "text"),
        make_node("paragraph", "a", None, "text"),
        make_node("subparagraph", "i", None, "text"),
        make_node("sub_subparagraph", "A", None, "text"),
    ]
    annotate_paths(nodes)
    assert nodes[1]["path"]["section"] is None  # the schedule closed out the earlier section
    assert nodes[2]["path"]["schedule"] == "1"
    assert nodes[6]["path"]["sub_subparagraph"] == "A"
    assert nodes[6]["path"]["subparagraph"] == "i"


def test_a_schedule_note_attaches_to_its_schedule():
    nodes = [
        make_node("section", "1", "Purposes", "text"),
        make_node("schedule", "2", "Forms"),
        make_node("section", "1", "A form", "text"),
    ]
    pages = [_page_with_notes(["Sch. 2 repealed by No. 6958 s. 8(4)(d)."])]

    unattached = attach_history(nodes, pages)

    assert unattached == []
    assert nodes[1]["history"][0]["raw"].startswith("Sch. 2 repealed")


def test_a_schedule_clause_note_does_not_land_on_the_body_section():
    # A Schedule numbers its own clauses from 1 again, so "Sch. 2 cl. 1"
    # and the body's own section 1 share a number.
    nodes = [
        make_node("section", "1", "Purposes", "text"),
        make_node("schedule", "2", "Forms"),
        make_node("section", "1", "A form", "text"),
    ]
    pages = [_page_with_notes(["Sch. 2 cl. 1 amended by No. 47/2016 s. 37."])]

    attach_history(nodes, pages)

    assert "history" not in nodes[0]
    assert nodes[2]["history"][0]["raw"].startswith("Sch. 2 cl. 1")


def test_a_chapter_note_attaches_to_its_chapter():
    nodes = [
        make_node("chapter", "10", "Repeals"),
        make_node("section", "439", "Repeal", "text"),
    ]
    pages = [_page_with_notes(["Ch. 10 (Heading and s. 439) inserted by No. 68/2009 s. 55."])]

    unattached = attach_history(nodes, pages)

    assert unattached == []
    assert nodes[0]["history"][0]["raw"].startswith("Ch. 10")


def test_a_provenance_note_is_kept_but_never_attached():
    # It names section 15 of the *previous consolidation*, not a provision
    # of this Act -- there is nothing here for it to attach to.
    nodes = [make_node("section", "15", "Murder", "text")]
    pages = [_page_with_notes(["No. 6103 s. 15."])]

    unattached = attach_history(nodes, pages)

    assert "history" not in nodes[0]
    assert [n["kind"] for n in unattached] == ["provenance"]


# ---------------------------------------------------------------------
# "Note to s. 6(1) ..." is about the note, not about s 6(1)
# ---------------------------------------------------------------------

def _s6() -> list[dict]:
    """Criminal Procedure Act s 6(1): a subsection, its three paragraphs,
    and the two notes printed under the last of them. The notes follow
    paragraph (c), so that is where the parser puts them and what their
    path says -- which is why a citation naming only the subsection has
    to still reach them."""
    return [
        make_node("section", "6", "Commencement of a criminal proceeding"),
        make_node("subsection", "1", None, "A criminal proceeding is commenced—"),
        make_node("paragraph", "a", None, "by filing a charge-sheet..."),
        make_node("paragraph", "b", None, "if the accused is arrested..."),
        make_node("paragraph", "c", None, "if a summons is issued..."),
        make_node("note", "1", None, "A criminal proceeding against a child..."),
        make_node("note", "2", None, "In the case of a criminal proceeding..."),
        make_node("subsection", "2", None, "If a charge-sheet is filed..."),
    ]


def _attached(nodes: list[dict]) -> list[tuple]:
    """(type, number, confidence) for every note that found a home."""
    return [
        (n["type"], n.get("number"), h.get("confidence"))
        for n in nodes for h in n.get("history") or []
    ]


def test_a_note_citation_lands_on_the_note_not_the_provision():
    """It used to land on s 6(1). The citation records a change to the
    note printed under that subsection; the subsection itself still says
    what it always said, so putting the note's history on it credited the
    change to the wrong provision."""
    nodes = _s6()
    attach_history(nodes, [_page_with_notes(["Note to s. 6(1) substituted as Notes by No. 32/2024 s. 812."])])
    assert _attached(nodes) == [("note", "1", "low")]


def test_a_note_citation_that_cannot_say_which_note_is_low_confidence():
    """s 6(1) carries two notes and "Note to s. 6(1)" names neither, so
    the first is a guess -- flagged, not presented as known, so a
    reviewer can re-point it."""
    nodes = _s6()
    attach_history(nodes, [_page_with_notes(["Note to s. 6(1) amended by No. 1/2020 s. 2."])])
    assert nodes[5]["history"][0]["confidence"] == "low"


def test_a_lone_note_under_a_provision_is_not_a_guess():
    nodes = [
        make_node("section", "6", "Commencement"),
        make_node("subsection", "3", None, "A charge-sheet must—"),
        make_node("note", None, None, "Section 18 requires an informant..."),
    ]
    attach_history(nodes, [_page_with_notes(["Note to s. 6(3) amended by No. 1/2020 s. 2."])])
    assert _attached(nodes) == [("note", None, "high")]


def test_a_numbered_note_citation_picks_that_note():
    nodes = [
        make_node("section", "55", "Contest mention"),
        make_node("subsection", "4", None, "The accused must attend..."),
        make_node("note", "1", None, "Section 3 defines attend."),
        make_node("note", "2", None, "See section 334..."),
        make_node("note", "3", None, "Section 330 gives the court power..."),
    ]
    attach_history(nodes, [_page_with_notes(["Note 1 to s. 55(4) amended by No. 38/2016 s. 9(2)."])])
    assert _attached(nodes) == [("note", "1", "high")]


def test_a_bracket_can_name_a_paragraph_where_a_section_has_no_subsections():
    """Criminal Procedure Act s 119. Its paragraphs hang straight off the
    section, so "s. 119(c)" names a paragraph where "s. 6(1)" named a
    subsection. Reading the first bracket as a subsection either way put
    every note in s 119 out of reach of its own citation."""
    nodes = [
        make_node("section", "119", "Case direction notice"),
        make_node("paragraph", "b", None, "must specify the procedure..."),
        make_node("paragraph", "c", None, "must state the names of any witnesses..."),
        make_node("note", "1", None, "See section 123(1)..."),
        make_node("paragraph", "d", None, "must state, in respect of each issue..."),
    ]
    attach_history(nodes, [_page_with_notes(["Note 1 to s. 119(c) substituted by No. 48/2018 s. 22."])])
    assert _attached(nodes) == [("note", "1", "high")]


def test_an_example_citation_lands_on_the_example():
    nodes = [
        make_node("section", "43A", "Alternative offence"),
        make_node("subsection", "2", None, "The court may..."),
        make_node("example", None, None, "An example of the operation..."),
    ]
    attach_history(nodes, [_page_with_notes(["Example to s. 43A(2) amended by No. 47/2016 s. 10."])])
    assert _attached(nodes) == [("example", None, "high")]


def test_a_repealed_note_falls_back_to_the_provision():
    """"Note to s. 38(2) repealed" -- the note is gone, so there is
    nothing of its own left to attach to. The provision it was printed
    under is the nearest true anchor, and the fallback is marked as one
    rather than passed off as a match."""
    nodes = [
        make_node("section", "38", "Filing"),
        make_node("subsection", "2", None, "A charge-sheet may be filed..."),
    ]
    attach_history(nodes, [_page_with_notes(["Note to s. 38(2) repealed by No. 68/2009 s. 9(c)."])])
    assert _attached(nodes) == [("subsection", "2", "low")]


def test_an_ordinary_section_citation_still_lands_on_the_provision():
    """The narrowing applies only where the citation names a note. A
    plain "S. 6(1)(a) amended by ..." is about the paragraph itself."""
    nodes = _s6()
    attach_history(nodes, [_page_with_notes(["S. 6(1)(a) amended by No. 20/2025 s. 5."])])
    assert _attached(nodes) == [("paragraph", "a", "high")]
