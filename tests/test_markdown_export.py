"""Tests for ai_pipeline/markdown_export.py."""
import re

import yaml

from ai_pipeline.akn_export import build_hierarchy_tree
from ai_pipeline.markdown_export import (
    _github_slug,
    export_to_markdown,
    page_title,
)

from conftest import make_node

FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def front_matter(text: str) -> dict:
    m = FRONT_MATTER_RE.match(text)
    assert m, f"no front matter found at top of file:\n{text[:200]}"
    return yaml.safe_load(m.group(1))


def check_all_links_resolve(out_dir) -> None:
    """Every ](target) in every generated .md file must resolve to an
    existing file and, if fragmented, an existing header-derived slug in
    that file -- simulating GitHub's own slug algorithm independently
    against the actual rendered header lines, not the generator's internal
    state (a true black-box check)."""
    slug_strip_re = re.compile(r"[^\w\s-]")
    header_re = re.compile(r"^(#{1,6})\s+(.*)$")
    link_re = re.compile(r"\]\(([^)]+)\)")

    def slug_for(text, counts):
        s = text.strip().lower()
        s = slug_strip_re.sub("", s)
        s = re.sub(r"\s+", "-", s)
        if s not in counts:
            counts[s] = 0
            return s
        counts[s] += 1
        return f"{s}-{counts[s]}"

    def slugs_in_file(path):
        counts, slugs = {}, set()
        for line in path.read_text(encoding="utf-8").splitlines():
            m = header_re.match(line)
            if m:
                slugs.add(slug_for(m.group(2), counts))
        return slugs

    md_files = list(out_dir.rglob("*.md"))
    assert md_files, "no markdown files were written"
    slug_cache = {f: slugs_in_file(f) for f in md_files}
    for f in md_files:
        text = f.read_text(encoding="utf-8")
        assert "<a id=" not in text, f"{f} still uses raw <a id> anchors instead of real headers"
        for m in link_re.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, frag = target.partition("#")
            target_path = (f.parent / file_part).resolve() if file_part else f.resolve()
            assert target_path.exists(), f"{f}: broken file link -> {target}"
            if frag:
                assert frag in slug_cache.get(target_path, slugs_in_file(target_path)), f"{f}: broken fragment -> {target}"


def _tiny_act_nodes(verified_indices: set[int] | None = None, verified_at: str = "2026-08-30T09:00:00+00:00") -> list[dict]:
    """Part I > Division 1 > [Section 1 (2 subsections), heading_group,
    Section 2 (defines a term)]. verified_indices optionally stamps nodes
    at those positions in the returned list -- by index rather than
    (type, number), since Subsection numbers restart at "1" in every
    Section and would otherwise collide."""
    verified_indices = verified_indices or set()
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("division", "1", "Offences against the person"),
        make_node("section", "1", "Murder", ""),
        make_node("subsection", "1", None, "A person who kills another person commits murder."),
        make_node("subsection", "2", None, "The penalty is imprisonment for life."),
        make_node("heading_group", None, "Theft and related offences", "Theft and related offences"),
        make_node("section", "2", "Definitions", ""),
        make_node("subsection", "1", None, "weapon means any object capable of causing injury."),
    ]
    for i in verified_indices:
        nodes[i]["verified_at"] = verified_at
    return nodes


def test_github_slug_deduplicates():
    counts: dict[str, int] = {}
    assert _github_slug("Foo Bar", counts) == "foo-bar"
    assert _github_slug("Foo Bar", counts) == "foo-bar-1"
    assert _github_slug("Foo Bar", counts) == "foo-bar-2"


def test_heading_group_attaches_as_sibling_of_sections_not_nested_in_a_section():
    """Regression: a heading_group node is appended without going through
    the open_node/stack machinery real hierarchy levels use, so it used
    to attach to whatever Subsection/Section happened to still be open in
    the flat node list -- rendering as a stray heading buried at the tail
    of the *previous* Section's page instead of its own entry between
    Sections. build_hierarchy_tree must pop it back to Division level."""
    nodes = _tiny_act_nodes()
    tree_roots, _collisions = build_hierarchy_tree(nodes)

    def find_heading_group(tree_node):
        if tree_node["node"]["type"] == "heading_group":
            return tree_node
        for child in tree_node["children"]:
            found = find_heading_group(child)
            if found:
                return found
        return None

    part = tree_roots[0]
    division = part["children"][0]
    hg = find_heading_group(division)
    assert hg is not None, "heading_group not found anywhere in the tree"
    # It must be a direct child of the Division, not nested inside Section 1.
    assert hg in division["children"]
    section1 = next(c for c in division["children"] if c["node"].get("number") == "1")
    assert find_heading_group(section1) is None


