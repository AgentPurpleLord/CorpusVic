r"""
Per-Act settings for the rule-based parser -- lets one Act's numbering
style differ from the rest without needing a code change.

The base patterns below match the drafting style shared by every Act
checked so far (Crimes, Evidence, Criminal Procedure, Interpretation). A
new Act with a different numbering style doesn't need new code -- just
add a YAML file at corpus/domain/rules/<act-slug>.yaml overriding the
patterns that differ (pass --profile <act-slug> to run_pipeline.py to
use it). For example, the Criminal Procedure Act numbers its Parts
"5.1", "5.2" instead of roman numerals, which the default "part" pattern
doesn't allow for -- see corpus/domain/rules/criminal-procedure-act.yaml
for the actual fix, and corpus/domain/rules/TEMPLATE.yaml for a fully-
commented starting point to copy for a new Act.

These files are YAML rather than JSON specifically because every value
here is a regular expression. In JSON, every backslash in a pattern has
to be doubled ("\\\\d+" for a plain "\\d+"), which turns a two-minute
tweak into a backslash-hunting exercise. YAML's single-quoted style
takes a backslash literally, so a profile can just write

    section: '^(\d+[A-Za-z]*)\s+(.+)$'

and it means exactly what it looks like. YAML also allows "#" comments,
which JSON doesn't -- worth using freely, since the whole point of a
profile is explaining *why* this Act's numbering is different.

Any pattern you don't override falls back to DEFAULT_PATTERNS. Every
pattern (default or overridden) needs at least two capture groups --
(number, heading/rest) -- except the marker patterns "notes_marker",
"example_marker" and "penalty_marker" (plain yes/no checks, no groups
needed) and "subdivision" (two two-group options, four groups
total; see its own comment below). This is checked as soon as the
profile loads, not later while parsing, so a typo in a profile is
reported right away, naming the exact key that's wrong, instead of
showing up later as a confusing crash deep inside rule_parser.py.

One key, `hierarchy:`, isn't a pattern -- it's an ordered list of this
Act's container level names. The default (see hierarchy.py) is chapter/
part/division/subdivision/section/subsection/paragraph/subparagraph --
"chapter" is already in that list, with a default pattern that matches
the usual "Chapter N—Title" heading, so an Act that groups its Parts
under Chapters (the Criminal Procedure Act, the Evidence Act) needs no
override at all. Only set `hierarchy:` to reorder the levels or add one
with no default (and give it its own pattern in the same profile). The
four bracket-numbered levels (section, subsection, paragraph,
subparagraph) must always stay in the list. load_hierarchy() works out
the final list; run_pipeline.py saves the result alongside the parsed
nodes so the exporters can read it back without reloading the profile.

Run `python show_profile.py <act-slug>` to see a profile's final
patterns (and which ones are overrides versus defaults), and
`python show_profile.py <act-slug> --test "some line of text"` to check
which pattern a specific line matches and what it captures -- the
fastest way to check an edit before re-running the whole pipeline.
"""
import json
import re
from pathlib import Path

import yaml

from corpus.domain.hierarchy import HIERARCHY_ORDER
from corpus.parsing.versions import _DOCUMENT_SLUG_RE

PROFILES_DIR = Path(__file__).parent / "rules"

# The bracket-numbered levels at the bottom of the hierarchy -- always
# part of the model. A profile's `hierarchy:` override can reorder or
# add to the heading levels above these, but can't remove them
# (rule_parser.py's bracket-item classifier assumes all of these exist).
_REQUIRED_LEVELS = {"section", "subsection", "paragraph", "subparagraph"}

# Profile keys that aren't patterns.
_RESERVED_KEYS = {"hierarchy"}

