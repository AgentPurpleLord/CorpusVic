"""Loading the recognition rules that say what each node type looks like.

The rules live in YAML rather than in code so that adjusting what the
parser looks for is an edit to a sentence, not to a classifier. One base
file, victorian-act.yaml, holds the conventions from domain.md that every
Victorian Act follows; a file named after an Act overrides it block by
block, for the Acts that do something of their own.

A key that is not a real condition is an error here, while the file
loads, naming the key. A rule file is edited by hand and a typo in one
would otherwise show up as a provision quietly coming out as the wrong
type.
"""
import re
from dataclasses import fields
from pathlib import Path

import yaml

from corpus.domain.node import DEFAULT_NESTING_GAP, NodeType, NodeTypeRegistry, Recognition

RULES_DIR = Path(__file__).resolve().parent / "rules" / "recognition"

# The conventions every Victorian Act is drafted to, which a per-Act file
# narrows rather than replaces.
BASE_RULESET = "victorian-act"

_RECOGNITION_KEYS = {f.name for f in fields(Recognition)}

# Settings about the document as a whole rather than about one type.
_SETTINGS_KEYS = {"nesting_gap"}
_NODE_TYPE_KEYS = {f.name for f in fields(NodeType)} - {"id", "recognition"}


class RulesetError(ValueError):
    """A rule file could not be read as rules: bad YAML, a key that is
    not a condition, or a pattern that does not compile."""


def _path(name: str) -> "Path | None":
    for suffix in (".yaml", ".yml"):
        candidate = RULES_DIR / f"{name}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _read(name: str) -> tuple[dict, dict]:
    path = _path(name)
    if path is None:
        available = sorted(p.stem for p in RULES_DIR.glob("*.y*ml"))
        raise RulesetError(f"no ruleset named {name!r} in {RULES_DIR} -- available: {', '.join(available) or '(none)'}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise RulesetError(f"{path}: not valid YAML ({e})") from e
    if not isinstance(data, dict):
        raise RulesetError(f"{path}: expected a mapping at the top level, got {type(data).__name__}")
    types = data.get("types", {})
    if not isinstance(types, dict):
        raise RulesetError(f'{path}: "types" must be a mapping of type id to its rule')
    settings = {k: v for k, v in data.items() if k != "types"}
    unknown = set(settings) - _SETTINGS_KEYS
    if unknown:
        raise RulesetError(
            f"{path}: unknown setting(s) {', '.join(sorted(unknown))} "
            f"-- available: {', '.join(sorted(_SETTINGS_KEYS))}, types"
        )
    return types, settings


def _merge(base: dict, override: dict) -> dict:
    """A per-Act file replaces the blocks it names and leaves the rest.

    Merged one level into `recognition` as well, so an Act that only
    needs a different pattern for one type says just that, and keeps
    every other condition the base ruleset set for it.
    """
    merged = {k: dict(v) for k, v in base.items()}
    for type_id, block in override.items():
        block = dict(block or {})
        existing = merged.get(type_id)
        if existing and isinstance(block.get("recognition"), dict) and isinstance(existing.get("recognition"), dict):
            block["recognition"] = {**existing["recognition"], **block["recognition"]}
        merged[type_id] = {**(existing or {}), **block}
    return merged


def _recognition(source: str, type_id: str, block: dict) -> Recognition:
    unknown = set(block) - _RECOGNITION_KEYS
    if unknown:
        raise RulesetError(
            f"{source}: type {type_id!r} has unknown condition(s) {', '.join(sorted(unknown))} "
            f"-- available: {', '.join(sorted(_RECOGNITION_KEYS))}"
        )
    block = dict(block)
    if "parent_types" in block:
        block["parent_types"] = tuple(block["parent_types"] or ())
    for key in ("pattern", "not_pattern"):
        if block.get(key) is not None:
            try:
                re.compile(block[key])
            except re.error as e:
                raise RulesetError(f"{source}: type {type_id!r} {key} does not compile ({e})\n  {block[key]}") from e
    return Recognition(**block)


def nesting_gap(act: "str | None" = None, base: str = BASE_RULESET) -> float:
    """How far right this document sets one level. See recognise.py."""
    _types, settings = _read(base)
    if act and _path(act):
        settings = {**settings, **_read(act)[1]}
    return float(settings.get("nesting_gap", DEFAULT_NESTING_GAP))


def build_registry(act: "str | None" = None, base: str = BASE_RULESET) -> NodeTypeRegistry:
    """The node types for one document, base rules plus its own overrides.

    Passing no Act gives the base ruleset alone, which is the right
    answer for any Act that follows the ordinary conventions.
    """
    types, _settings = _read(base)
    source = base
    if act and _path(act):
        types = _merge(types, _read(act)[0])
        source = f"{base} + {act}"

    registry = NodeTypeRegistry()
    for type_id, block in types.items():
        block = dict(block or {})
        rule = block.pop("recognition", None)
        unknown = set(block) - _NODE_TYPE_KEYS
        if unknown:
            raise RulesetError(
                f"{source}: type {type_id!r} has unknown field(s) {', '.join(sorted(unknown))} "
                f"-- available: {', '.join(sorted(_NODE_TYPE_KEYS))}, recognition"
            )
        block.setdefault("name", type_id)
        block.setdefault("label", type_id)
        registry.register(NodeType(
            id=type_id,
            recognition=_recognition(source, type_id, rule) if rule else None,
            **block,
        ))
    return registry
