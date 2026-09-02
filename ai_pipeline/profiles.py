r"""
Per-act-family pattern configuration for the rule-based parser.

The base patterns below match the Chief Parliamentary Counsel (Victoria)
drafting convention shared by every Act checked so far (Crimes, Evidence,
Criminal Procedure, Interpretation). A new Act with a different numbering
style doesn't need new code -- drop a YAML file at
ai_pipeline/profiles/<act-slug>.yaml overriding just the patterns that
differ (pass --profile <act-slug> to run_pipeline.py to use it), e.g. the
Criminal Procedure Act numbers its Parts "5.1", "5.2" instead of roman
numerals, which the default "part" pattern's character class doesn't
allow -- see ai_pipeline/profiles/criminal-procedure-act.yaml for the
actual override that fixes it, and ai_pipeline/profiles/TEMPLATE.yaml for
a fully-commented starting point to copy for a new Act.

YAML rather than JSON specifically because every value here is a regular
expression: JSON strings require every backslash doubled ("\\\\d+" for a
single "\\d+" in the pattern), which turns a two-minute tweak into a
backslash-hunting exercise. YAML's single-quoted scalar style takes a
backslash literally, so a profile can just write

    section: '^(\d+[A-Za-z]*)\s+(.+)$'

and it means exactly what it looks like. YAML also allows "#" comments,
which JSON doesn't -- worth using liberally, since the whole point of a
profile is explaining *why* this Act's numbering differs.

Any key you don't override falls back to DEFAULT_PATTERNS. Every pattern
(default or overridden) must have at least two capture groups -- (number,
heading/rest) -- except "notes_marker" (a pure yes/no boundary check, no
groups needed) and "subdivision" (two two-group alternatives, four groups
total; see its own comment below). This is checked when the profile
loads, not at parse time, so a typo in a profile surfaces immediately
with the offending key named, rather than as a confusing IndexError three
modules away in rule_parser.py.

One reserved key isn't a pattern: `hierarchy:`, an ordered list of the
container level names for this Act. The default (see hierarchy.py) is
chapter/part/division/subdivision/section/subsection/paragraph/
subparagraph -- "chapter" is already in it, and its default pattern
matches the usual "Chapter N—Title" heading, so an Act that groups its
Parts under Chapters (the Criminal Procedure Act, the Evidence Act) needs
no override at all. Set `hierarchy:` only to reorder the heading levels or
introduce a level with no default (add its pattern in the same profile
too). The four bracket-numbered levels (section, subsection, paragraph,
subparagraph) must stay in the list. load_hierarchy() resolves it;
run_pipeline.py persists the result alongside the parsed nodes so the
exporters read it back rather than re-loading the profile.

Use `python show_profile.py <act-slug>` to see a profile's resolved
patterns (and which ones are overrides vs defaults), and
`python show_profile.py <act-slug> --test "some line of text"` to check
which pattern a specific line matches and what it captures -- the
fastest way to check an edit before re-running the full pipeline on it.
"""
import re
from pathlib import Path

import yaml

from .hierarchy import HIERARCHY_ORDER

PROFILES_DIR = Path(__file__).parent / "profiles"

# The bracket-numbered tail of the hierarchy -- always part of the model,
# so a profile's `hierarchy:` override may reorder or prepend to the
# heading levels but can't drop these (rule_parser.py's bracket-item
# classifier assumes all three exist).
_REQUIRED_LEVELS = {"section", "subsection", "paragraph", "subparagraph"}

# Profile keys that aren't patterns.
_RESERVED_KEYS = {"hierarchy"}