# Every pattern must have exactly two capture groups: (number, heading/rest),
# except notes_marker/example_marker/penalty_marker (pure boundary checks,
# no groups).
DEFAULT_PATTERNS = {
    # A Schedule heading uses the same "Word N—Title" shape as Chapter,
    # Part and Division, but the dash between them has been seen doubled
    # in real Acts ("Schedule 1––Charges on a charge-sheet or
    # indictment") as well as the ordinary single dash used everywhere
    # else ("Schedule 5—Transitional provisions..."). Matching one or
    # more dashes, rather than exactly one, means either form's title
    # comes out clean instead of with a stray leading dash left on it.
    "schedule": r"^Schedule\s+(\d+[A-Za-z]*)\s*[—–-]+\s*(.+)$",
    # Chapter is the optional top level above Part. Most Victorian Acts
    # don't have one; some (like the Criminal Procedure Act) group their
    # Parts under numbered Chapters. Same "Word N—Title" shape as Part
    # and Division -- only used by Acts whose profile lists "chapter" in
    # `hierarchy:`.
    "chapter": r"^Chapter\s+(\d+[A-Z]*)\s*[—–-]\s*(.+)$",
    "part": r"^Part\s+([A-Z0-9]+[A-Z]?)\s*[—–-]\s*(.+)$",
    "division": r"^Division\s+(\d+[A-Z]*)\s*[—–-]\s*(.+)$",
    # Subdivision headings are usually "(1) Homicide" -- a bracketed
    # number plus a short bold title, told apart from a subsection by
    # being bold (checked separately, not in this pattern). The same Act
    # can also spell it out as "Subdivision 2—Title" in places, so both
    # forms are accepted.
    "subdivision": r"^(?:\((\w+)\)\s+(.+)|Subdivision\s+(\w+)\s*[—–-]\s*(.+))$",
    # A section is any number at the start of a line (with an optional
    # letter suffix -- some heavily-amended Acts run these out to 5-6
    # letters, e.g. "464ZFAAA", "465AAAAB", so there's no cap on the
    # suffix length) that isn't wrapped in brackets. Brackets always mean
    # subsection, paragraph or subparagraph instead, so there's no
    # overlap with the patterns below.
    "section": r"^(\d+[A-Za-z]*)\s+(.+)$",
    "subsection": r"^\((\d+[A-Za-z]*)\)\s*(.*)$",
    "paragraph": r"^\(([a-z]{1,3})\)\s*(.*)$",
    "subparagraph": r"^\(([ivxlcdm]+)\)\s*(.*)$",
    # Bracketed capital letters -- "(A)", "(B)" -- one level deeper than
    # a subparagraph's lowercase roman numerals. Drafters avoid this
    # level where they can (see basic-structure.yaml), but it does show
    # up in heavily-amended sections. Case alone (upper vs lower) is
    # enough to tell this apart from paragraph/subparagraph -- unlike
    # those two, which can both match a bare "(i)" and need
    # _bracket_level's own check on what number came before it to tell
    # them apart, a capital letter never needs that.
    "sub_subparagraph": r"^\(([A-Z]{1,3})\)\s*(.*)$",
    # "Examples—" as the Family Violence Protection Act prints it before a
    # list of dot points; "Note:" likewise.
    "notes_marker": r"^Notes?[\u2014\u2013:]?$",
    "note_item": r"^(\d+)\s+(.+)$",
    # An "Example" callout is set exactly like a plain, unnumbered "Note"
    # (see rule_parser.py's _handle_marked_block) -- same bold, body-
    # sized, standalone-line style, just a different word.
    "example_marker": r"^Examples?[\u2014\u2013:]?$",
    # The penalty for an offence, which Victorian drafting sets on its
    # own line under the provision creating it: "Penalty: Level 3
    # imprisonment (20 years maximum)." It is not part of the offence's
    # own sentence and shouldn't read as though it were -- see
    # rule_parser's own penalty handling.
    #
    # Anchored and capitalised deliberately. "penalty" appears
    # constantly in ordinary legislative prose ("...where a penalty is
    # prescribed by law...", "the penalty must be recovered only
    # before..."), and every one of those is mid-sentence and lowercase;
    # every real penalty line starts one. The colon is required for the
    # same reason -- it is what makes the line a label rather than a
    # sentence.
    "penalty_marker": r"^Penalt(?:y|ies)\s*:",
}

