"""Deciding what a printed line is, from what a reader can see on it.

Victorian drafting sets each level of the hierarchy differently, and
domain.md writes those settings down: a Section carries a bolded number
and heading, a subsection is "(1)" in normal font indented once from its
Section, a defined term opens in bold italics, a Schedule heading is
larger than body text. Each of those sentences is a set of conditions on
one line, and NodeType.recognition holds them.

This module evaluates them. It reads no PDF and keeps no state: given a
line, the provisions currently open around it, and the registry, it
returns every type that could match and, for the ones that could not,
the condition that ruled them out. That is what makes a misread
diagnosable -- `show_profile --explain` prints exactly this -- and what
lets a rule be tested without a document.
"""
import re
from dataclasses import dataclass, field

from corpus.domain.node import DEFAULT_NESTING_GAP, NodeType, NodeTypeRegistry, Recognition


# How unequal a line's two margins may be and still count as centred.
# Centring is done on the line's own width, which the justified body text
# around it is not, so the two margins of a heading come out within a few
# points of each other and those of an indented provision do not.
CENTRING_TOLERANCE = 12.0


# A provision opens a new sentence. When the line above stopped at one of
# these, whatever follows is a fresh start rather than the tail of a
# sentence that happened to wrap onto a bracketed cross-reference.
_TERMINAL_PUNCT_RE = re.compile(r"[.;:!?]\s*$")


@dataclass(frozen=True)
class Context:
    """Everything outside the line itself that a rule may read.

    Assembled by the caller from its own parse state, so that recognition
    stays a function of its inputs and a line can be re-tested later from
    the numbers alone.
    """

    # The document's ordinary text size. Size rules are ratios against
    # this, so one rule covers an Act set in 12pt and a Bill set in 10pt.
    body_size: float = 12.0

    # The provisions enclosing this line, outermost first: the type of
    # each, and where it started on the page.
    open_types: tuple[str, ...] = ()
    open_x0: tuple[float, ...] = ()

    # How far right is a level. See DEFAULT_NESTING_GAP.
    nesting_gap: float = DEFAULT_NESTING_GAP

    # The width of the page the line is printed on, for `centred`.
    page_width: float = 0.0

    # The line above, for `after_clean_break`. A heading counts as clean:
    # "Division 1—Offences" has no sentence to finish.
    prev_text: str = ""
    prev_was_heading: bool = False

    def enclosing(self, x0: float) -> "tuple[str | None, float | None]":
        """The provision a line at this x0 sits inside.

        Not simply the innermost thing open: that is the line's older
        sibling as often as its parent. "(c)" follows "(a)" at the same
        indent, so the provision it belongs to is whatever "(a)" itself
        belonged to, and the page says so by starting both at the same
        place. Walking out until something starts a clear level further
        left is how a reader tells a sibling from a parent, and it is why
        "(c)" comes out a paragraph rather than a level deeper.
        """
        for type_id, open_x0 in zip(reversed(self.open_types), reversed(self.open_x0)):
            if open_x0 < x0 - self.nesting_gap:
                return type_id, open_x0
        return None, None


@dataclass(frozen=True)
class Condition:
    """One rule, and whether this line satisfied it."""

    name: str
    ok: bool
    detail: str

    def __str__(self) -> str:
        return f"{self.name} {'PASS' if self.ok else 'FAIL'} ({self.detail})"


@dataclass(frozen=True)
class Verdict:
    """One node type weighed against one line."""

    type_id: str
    matched: bool
    conditions: list[Condition] = field(default_factory=list)

    # What `pattern` captured: the provision's number and its heading or
    # opening text. Only set when the type matched.
    number: "str | None" = None
    heading: "str | None" = None

    @property
    def failed(self) -> "Condition | None":
        """The condition that ruled this type out -- the whole reason a
        line was not read as this type, and the thing to change."""
        return next((c for c in self.conditions if not c.ok), None)


def _clean_break(ctx: Context) -> bool:
    if not ctx.prev_text or ctx.prev_was_heading:
        return True
    return bool(_TERMINAL_PUNCT_RE.search(ctx.prev_text))


