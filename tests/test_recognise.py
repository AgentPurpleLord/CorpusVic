"""The recognition rules, checked against real printed lines.

The Criminal Procedure Act s 97 lines below are the real thing, x0 and
all, read out of data/extracted/criminal-procedure-act-v114.json. Two of
them were read as the wrong type before recognition rules existed: "(c)"
came out a subparagraph, and "(iv)" a paragraph.
"""
import pytest

from corpus.domain.node import NodeType, NodeTypeRegistry, Recognition
from corpus.domain.ruleset import RulesetError, build_registry, nesting_gap
from corpus.parsing.extract import BodyLine
from corpus.parsing.recognise import Context, best, recognise, weigh


def line(text, x0=216.2, *, size=12.0, bold=False, bold_italic=None):
    return BodyLine(text=text, x0=x0, x1=x0 + 200, y0=0.0, y1=12.0, page_no=1,
                    size=size, bold=bold, leading_bold_italic=bold_italic)


def ctx(*open_above, body_size=12.0, prev_text="", prev_was_heading=False):
    return Context(
        body_size=body_size,
        open_types=tuple(t for t, _ in open_above),
        open_x0=tuple(x for _, x in open_above),
        prev_text=prev_text,
        prev_was_heading=prev_was_heading,
    )


@pytest.fixture
def registry():
    return build_registry()


# --- conditions, one at a time -------------------------------------------

def test_a_rule_with_no_conditions_matches_anything():
    reg = NodeTypeRegistry()
    reg.register(NodeType(id="anything", name="Anything", label="x", recognition=Recognition()))
    assert best(line("whatever"), ctx(), reg).type_id == "anything"


def test_a_type_with_no_rule_is_never_considered(registry):
    registry.register(NodeType(id="ruleless", name="Ruleless", label="x"))
    assert "ruleless" not in {v.type_id for v in recognise(line("(a) text"), ctx(), registry)}


def test_not_bold_rules_out_a_bold_line():
    plain = NodeType(id="plain", name="Plain", label="x",
                     recognition=Recognition(not_bold=True))
    assert weigh(plain, line("text"), ctx()).matched
    assert weigh(plain, line("text", bold=True), ctx()).failed.name == "not_bold"


def test_size_is_judged_against_this_documents_body_size(registry):
    # A 16pt Part heading in a 12pt print, and the same heading in a 10pt
    # print -- one rule, because the condition is a ratio.
    for body, size in ((12.0, 16.0), (10.0, 13.3)):
        heading = line("Part 3—Committal proceeding", x0=71.0, size=size, bold=True)
        assert best(heading, ctx(body_size=body), registry).type_id == "part"


def test_a_part_heading_at_body_size_is_not_a_part(registry):
    # "Part 3—..." printed inline at body size is a cross-reference.
    verdict = weigh(registry.get("part"), line("Part 3—Committal proceeding", x0=71.0, bold=True), ctx())
    assert verdict.failed.name == "min_size_ratio"


def test_the_failing_condition_is_reported(registry):
    verdict = weigh(registry.get("subparagraph"), line("(i) text", x0=243.8),
                    ctx(("section", 163.7)))
    assert verdict.failed.name == "parent_types"
    assert "needs one of ['paragraph']" in verdict.failed.detail


# --- what "indented once from" means -------------------------------------

def test_a_sibling_at_the_same_indent_is_not_a_parent():
    # "(c)" follows "(a)" at the same x0, so it belongs where "(a)" did.
    enclosing = ctx(("section", 163.7), ("paragraph", 216.2)).enclosing(216.2)
    assert enclosing == ("section", 163.7)


def test_marker_width_does_not_make_a_level():
    # "(iv)" is set six points left of its sibling "(i)" so their text
    # aligns. Six points is not a level.
    enclosing = ctx(("paragraph", 215.5), ("subparagraph", 243.8)).enclosing(237.8)
    assert enclosing == ("paragraph", 215.5)


def test_a_real_level_is_a_parent():
    enclosing = ctx(("section", 163.7), ("subsection", 190.1)).enclosing(216.2)
    assert enclosing == ("subsection", 190.1)


def test_nothing_open_cannot_rule_a_type_out(registry):
    verdict = weigh(registry.get("paragraph"), line("(a) text"), ctx())
    assert verdict.matched


# --- Criminal Procedure Act s 97, line by line ---------------------------