# Every key is matched with case sensitivity except these -- a Chapter,
# Part or Division heading is occasionally printed in a slightly
# different case (small caps, etc.) in a scanned or reflowed Act, while
# Section/Subsection/Paragraph/Subparagraph numbering is never
# ambiguous enough to need that, and ignoring case there would risk
# matching stray body text instead.
_IGNORECASE_KEYS = {"chapter", "part", "division"}


class ProfileError(ValueError):
    """A profile file failed to load: invalid YAML, an unrecognised
    pattern key (almost always a typo), or a pattern that doesn't
    compile or doesn't have enough capture groups. Raised while the
    profile loads -- before any PDF parsing starts -- so the problem is
    obvious and names the exact key, instead of showing up later as a
    confusing crash or silently wrong classification deep inside
    rule_parser.py."""


# Every key needs (number, heading/rest) -- two groups -- except
# notes_marker/example_marker/penalty_marker, which are just yes/no
# checks ("does this line say "Notes"/"Example"/"Penalty:"?");
# rule_parser.py only checks whether they matched at all and never reads
# a group from any of them.
_MIN_GROUPS = {"notes_marker": 0, "example_marker": 0, "penalty_marker": 0}


def _validate_pattern(source: str, key: str, pattern: str) -> None:
    if not isinstance(pattern, str):
        raise ProfileError(f"{source}: pattern \"{key}\" must be a string, got {type(pattern).__name__}")
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ProfileError(f'{source}: pattern "{key}" does not compile as a regex ({e})\n  pattern: {pattern}') from e
    needed = _MIN_GROUPS.get(key, 2)
    if compiled.groups < needed:
        raise ProfileError(
            f'{source}: pattern "{key}" has {compiled.groups} capture group(s), needs at least {needed} '
            f"-- (number, heading/rest)\n  pattern: {pattern}"
        )


def describe_profile(name: str | None) -> list[tuple[str, str, bool]]:
    """[(key, pattern, is_override), ...] for every pattern key, in
    DEFAULT_PATTERNS order -- what show_profile.py displays, and reusable
    anywhere else a resolved profile needs to be shown or inspected."""
    _source, data = _load_raw(name) if name else (None, {})
    overrides = _pattern_overrides(_source, data) if _source else {}
    return [(key, overrides.get(key, DEFAULT_PATTERNS[key]), key in overrides) for key in DEFAULT_PATTERNS]


def _profile_path(name: str) -> Path | None:
    for ext in (".yaml", ".yml"):
        path = PROFILES_DIR / f"{name}{ext}"
        if path.exists():
            return path
    return None


def profile_for(act_slug: str, base_dir: "str | Path | None" = None) -> "str | None":
    """The profile a document should be parsed with, worked out rather
    than remembered by whoever is running the parse.

    Nothing ties a profile's filename to a PDF, so a re-parse that simply
    forgot to name one produced a quietly worse parse instead of an
    error: without its own profile the Criminal Procedure Act's "Part
    2.1" stops matching as a Part at all, and its heading is swallowed
    into the Chapter above it. The fix is to stop asking.

    In order: the profile the existing parse recorded using, then a
    profile file named after the work (one profile serves every reprint
    of an Act -- how it numbers its Parts is a fact about the Act), then
    one named after the slug itself.
    """
    base = Path(base_dir) if base_dir else Path(".")
    parsed = base / "data" / "parsed" / f"{act_slug}.json"
    if parsed.exists():
        try:
            recorded = json.loads(parsed.read_text(encoding="utf-8")).get("profile")
        except (OSError, ValueError):
            recorded = None
        if recorded and profile_exists(recorded):
            return recorded
    work = _DOCUMENT_SLUG_RE.match(act_slug)
    for name in ([work.group("work")] if work else []) + [act_slug]:
        if profile_exists(name):
            return name
    return None


def available_profiles() -> list[str]:
    """Every profile that exists, for offering rather than having to be
    remembered. TEMPLATE is the documented blank to copy, not a profile
    any document is parsed with."""
    return sorted(
        path.stem for path in PROFILES_DIR.glob("*.y*ml") if path.stem != "TEMPLATE"
    )