def _check(rule: Recognition, line, ctx: Context) -> "tuple[list[Condition], str | None, str | None]":
    """Every condition the rule sets, evaluated against this line.

    Unset conditions are not reported: a rule that says nothing about
    boldness should not print a row about boldness. A rule that sets
    nothing matches everything, which is what an unconstrained type means.
    """
    out: list[Condition] = []
    number = heading = None
    text = line.text.strip()
    size = round(line.size, 1)
    ratio = size / ctx.body_size if ctx.body_size else 1.0

    if rule.pattern is not None:
        m = re.match(rule.pattern, text)
        if m:
            groups = [g for g in m.groups() if g is not None]
            number = groups[0] if groups else None
            heading = groups[1] if len(groups) > 1 else None
        out.append(Condition("pattern", m is not None,
                             f"captured {number!r}, {heading!r}" if m else rule.pattern))

    if rule.not_pattern is not None:
        hit = re.match(rule.not_pattern, text)
        out.append(Condition("not_pattern", hit is None,
                             "matched, so excluded" if hit else "not matched"))

    if rule.bold is not None:
        out.append(Condition("bold", line.bold is rule.bold,
                             f"line is {'bold' if line.bold else 'not bold'}"))
    if rule.not_bold:
        out.append(Condition("not_bold", not line.bold,
                             f"line is {'bold' if line.bold else 'not bold'}"))

    has_bi = line.leading_bold_italic is not None
    if rule.bold_italic is not None:
        out.append(Condition("bold_italic", has_bi is rule.bold_italic,
                             f"opens with {line.leading_bold_italic!r}" if has_bi
                             else "no leading bold italic"))
    if rule.not_bold_italic:
        out.append(Condition("not_bold_italic", not has_bi,
                             f"opens with {line.leading_bold_italic!r}" if has_bi
                             else "no leading bold italic"))

    if rule.min_size_ratio is not None:
        out.append(Condition("min_size_ratio", ratio >= rule.min_size_ratio - 0.01,
                             f"{size} is {ratio:.2f} of body {ctx.body_size}"))
    if rule.max_size_ratio is not None:
        out.append(Condition("max_size_ratio", ratio <= rule.max_size_ratio + 0.01,
                             f"{size} is {ratio:.2f} of body {ctx.body_size}"))

    if rule.centred is not None:
        if not ctx.page_width:
            out.append(Condition("centred", True, "page width not known"))
        else:
            left, right = line.x0, ctx.page_width - line.x1
            centred = abs(left - right) <= CENTRING_TOLERANCE
            out.append(Condition("centred", centred is rule.centred,
                                 f"margins {left:.0f} left, {right:.0f} right"))

    if rule.min_indent is not None:
        out.append(Condition("min_indent", line.x0 >= rule.min_indent - INDENT_TOLERANCE,
                             f"x0 {line.x0:.1f}"))
    if rule.max_indent is not None:
        out.append(Condition("max_indent", line.x0 <= rule.max_indent + INDENT_TOLERANCE,
                             f"x0 {line.x0:.1f}"))

    parent_type, parent_x0 = ctx.enclosing(line.x0)

    if rule.indented_from_parent is not None:
        # Nothing enclosing means nothing to be indented from, and the
        # rule cannot be judged either way -- treated as satisfied, so a
        # type is only ever ruled out by evidence that is actually there.
        if parent_x0 is None:
            out.append(Condition("indented_from_parent", True, "nothing open further left"))
        else:
            deeper = line.x0 > parent_x0 + ctx.nesting_gap
            out.append(Condition("indented_from_parent", deeper is rule.indented_from_parent,
                                 f"x0 {line.x0:.1f} inside {parent_type} at {parent_x0:.1f}"))

    if rule.parent_types:
        out.append(Condition("parent_types", parent_type in rule.parent_types,
                             f"inside {parent_type}, needs one of {list(rule.parent_types)}"))

    if rule.after_clean_break is not None:
        clean = _clean_break(ctx)
        out.append(Condition("after_clean_break", clean is rule.after_clean_break,
                             f"previous line {'ended cleanly' if clean else 'was mid-sentence'}"))

    return out, number, heading


def weigh(node_type: NodeType, line, ctx: Context) -> Verdict:
    """One type against one line, with the reasoning kept."""
    if node_type.recognition is None:
        return Verdict(node_type.id, False,
                       [Condition("recognition", False, "type has no recognition rule")])
    conditions, number, heading = _check(node_type.recognition, line, ctx)
    matched = all(c.ok for c in conditions)
    return Verdict(node_type.id, matched, conditions,
                   number if matched else None, heading if matched else None)


def recognise(line, ctx: Context, registry: NodeTypeRegistry) -> list[Verdict]:
    """Every type that carries a rule, weighed against this line.

    Ordered by NodeType.priority, so the first matching verdict is the
    one that wins. Types are returned whether they matched or not,
    because the ones that did not are the diagnosis.
    """
    types = [t for t in registry.all() if t.recognition is not None]
    types.sort(key=lambda t: (t.priority, t.id))
    return [weigh(t, line, ctx) for t in types]


def best(line, ctx: Context, registry: NodeTypeRegistry) -> "Verdict | None":
    """The type this line opens, or None if it opens nothing.

    A line that matches no type is ordinary text continuing whatever
    provision is already open, which is the common case.
    """
    return next((v for v in recognise(line, ctx, registry) if v.matched), None)
