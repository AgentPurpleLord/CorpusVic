"""Tests for ai_pipeline/profiles.py's YAML profile loading/validation."""
import pytest

from ai_pipeline import profiles


@pytest.fixture
def isolated_profiles_dir(tmp_path, monkeypatch):
    """Points PROFILES_DIR at a scratch directory so tests can write
    profile files without touching the real ai_pipeline/profiles/."""
    monkeypatch.setattr(profiles, "PROFILES_DIR", tmp_path)
    return tmp_path


def write_profile(directory, name, content, ext="yaml"):
    (directory / f"{name}.{ext}").write_text(content, encoding="utf-8")


def test_load_profile_none_returns_compiled_defaults():
    patterns = profiles.load_profile(None)
    assert set(patterns) == set(profiles.DEFAULT_PATTERNS)
    m = patterns["section"].match("1 Short title")
    assert m and m.group(1) == "1" and m.group(2) == "Short title"


def test_load_profile_missing_file_falls_back_to_defaults(isolated_profiles_dir):
    patterns = profiles.load_profile("no-such-act")
    assert patterns["part"].pattern == profiles.DEFAULT_PATTERNS["part"]


def test_load_profile_with_override_only_changes_that_key(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "part: '^Part\\s+([\\dA-Z.]+)\\s*[—–-]\\s*(.+)$'\n")
    patterns = profiles.load_profile("test-act")
    m = patterns["part"].match("Part 4.6A—Early committal for trial")
    assert m and m.group(1) == "4.6A" and m.group(2) == "Early committal for trial"
    # everything else is untouched
    assert patterns["division"].pattern == profiles.DEFAULT_PATTERNS["division"]


def test_load_profile_accepts_yml_extension_too(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "notes_marker: '^Endnotes?$'\n", ext="yml")
    patterns = profiles.load_profile("test-act")
    assert patterns["notes_marker"].match("Endnote")


def test_describe_profile_flags_overrides_vs_defaults(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "section: '^(\\d+)\\s+(.+)$'\n")
    rows = {key: (pattern, is_override) for key, pattern, is_override in profiles.describe_profile("test-act")}
    assert rows["section"][1] is True
    assert rows["section"][0] == r"^(\d+)\s+(.+)$"
    assert rows["division"][1] is False
    assert rows["division"][0] == profiles.DEFAULT_PATTERNS["division"]


def test_unknown_pattern_key_raises_with_valid_keys_listed(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "sub_divison: '^(\\d+)\\s+(.+)$'\n")
    with pytest.raises(profiles.ProfileError, match="unknown pattern key"):
        profiles.load_profile("test-act")


def test_invalid_regex_raises_naming_the_key(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "section: '^(\\d+[A-Za-z]*'\n")
    with pytest.raises(profiles.ProfileError, match='"section"'):
        profiles.load_profile("test-act")


def test_too_few_capture_groups_raises(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "section: '^(\\d+)$'\n")
    with pytest.raises(profiles.ProfileError, match="needs at least 2"):
        profiles.load_profile("test-act")


def test_non_mapping_yaml_raises(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "- just\n- a\n- list\n")
    with pytest.raises(profiles.ProfileError, match="mapping"):
        profiles.load_profile("test-act")


def test_invalid_yaml_syntax_raises(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "section: '^(\\d+)\\s+(.+)$\n  bad indent: [\n")
    with pytest.raises(profiles.ProfileError, match="invalid YAML"):
        profiles.load_profile("test-act")


def test_chapter_is_a_default_pattern():
    patterns = profiles.load_profile(None)
    m = patterns["chapter"].match("Chapter 2—Commencing a criminal proceeding")
    assert m and m.group(1) == "2" and m.group(2) == "Commencing a criminal proceeding"


def test_schedule_is_a_default_pattern_and_tolerates_a_doubled_dash():
    """Real Acts have been seen using both a single em-dash and a doubled
    en-dash as the Schedule heading's separator (Criminal Procedure Act
    Schedules 1-4 use "––", Schedule 5 uses "—") -- the pattern needs "+"
    rather than the single-char class Chapter/Part/Division use, so the
    title comes out clean either way."""
    patterns = profiles.load_profile(None)
    m = patterns["schedule"].match("Schedule 1––Charges on a charge-sheet or indictment")
    assert m and m.group(1) == "1" and m.group(2) == "Charges on a charge-sheet or indictment"
    m2 = patterns["schedule"].match("Schedule 5—Transitional provisions")
    assert m2 and m2.group(1) == "5" and m2.group(2) == "Transitional provisions"