# The printed lines, their measured x0, and what the drafter meant. The
# stack is what is open when the parser reaches each line.
S97 = [
    ("97 Purposes of a committal proceeding", 163.7, True, [], "section"),
    ("(a) to determine whether a charge for an offence", 216.2, False,
     [("section", 163.7)], "paragraph"),
    ("(c) to determine how the accused proposes to", 216.2, False,
     [("section", 163.7), ("paragraph", 216.2)], "paragraph"),
    ("(d) to ensure a fair trial, if the matter proceeds to", 215.5, False,
     [("section", 163.7), ("paragraph", 216.2)], "paragraph"),
    ("(i) ensuring that the prosecution case", 243.8, False,
     [("section", 163.7), ("paragraph", 215.5)], "subparagraph"),
    ("(ii) enabling the accused to hear or read the", 240.5, False,
     [("section", 163.7), ("paragraph", 215.5), ("subparagraph", 243.8)], "subparagraph"),
    ("(iv) enabling the accused to adequately", 237.8, False,
     [("section", 163.7), ("paragraph", 215.5), ("subparagraph", 240.5)], "subparagraph"),
    ("(v) enabling the issues in contention to be", 241.1, False,
     [("section", 163.7), ("paragraph", 215.5), ("subparagraph", 237.8)], "subparagraph"),
]


@pytest.mark.parametrize("text,x0,bold,stack,expected", S97, ids=[row[0][:12] for row in S97])
def test_section_97_reads_as_drafted(registry, text, x0, bold, stack, expected):
    verdict = best(line(text, x0=x0, bold=bold),
                   ctx(*stack, prev_was_heading=True), registry)
    assert verdict is not None, f"{text!r} matched no type"
    assert verdict.type_id == expected


def test_section_97_numbers_come_out_whole(registry):
    verdict = best(line("(iv) enabling the accused to adequately", x0=237.8),
                   ctx(("section", 163.7), ("paragraph", 215.5), ("subparagraph", 240.5)),
                   registry)
    assert verdict.number == "iv"


# --- the rule files ------------------------------------------------------

def test_the_base_ruleset_covers_the_hierarchy(registry):
    for level in ("schedule", "chapter", "part", "division", "section",
                  "subsection", "paragraph", "subparagraph", "sub_subparagraph"):
        assert registry.exists(level), level
        assert registry.get(level).recognition is not None


def test_every_pattern_captures_number_and_heading(registry):
    import re
    for node_type in registry.all():
        pattern = node_type.recognition.pattern
        if pattern:
            assert re.compile(pattern).groups >= 2, node_type.id


def test_subparagraph_is_tried_before_paragraph(registry):
    # Both patterns match a bare "(i)". Order is what settles it.
    assert registry.get("subparagraph").priority < registry.get("paragraph").priority


def test_an_unknown_condition_names_itself(tmp_path, monkeypatch):
    import corpus.domain.ruleset as ruleset
    monkeypatch.setattr(ruleset, "RULES_DIR", tmp_path)
    (tmp_path / "bad.yaml").write_text("types:\n  section:\n    recognition:\n      boldish: true\n")
    with pytest.raises(RulesetError, match="boldish"):
        ruleset.build_registry(base="bad")


def test_a_broken_pattern_names_its_type(tmp_path, monkeypatch):
    import corpus.domain.ruleset as ruleset
    monkeypatch.setattr(ruleset, "RULES_DIR", tmp_path)
    (tmp_path / "bad.yaml").write_text("types:\n  section:\n    recognition:\n      pattern: '([unclosed'\n")
    with pytest.raises(RulesetError, match="section"):
        ruleset.build_registry(base="bad")


def test_a_per_act_file_overrides_one_condition(tmp_path, monkeypatch):
    import corpus.domain.ruleset as ruleset
    monkeypatch.setattr(ruleset, "RULES_DIR", tmp_path)
    (tmp_path / "base.yaml").write_text(
        "types:\n  section:\n    name: Section\n    label: s\n"
        "    recognition:\n      pattern: '^(\\d+)\\s+(.+)$'\n      bold: true\n")
    (tmp_path / "odd-act.yaml").write_text(
        "types:\n  section:\n    recognition:\n      bold: false\n")
    section = ruleset.build_registry("odd-act", base="base").get("section")
    assert section.recognition.bold is False
    # Everything the Act did not mention is still the base rule.
    assert section.recognition.pattern == r"^(\d+)\s+(.+)$"
    assert section.name == "Section"