def profile_exists(name: str) -> bool:
    """Whether a profile file of this name exists. run_pipeline.py uses
    this to apply an Act's own profile automatically -- a file named
    after the Act was clearly written for it, and making someone name it
    again on the command line only ever meant it got forgotten."""
    return _profile_path(name) is not None


def _load_raw(name: str) -> tuple[str | None, dict]:
    """(path-as-str, parsed-mapping) for a profile file. Raises
    ProfileError if there is no such profile, on invalid YAML, or on a top
    level that isn't a mapping -- everything else (unknown keys, bad
    patterns, a malformed hierarchy) is checked by whichever caller needs
    it.

    Called only with a name; parsing with no profile at all is the
    `name is None` path in load_profile, which never reaches here."""
    path = _profile_path(name)
    if path is None:
        # Named a profile that isn't there. Falling back to the built-in
        # patterns looks harmless and isn't: the Criminal Procedure Act
        # parsed with the defaults stops matching "Part 2.1" as a Part at
        # all and folds its heading into the Chapter above it, with
        # nothing anywhere to say why. Asking for a profile that does not
        # exist is a mistake, and mistakes are better loud.
        raise ProfileError(
            f"No profile named {name!r} in {PROFILES_DIR} -- "
            f"available: {', '.join(sorted(p.stem for p in PROFILES_DIR.glob('*.y*ml'))) or '(none)'}"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"{path}: invalid YAML ({e})") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: must be a YAML mapping of profile keys, got {type(data).__name__}")
    return str(path), data


def _pattern_overrides(source: str, data: dict) -> dict:
    overrides = {k: v for k, v in data.items() if k not in _RESERVED_KEYS}
    unknown = set(overrides) - set(DEFAULT_PATTERNS)
    if unknown:
        raise ProfileError(
            f"{source}: unknown pattern key(s) {sorted(unknown)} -- valid keys are "
            f"{sorted(DEFAULT_PATTERNS)} (plus reserved key(s) {sorted(_RESERVED_KEYS)})"
        )
    for key, pattern in overrides.items():
        _validate_pattern(source, key, pattern)
    return overrides


def _resolve_hierarchy(source: str, data: dict, pattern_overrides: dict) -> list[str]:
    raw = data.get("hierarchy")
    if raw is None:
        return list(HIERARCHY_ORDER)
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ProfileError(f"{source}: 'hierarchy' must be a list of non-empty level names, got {raw!r}")
    levels = [x.strip() for x in raw]
    if len(set(levels)) != len(levels):
        raise ProfileError(f"{source}: 'hierarchy' has a duplicate level: {levels}")
    missing_required = _REQUIRED_LEVELS - set(levels)
    if missing_required:
        raise ProfileError(
            f"{source}: 'hierarchy' must include {sorted(_REQUIRED_LEVELS)} (the bracket-numbered levels "
            f"are always part of the model) -- missing {sorted(missing_required)}"
        )
    available = set(DEFAULT_PATTERNS) | set(pattern_overrides)
    no_pattern = [lvl for lvl in levels if lvl not in available]
    if no_pattern:
        raise ProfileError(
            f"{source}: 'hierarchy' names level(s) with no matching pattern: {no_pattern}. "
            f"Add a '{no_pattern[0]}:' regex to this profile (two capture groups: number, title)."
        )
    return levels


def load_hierarchy(name: str | None) -> list[str]:
    """The final ordered list of container levels for this Act -- the
    default (see hierarchy.py) unless the profile overrides it with its
    own `hierarchy:` list. run_pipeline.py saves this into
    data/parsed/<act>.json so the exporters can rebuild the tree
    without reloading the profile."""
    if not name:
        return list(HIERARCHY_ORDER)
    source, data = _load_raw(name)
    if source is None:
        return list(HIERARCHY_ORDER)
    return _resolve_hierarchy(source, data, _pattern_overrides(source, data))


def load_profile(name: str | None) -> dict:
    patterns = dict(DEFAULT_PATTERNS)
    if name:
        source, data = _load_raw(name)
        if source is not None:
            patterns.update(_pattern_overrides(source, data))
    return {key: re.compile(pattern, re.IGNORECASE if key in _IGNORECASE_KEYS else 0) for key, pattern in patterns.items()}