def test_sub_subparagraph_is_a_default_pattern():
    patterns = profiles.load_profile(None)
    m = patterns["sub_subparagraph"].match("(A) records of any medical examination")
    assert m and m.group(1) == "A"
    # Case alone must keep it from also matching a lowercase paragraph/
    # subparagraph marker.
    assert not patterns["sub_subparagraph"].match("(a) a lowercase paragraph")


def test_example_marker_is_a_default_pattern():
    patterns = profiles.load_profile(None)
    assert patterns["example_marker"].match("Example")
    assert patterns["example_marker"].match("Examples")
    assert not patterns["example_marker"].match("Example to s. 4")


# --- hierarchy: resolution --------------------------------------------------

def test_load_hierarchy_defaults_when_no_profile():
    assert profiles.load_hierarchy(None) == profiles.HIERARCHY_ORDER
    assert profiles.load_hierarchy(None)[0] == "schedule"


def test_load_hierarchy_defaults_when_profile_omits_the_key(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "part: '^Part\\s+(\\S+)\\s*[—–-]\\s*(.+)$'\n")
    assert profiles.load_hierarchy("test-act") == profiles.HIERARCHY_ORDER


def test_hierarchy_override_reorders_levels(isolated_profiles_dir):
    write_profile(
        isolated_profiles_dir, "test-act",
        "hierarchy: [part, division, section, subsection, paragraph, subparagraph]\n",
    )
    assert profiles.load_hierarchy("test-act") == ["part", "division", "section", "subsection", "paragraph", "subparagraph"]


def test_hierarchy_key_is_not_treated_as_a_pattern(isolated_profiles_dir):
    write_profile(
        isolated_profiles_dir, "test-act",
        "hierarchy: [chapter, part, division, subdivision, section, subsection, paragraph, subparagraph]\n"
        "part: '^PART\\s+(\\S+)\\s*[—–-]\\s*(.+)$'\n",
    )
    patterns = profiles.load_profile("test-act")  # must not raise "unknown pattern key: hierarchy"
    assert patterns["part"].match("PART 3—Sentencing")
    rows = {k: is_override for k, _p, is_override in profiles.describe_profile("test-act")}
    assert rows["part"] is True and rows["division"] is False


def test_hierarchy_missing_section_raises(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "hierarchy: [part, division, subsection, paragraph, subparagraph]\n")
    with pytest.raises(profiles.ProfileError, match="section"):
        profiles.load_hierarchy("test-act")


def test_hierarchy_missing_bracket_level_raises(isolated_profiles_dir):
    write_profile(isolated_profiles_dir, "test-act", "hierarchy: [chapter, part, section, subsection, paragraph]\n")
    with pytest.raises(profiles.ProfileError, match="subparagraph"):
        profiles.load_hierarchy("test-act")


def test_hierarchy_level_without_a_pattern_raises(isolated_profiles_dir):
    write_profile(
        isolated_profiles_dir, "test-act",
        "hierarchy: [book, part, division, subdivision, section, subsection, paragraph, subparagraph]\n",
    )
    with pytest.raises(profiles.ProfileError, match="no matching pattern.*book"):
        profiles.load_hierarchy("test-act")


def test_hierarchy_new_level_with_its_own_pattern_is_accepted(isolated_profiles_dir):
    write_profile(
        isolated_profiles_dir, "test-act",
        "hierarchy: [chapter, part, division, subdivision, section, subsection, paragraph, subparagraph]\n"
        "chapter: '^CHAPTER\\s+(\\d+)\\s*[—–-]\\s*(.+)$'\n",
    )
    assert profiles.load_hierarchy("test-act")[0] == "chapter"
    assert profiles.load_profile("test-act")["chapter"].match("CHAPTER 5—Trial")


def test_hierarchy_duplicate_level_raises(isolated_profiles_dir):
    write_profile(
        isolated_profiles_dir, "test-act",
        "hierarchy: [part, part, section, subsection, paragraph, subparagraph]\n",
    )
    with pytest.raises(profiles.ProfileError, match="duplicate"):
        profiles.load_hierarchy("test-act")


def test_real_criminal_procedure_act_profile_overrides_part_pattern():
    """The actual committed profile (ai_pipeline/profiles/criminal-procedure-act.yaml)
    fixes a real gap: the default "part" pattern can't match this Act's
    dotted Part numbering ("Part 5.1", "Part 4.6A"), so every Part-level
    heading fell through to a generic heading_group node instead."""
    patterns = profiles.load_profile("criminal-procedure-act")
    m = patterns["part"].match("Part 2.1—Ways in which a criminal proceeding is commenced")
    assert m is not None
    assert m.group(1) == "2.1"
    assert m.group(2) == "Ways in which a criminal proceeding is commenced"
