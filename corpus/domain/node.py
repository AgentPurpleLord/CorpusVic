"""What a node type is, and how a printed line is recognised as one.

Victorian Acts are typeset to a house style, and that style is the
signal. Per domain.md: a Section carries a bolded number and heading; a
subsection is "(1)", normal font, indented once from the section; a
paragraph is "(a)", indented once again; a defined term is set in bold
italics. Every one of those is something a reader can see on the page and
a parser can measure.

Recognition below writes those sentences down as conditions, so changing
what the parser looks for means editing a rule rather than editing code.
"""
from dataclasses import dataclass, field


# How much further right a line must start before it counts as a level
# deeper, rather than as another item at the same level.
#
# It cannot be a hair's breadth. Victorian Acts set a bracketed marker in
# a hanging indent, with the body text aligned and the marker pushed left
# to fit, so siblings start at visibly different places: in s 97 of the
# Criminal Procedure Act, "(i)" begins at 243.8 and its own sibling "(iv)"
# at 237.8, six points to its left. A full level, by contrast, is about
# 26 points -- subsections near 188, paragraphs near 216, subparagraphs
# near 243. Half a level sits well clear of both.
#
# Measured on 12pt prints. An Act typeset to another measure sets its own
# value with `nesting_gap:` in its recognition rules.
DEFAULT_NESTING_GAP = 13.0


@dataclass(frozen=True)
class Recognition:
    """The printed evidence that a line opens a node of this type.

    Every field is optional and None means "this type does not care".
    A line matches when every condition that is set is satisfied, so
    setting none matches any line and setting one narrows by exactly
    that much.

    Each condition can be inverted by its `not_` twin, which is the half
    that says what a type must NOT look like -- a paragraph that is bold
    is an Act-name citation inside a list, not a heading, and saying
    `not_bold: true` is how that gets ruled out.
    """

    # The line's text. Two capture groups: (number, heading-or-rest).
    pattern: str | None = None

    # Font weight. Bold marks the levels a drafter sets as headings --
    # Chapter down to Section -- and never marks subsection or below.
    bold: bool | None = None

    # A leading run of bold italic, which is how a defined term is
    # introduced where it is defined.
    bold_italic: bool | None = None

    # Font size as a multiple of this document's own body size, so a rule
    # holds whether the Act is set in 12pt or 10pt. A Part heading is
    # about 1.33 of body, a Division about 1.17, a Section exactly 1.0.
    min_size_ratio: float | None = None
    max_size_ratio: float | None = None

    # Where the line starts, in points from the left edge of the page.
    # Absolute rather than relative because it is what you can read off
    # the page and check: run `show_profile --explain` on a line and it
    # prints the number to put here.
    min_indent: float | None = None
    max_indent: float | None = None

    # How far in from the provision currently open. domain.md states the
    # hierarchy this way -- "indented once from a subsection" -- and it
    # is the only form that holds when the same marker appears at two
    # depths, as "(a)" does directly under a Section and again under a
    # subsection.
    indented_from_parent: bool | None = None

    # Whether the line is centred on the page. Victorian drafting centres
    # the headings that divide an Act -- Chapter, Part, Division,
    # Subdivision -- and sets everything else against a left indent, so a
    # centred line starts at a different place for every title while a
    # provision starts at the same place every time.
    centred: bool | None = None

    # The provision that must already be open for this type to be
    # possible. A subparagraph only follows a paragraph.
    parent_types: tuple[str, ...] = ()

    # Whether the previous line has to have finished cleanly. A numbered
    # provision opens after a full stop, a semicolon or a dash; a bare
    # number mid-sentence is a citation that happened to wrap.
    after_clean_break: bool | None = None

    not_bold: bool | None = None
    not_bold_italic: bool | None = None
    not_pattern: str | None = None


@dataclass
class NodeType:
    id: str
    name: str
    label: str
    description: str | None = None

    # How a line is recognised as opening a node of this type.
    recognition: Recognition | None = None

    # Which type wins when more than one matches. Lower is tried first.
    priority: int = 100

    # What kind of node is this?
    # Provisions are sections, clauses, etc.
    # Containers are chapters, parts, etc.
    is_provision: bool = False
    is_container: bool = False

    # What content does this node contain?
    allows_text: bool = True
    allows_heading: bool = True

    # What nodes can be children of this node?
    allows_children: list[str] = field(default_factory=list)
    
@dataclass
class Node:
    id: str
    node_type_id: str
    number: str | None = None
    heading: str | None = None
    text: str | None = None
    children: list["Node"] = field(default_factory=list)


class NodeTypeRegistry:
    def __init__(self):
        self._types: dict[str, NodeType] = {}

    def register(self, node_type: NodeType) -> None:
        if node_type.id in self._types:
            raise ValueError(
                f"Node type already exists: {node_type.id}"
            )

        self._types[node_type.id] = node_type

    def get(self, type_id: str) -> NodeType:
        try:
            return self._types[type_id]
        except KeyError:
            raise ValueError(
                f"Unknown node type: {type_id}"
            ) from None

    def exists(self, type_id: str) -> bool:
        return type_id in self._types

    def all(self) -> list[NodeType]:
        return list(self._types.values())