# Every pattern must have exactly two capture groups: (number, heading/rest),
# except notes_marker/example_marker (pure boundary checks, no groups).
DEFAULT_PATTERNS = {
    # A Schedule heading uses the same "Word N—Title" shape as Chapter/Part/
    # Division, but the dash separator has been seen doubled in real Acts
    # ("Schedule 1––Charges on a charge-sheet or indictment") as well as
    # the ordinary single em-dash used everywhere else ("Schedule 5—
    # Transitional provisions...") -- "+" rather than the single-character
    # class the other Word-N-Title patterns use, so either form's title
    # capture comes out clean instead of with a stray leading dash.
    "schedule": r"^Schedule\s+(\d+[A-Za-z]*)\s*[—–-]+\s*(.+)$",
    # Chapter is the optional top level above Part. Most Victorian Acts have
    # none; some (e.g. the Criminal Procedure Act) group their Parts under
    # numbered Chapters. Same "Word N—Title" shape as Part/Division -- only
    # Acts whose profile lists "chapter" in `hierarchy:` actually use it.
    "chapter": r"^Chapter\s+(\d+[A-Z]*)\s*[—–-]\s*(.+)$",
    "part": r"^Part\s+([A-Z0-9]+[A-Z]?)\s*[—–-]\s*(.+)$",
    "division": r"^Division\s+(\d+[A-Z]*)\s*[—–-]\s*(.+)$",
    # Subdivision headings are usually "(1) Homicide" -- a bracketed number
    # plus a short bold title, distinguished from a subsection by being
    # bold (checked separately, not in this regex). The same Act can also
    # spell it out as "Subdivision 2—Title" in places, so both are accepted.
    "subdivision": r"^(?:\((\w+)\)\s+(.+)|Subdivision\s+(\w+)\s*[—–-]\s*(.+))$",
    # A section is any leading number (with optional letter suffix -- some
    # heavily-amended Acts run these out to 5-6 letters, e.g. "464ZFAAA",
    # "465AAAAB", so the suffix is unbounded rather than capped) NOT
    # wrapped in brackets -- brackets always mean subsection/paragraph/
    # subparagraph instead, so there's no ambiguity with the patterns below.
    "section": r"^(\d+[A-Za-z]*)\s+(.+)$",
    "subsection": r"^\((\d+[A-Za-z]*)\)\s*(.*)$",
    "paragraph": r"^\(([a-z]{1,3})\)\s*(.*)$",
    "subparagraph": r"^\(([ivxlcdm]+)\)\s*(.*)$",
    # Bracketed capital letters -- "(A)", "(B)" -- one level deeper than a
    # subparagraph's lowercase roman numerals. Drafters avoid this level
    # where possible (see basic-structure.yaml), but it does appear in
    # heavily-amended sections. Case alone (upper vs lower) keeps this
    # unambiguous against paragraph/subparagraph -- unlike those two, which
    # can both match a bare "(i)" and need _bracket_level's own sequence-
    # continuity check to disambiguate, a capital letter never does.
    "sub_subparagraph": r"^\(([A-Z]{1,3})\)\s*(.*)$",
    "notes_marker": r"^Notes?$",
    "note_item": r"^(\d+)\s+(.+)$",
    # An "Example" callout is set exactly like a singular, unnumbered
    # "Note" (see rule_parser.py's _handle_marked_block) -- same bold,
    # body-sized, standalone-line convention, just a different marker word.
    "example_marker": r"^Examples?$",
}

# Every key is matched case-sensitively except these -- a Chapter/Part/
# Division heading is occasionally set in a slightly different case (small
# caps, etc.) across scanned/reflowed Acts, while Section/Subsection/
# Paragraph/Subparagraph numbering is never ambiguous enough to need it and
# case-insensitivity there would risk matching stray body text.
_IGNORECASE_KEYS = {"chapter", "part", "division"}


class ProfileError(ValueError):
    """A profile file failed to load: invalid YAML, an unrecognised
    pattern key (almost always a typo), or a pattern that doesn't compile
    or doesn't have enough capture groups. Raised while loading the
    profile -- before any PDF parsing starts -- so the problem is obvious
    and names the exact key, instead of surfacing later as a confusing
    IndexError or silently-wrong classification deep in rule_parser.py."""


# Every key needs (number, heading/rest) -- two groups -- except
# notes_marker/example_marker, pure boundary checks ("does this line say
# "Notes"/"Example"?"); rule_parser.py only tests them for truthiness and
# never reads a group from either.
_MIN_GROUPS = {"notes_marker": 0, "example_marker": 0}


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
    DEFAULT_PATTERNS order -- what show_profile.py renders, and reusable
    anywhere else a resolved profile needs displaying or introspecting."""
    _source, data = _load_raw(name) if name else (None, {})
    overrides = _pattern_overrides(_source, data) if _source else {}
    return [(key, overrides.get(key, DEFAULT_PATTERNS[key]), key in overrides) for key in DEFAULT_PATTERNS]


def _profile_path(name: str) -> Path | None:
    for ext in (".yaml", ".yml"):
        path = PROFILES_DIR / f"{name}{ext}"
        if path.exists():
            return path
    return None


def _load_raw(name: str) -> tuple[str | None, dict]:
    """(path-as-str, parsed-mapping) for a profile file, or (None, {}) if
    there's no file for this name. Raises ProfileError on invalid YAML or a
    non-mapping top level -- everything else (unknown keys, bad patterns, a
    malformed hierarchy) is validated by the caller that needs it."""
    path = _profile_path(name)
    if path is None:
        return None, {}
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
    """The resolved ordered list of container levels for this Act -- the
    default (see hierarchy.py) unless the profile overrides it with a
    `hierarchy:` list. run_pipeline.py persists this into
    data/ai_parsed/<act>.json so the exporters can rebuild the tree
    without re-loading the profile."""
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
