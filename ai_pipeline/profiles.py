"""
Per-act-family pattern configuration for the rule-based parser.

The base patterns below match the Chief Parliamentary Counsel (Victoria)
drafting convention shared by every Act checked so far (Crimes, Evidence,
Criminal Procedure, Interpretation). A new Act with a different numbering
style doesn't need new code -- drop a JSON file at
ai_pipeline/profiles/<act-slug>.json overriding just the patterns that
differ, e.g.:

    {"part": "^Chapter\\s+(\\d+)\\s*[-:]\\s*(.+)$"}

Any key you don't override falls back to DEFAULT_PATTERNS.
"""
import json
import re
from pathlib import Path

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


def load_profile(name: str | None) -> dict:
    patterns = dict(DEFAULT_PATTERNS)
    if name:
        path = PROFILES_DIR / f"{name}.json"
        if path.exists():
            overrides = json.loads(path.read_text(encoding="utf-8"))
            patterns.update(overrides)
    return {key: re.compile(pattern, re.IGNORECASE if key in ("part", "division") else 0) for key, pattern in patterns.items()}