def test_an_unknown_ruleset_lists_what_exists(tmp_path, monkeypatch):
    import corpus.domain.ruleset as ruleset
    monkeypatch.setattr(ruleset, "RULES_DIR", tmp_path)
    (tmp_path / "victorian-act.yaml").write_text("types: {}\n")
    with pytest.raises(RulesetError, match="victorian-act"):
        ruleset.build_registry(base="nonesuch")


def test_the_nesting_gap_is_smaller_than_a_level_and_larger_than_jitter():
    # Levels sit ~26pt apart; sibling markers wobble by ~6pt.
    assert 6.0 < nesting_gap() < 26.0


# --- Crimes Act s 21A(2), the long lettered run --------------------------

# A paragraph run that goes (a) (b) (ba) (bb) (bc) (c) (d) (da) (dab) (db)
# (dc). Every one of them is a paragraph of subsection (2); the wide
# markers are set further left so their text still lines up, which is
# what made "(dab)" look like a level deeper than "(da)".
S21A = [
    ("(da) making threats to B;", 216.3, "paragraph"),
    ("(dab) causing or threatening to cause harm to any", 204.3, "paragraph"),
    ("(db) using abusive or offensive words to or in the", 209.7, "paragraph"),
    ("(dc) performing abusive or offensive acts in the", 210.3, "paragraph"),
]


@pytest.mark.parametrize("text,x0,expected", S21A, ids=[row[0][:6] for row in S21A])
def test_a_wide_marker_is_not_a_deeper_level(registry, text, x0, expected):
    stack = [("section", 163.7), ("subsection", 190.2), ("paragraph", 216.3)]
    assert best(line(text, x0=x0), ctx(*stack), registry).type_id == expected


def test_subparagraphs_under_that_run_are_still_subparagraphs(registry):
    stack = [("section", 163.7), ("subsection", 190.2), ("paragraph", 204.3)]
    verdict = best(line("(i) while in the presence of B or any other person; or", x0=243.9),
                   ctx(*stack), registry)
    assert verdict.type_id == "subparagraph"


# --- centring ------------------------------------------------------------

def test_a_centred_bracketed_title_is_a_subdivision(registry):
    # "(4) Offences against the person", centred across a 595pt page.
    heading = line("(4) Offences against the person", x0=220.4, size=12.0, bold=True)
    heading = BodyLine(**{**heading.__dict__, "x1": 595.0 - 220.4})
    assert best(heading, Context(page_width=595.0), registry).type_id == "subdivision"


def test_a_bold_subsection_at_the_left_indent_is_not_a_subdivision(registry):
    # Bold because its longest run of characters is a bold italic defined
    # term, not because the line is a heading.
    text = line("(4) In this section, emergency service vehicle means a", x0=190.2, bold=True)
    verdict = best(text, Context(page_width=595.0), registry)
    assert verdict.type_id == "subsection"


def test_weight_alone_never_rules_a_provision_out(registry):
    for level in ("subsection", "paragraph", "subparagraph", "sub_subparagraph"):
        rule = registry.get(level).recognition
        assert rule.not_bold is None, f"{level} checks weight, which is the dominant span's"
        assert rule.bold is None, level


# --- explaining one line -------------------------------------------------

def test_explain_works_for_an_act_with_no_pattern_profile(monkeypatch, capsys):
    """Recognition rules have a base of their own, so an Act that has
    never needed a pattern override still explains."""
    from corpus.parsing import show_profile

    monkeypatch.setattr("sys.argv", ["show_profile", "no-such-act", "--explain", "(a) a thing;"])
    show_profile.main()
    printed = capsys.readouterr().out
    assert "defaults throughout" in printed
    assert "paragraph          MATCH" in printed


def test_explain_says_when_the_line_was_not_found(monkeypatch, capsys):
    from corpus.parsing import show_profile

    monkeypatch.setattr("sys.argv", ["show_profile", "no-such-act", "--explain", "(a) a thing;"])
    show_profile.main()
    assert "line not found" in capsys.readouterr().out


def test_a_profile_that_does_not_exist_is_still_an_error_without_explain(monkeypatch):
    from corpus.parsing import show_profile

    monkeypatch.setattr("sys.argv", ["show_profile", "no-such-act"])
    with pytest.raises(SystemExit):
        show_profile.main()