def test_export_writes_valid_links_and_no_raw_anchors(tmp_path):
    parsed = {"nodes": _tiny_act_nodes(), "act": "test-act"}
    stats = export_to_markdown(parsed, str(tmp_path), act_title="Test Act 2026")
    assert stats["sections"] == 2
    check_all_links_resolve(tmp_path)


def test_front_matter_present_on_every_file_with_title_and_description(tmp_path):
    parsed = {"nodes": _tiny_act_nodes(), "act": "test-act"}
    export_to_markdown(parsed, str(tmp_path), act_title="Test Act 2026")

    index_fm = front_matter((tmp_path / "index.md").read_text(encoding="utf-8"))
    assert index_fm["title"] == "Test Act 2026"
    assert "description" in index_fm and index_fm["description"]

    for md_file in (tmp_path / "sections").glob("*.md"):
        fm = front_matter(md_file.read_text(encoding="utf-8"))
        assert fm["title"]
        assert fm["description"]


def test_front_matter_verification_rollup_full_partial_none(tmp_path):
    nodes = _tiny_act_nodes()
    # Section 1 (index 2) and both its Subsections (3, 4) -- fully verified,
    # each at a distinct timestamp to check the "latest wins" rollup.
    nodes[2]["verified_at"] = "2026-08-30T09:00:00+00:00"
    nodes[3]["verified_at"] = "2026-08-30T09:05:00+00:00"
    nodes[4]["verified_at"] = "2026-08-30T09:10:00+00:00"
    parsed = {"nodes": nodes, "act": "test-act"}
    export_to_markdown(parsed, str(tmp_path), act_title="Test Act 2026")

    s1 = front_matter((tmp_path / "sections" / "s1.md").read_text(encoding="utf-8"))
    assert s1["verified"] == "full"
    assert s1["verified_count"] == s1["total_count"] == 3
    assert s1["verified_at"] == "2026-08-30T09:10:00+00:00"  # the latest of the three

    s2 = front_matter((tmp_path / "sections" / "s2.md").read_text(encoding="utf-8"))
    assert s2["verified"] == "none"
    assert s2["verified_at"] is None
    assert s2["verified_count"] == 0

    index_fm = front_matter((tmp_path / "index.md").read_text(encoding="utf-8"))
    assert index_fm["verified"] == "partial"
    assert index_fm["verified_count"] == 3


def test_defined_term_cross_links_to_its_definitions_section(tmp_path):
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "2", "Definitions", ""),
        make_node("subsection", "1", None, "weapon means any object capable of causing injury."),
        make_node("section", "3", "Assault", ""),
        make_node("subsection", "1", None, "A person must not assault another person with a weapon."),
    ]
    parsed = {"nodes": nodes, "act": "test-act"}
    export_to_markdown(parsed, str(tmp_path), act_title="Test Act 2026")

    s3_text = (tmp_path / "sections" / "s3.md").read_text(encoding="utf-8")
    assert "[weapon](s2.md" in s3_text
    check_all_links_resolve(tmp_path)


def test_a_font_split_definition_node_still_cross_links_by_its_own_heading(tmp_path):
    """Regression: rule_parser.py's _try_definition_start splits a
    Definitions section's own bold+italic-led terms into dedicated
    "definition" nodes, with the term as that node's own heading and the
    body text starting straight at "means ..." -- no term left inline
    for extract_terms's text-pattern scan to find any more. The term
    must still resolve via the node's own heading instead."""
    nodes = [
        make_node("part", "I", "Offences"),
        make_node("section", "2", "Definitions", ""),
        make_node("definition", None, "weapon", "means any object capable of causing injury."),
        make_node("section", "3", "Assault", ""),
        make_node("subsection", "1", None, "A person must not assault another person with a weapon."),
    ]
    parsed = {"nodes": nodes, "act": "test-act"}
    export_to_markdown(parsed, str(tmp_path), act_title="Test Act 2026")

    s2_text = (tmp_path / "sections" / "s2.md").read_text(encoding="utf-8")
    assert "## weapon" in s2_text
    assert "means any object capable of causing injury." in s2_text
    s3_text = (tmp_path / "sections" / "s3.md").read_text(encoding="utf-8")
    assert "[weapon](s2.md" in s3_text
    check_all_links_resolve(tmp_path)


