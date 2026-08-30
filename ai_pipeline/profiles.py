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

Use `python show_profile.py <act-slug>` to see a profile's resolved
patterns (and which ones are overrides vs defaults), and
`python show_profile.py <act-slug> --test "some line of text"` to check
which pattern a specific line matches and what it captures -- the
fastest way to check an edit before re-running the full pipeline on it.
"""
import re
from pathlib import Path

import yaml

PROFILES_DIR = Path(__file__).parent / "profiles"

# Every pattern must have exactly two capture groups: (number, heading/rest).
DEFAULT_PATTERNS = {
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
    "notes_marker": r"^Notes?$",
    "note_item": r"^(\d+)\s+(.+)$",
}

# Every key is matched case-sensitively except these two -- a Part/Division
# heading is occasionally set in a slightly different case (small caps,
# etc.) across scanned/reflowed Acts, while Section/Subsection/Paragraph/
# Subparagraph numbering is never ambiguous enough to need it and case-
# insensitivity there would risk matching stray body text.
_IGNORECASE_KEYS = {"part", "division"}


class ProfileError(ValueError):
    """A profile file failed to load: invalid YAML, an unrecognised
    pattern key (almost always a typo), or a pattern that doesn't compile
    or doesn't have enough capture groups. Raised while loading the
    profile -- before any PDF parsing starts -- so the problem is obvious
    and names the exact key, instead of surfacing later as a confusing
    IndexError or silently-wrong classification deep in rule_parser.py."""


# Every key needs (number, heading/rest) -- two groups -- except
# notes_marker, which is a pure boundary check ("does this line say
# "Notes"?"); rule_parser.py only tests it for truthiness and never reads
# a group from it.
_MIN_GROUPS = {"notes_marker": 0}


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
    overrides = _load_overrides(name) if name else {}
    return [(key, overrides.get(key, DEFAULT_PATTERNS[key]), key in overrides) for key in DEFAULT_PATTERNS]


def _profile_path(name: str) -> Path | None:
    for ext in (".yaml", ".yml"):
        path = PROFILES_DIR / f"{name}{ext}"
        if path.exists():
            return path
    return None


def _load_overrides(name: str) -> dict:
    path = _profile_path(name)
    if path is None:
        return {}
    try:
        overrides = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ProfileError(f"{path}: invalid YAML ({e})") from e
    if not isinstance(overrides, dict):
        raise ProfileError(f"{path}: must be a YAML mapping of pattern-name -> pattern, got {type(overrides).__name__}")
    unknown = set(overrides) - set(DEFAULT_PATTERNS)
    if unknown:
        raise ProfileError(f"{path}: unknown pattern key(s) {sorted(unknown)} -- valid keys are {sorted(DEFAULT_PATTERNS)}")
    for key, pattern in overrides.items():
        _validate_pattern(str(path), key, pattern)
    return overrides


def load_profile(name: str | None) -> dict:
    patterns = dict(DEFAULT_PATTERNS)
    if name:
        patterns.update(_load_overrides(name))
    return {key: re.compile(pattern, re.IGNORECASE if key in _IGNORECASE_KEYS else 0) for key, pattern in patterns.items()}