def test_a_bulleted_list_exports_as_a_markdown_list(tmp_path):
    # An Act letters every item it lists, so its paragraphs always have a
    # number to head them with. An Explanatory Memorandum bullets them
    # instead (see em_parser.py) -- rendered as bare paragraphs, those
    # lose the fact that they are a list at all.
    nodes = [
        make_node("clause", "28", None, "lists the offences that may be heard summarily—"),
        make_node("paragraph", None, None, "an offence referred to in Schedule 2;"),
        make_node("paragraph", None, None, "an indictable offence described as being—"),
        make_node("subparagraph", None, None, "a level 5 or 6 offence; or"),
        make_node("subparagraph", None, None, "punishable by a term of imprisonment."),
    ]
    export_to_markdown({"nodes": nodes, "hierarchy": None}, str(tmp_path), act_title="Test EM")
    body = (tmp_path / "sections" / "c28.md").read_text(encoding="utf-8")

    assert "- an offence referred to in Schedule 2;" in body
    assert "  - a level 5 or 6 offence; or" in body
    # The clause's own lead-in is prose, not an item in its own list.
    assert "- lists the offences" not in body


def test_a_lettered_list_still_exports_as_headed_paragraphs(tmp_path):
    # An Act's own paragraphs carry "(a)"/"(b)", which head them; turning
    # those into bullets would drop the letters the Act refers to them by.
    nodes = [
        make_node("section", "28", "Summary hearing", "A charge may be heard summarily if—"),
        make_node("paragraph", "a", None, "an offence referred to in Schedule 2;"),
    ]
    export_to_markdown({"nodes": nodes, "hierarchy": None}, str(tmp_path), act_title="Test Act")
    body = (tmp_path / "sections" / "s28.md").read_text(encoding="utf-8")

    assert "(a)" in body
    assert "- an offence referred to in Schedule 2;" not in body


# ---------------------------------------------------------------------------
# A Schedule whose content is unnumbered prose (see
# hierarchy.schedule_is_pageable) -- previously invisible in both the
# Markdown export and the browse view, since a Schedule got only a bare
# <h4>/heading above whatever numbered children it had, and one with none
# of its own had nowhere for its content to go at all.
# ---------------------------------------------------------------------------

def _act_with_prose_schedule() -> list[dict]:
    return [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("schedule", "3", "Persons who may witness statements",
                 "1 A police officer.\n2 A justice of the peace."),
    ]


def test_a_schedule_with_no_numbered_items_gets_its_own_page(tmp_path):
    export_to_markdown({"nodes": _act_with_prose_schedule()}, str(tmp_path), act_title="Test Act")

    files = {f.name: f.read_text(encoding="utf-8") for f in (tmp_path / "sections").glob("*.md")}
    schedule_files = [body for body in files.values() if "A police officer" in body]
    assert len(schedule_files) == 1
    body = schedule_files[0]
    assert "# Schedule 3 - Persons who may witness statements" in body
    assert "A justice of the peace" in body


def test_the_schedule_page_is_linked_from_the_index(tmp_path):
    export_to_markdown({"nodes": _act_with_prose_schedule()}, str(tmp_path), act_title="Test Act")

    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "Schedule 3 - Persons who may witness statements" in index
    # Linked as an ordinary list item, not left as a bare, childless heading.
    assert re.search(r"\[Schedule 3 - Persons who may witness statements[^]]*]\(sections/", index)


def test_a_schedule_whose_items_are_ordinary_sections_gets_no_page_of_its_own(tmp_path):
    # Schedule 1 of the Criminal Procedure Act, e.g.: its own numbered
    # items already reuse the Section node type and so already have pages
    # of their own -- giving the Schedule itself one too would be an
    # empty, redundant page.
    nodes = [
        make_node("part", "1", "Preliminary"),
        make_node("section", "1", "Purposes", "The purposes of this Act are—"),
        make_node("schedule", "1", "Charges on a charge-sheet"),
        make_node("section", "1", "Statement of offence", "A charge-sheet must state the offence."),
    ]
    export_to_markdown({"nodes": nodes}, str(tmp_path), act_title="Test Act")

    files = {f.name: f.read_text(encoding="utf-8") for f in (tmp_path / "sections").glob("*.md")}
    assert not any(body.strip().startswith("# Schedule 1") for body in files.values())
    assert any("A charge-sheet must state the offence." in body for body in files.values())


def test_page_title_spells_out_a_pageable_schedule():
    node = make_node("schedule", "3", "Persons who may witness statements")
    assert page_title(node) == "Schedule 3 - Persons who may witness statements"
