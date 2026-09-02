"""
Links a Bill's clauses to its enacted Act's sections, and its Explanatory
Memorandum's clause notes to whichever Act/section they actually
describe -- the concrete "how do we connect the three documents" layer.

Two link types:

1. Bill clause -> Act section, by number (match_bill_to_act). Most
   clauses pass through Parliament unamended, so the same clause number
   usually lands on the same section number in the enacted Act -- but a
   House amendment can insert a brand-new numbered clause partway through
   a Bill (see the OCPC's Legislative Process Handbook, "House
   Amendments"), shifting every later clause's number relative to what
   the Bill's own introduction print shows. Matching by number alone
   can't tell "shifted because of an insertion" apart from "genuinely the
   same provision, just lightly reworded during drafting" -- text
   similarity between the two is what tells them apart. Every same-
   numbered pair is linked either way (never leave it unresolved just
   because the text drifted -- that's still the best guess available);
   a low-similarity match is flagged for a human to actually look at
   rather than trusted outright, and a clause number with no matching
   section at all is flagged as unmatched.

2. EM entry -> the Act/section (or Bill clause, for the Bill's own new
   provisions) it actually explains (resolve_em_links). The OCPC's own
   "Guide to preparing an explanatory memorandum" specifies the
   convention this follows: an amending-clause note names its target Act
   (short title + year) and section number close to the start of the
   note ("Clause 11 inserts new section 44A into the Confiscation Act
   1997 to..."), while a note explaining one of the Bill's own new
   provisions (no amendment involved) has no Act name to find at all --
   that absence is itself the signal that it means the Bill's own
   section, resolved via link type 1. A note can also fall back on "the
   Principal Act" or "this Act" instead of repeating a name already
   established earlier (same guide, section 6.2) -- tracked here as
   "whichever Act was most recently named explicitly", defaulting to the
   Bill's own eventual Act until a different one is introduced.

Nothing here marks a link as human-verified -- every record carries
verified_at=None until a reviewer confirms it (same convention as
link_annotations.py), whatever its confidence.
"""
import difflib
import re

from ai_pipeline.hierarchy import UNIT_BOUNDARY_TYPES, UNIT_ROOT_TYPES

# A Victorian Act's short title is always "Title Words... Act YYYY" (see
# the OCPC guide's own section 6.2: "Victorian Acts are referred to by
# their short title and year"), set in genuine Title Case apart from a
# small, fixed set of lower-case connector words ("Interpretation of
# Legislation Act 1984", "Crimes (Mental Impairment and Unfitness to be
# Tried) Act 1997"). Every word must be either capitalised (optionally
# wrapped in brackets, for a parenthetical qualifier) or one of those
# connectors -- critically, *not* simply "any word at all", which would
# just as happily swallow an ordinary sentence's own leading capital and
# everything between it and the next "Act YYYY" it happens to reach ("As
# the note to the clause indicates, the Electronic Transactions Act 2000"
# is not itself an Act name; only "Electronic Transactions Act 2000" is).
_ACT_CONNECTOR_WORDS = r"of|the|and|for|in|to|on|or|be"
_ACT_TITLE_WORD = r"\(?[A-Z][\w'(),]*"
_ACT_NAME_RE = re.compile(
    r"\b(" + _ACT_TITLE_WORD + r"(?:\s+(?:" + _ACT_TITLE_WORD + "|" + _ACT_CONNECTOR_WORDS + r")){0,12}?\s+Act\s+\d{4})\b"
)

# "section 44A", "sections 19A to 19C", "section 3(1)" -- the forward-
# referencing style the OCPC guide's section 6.3 describes (a pinpoint
# subsection reference is folded into the section number, never split
# out on its own).
_SECTION_REF_RE = re.compile(
    r"\bsections?\s+([\w.]+(?:\([\w.]+\))?(?:\s*(?:to|and|,)\s*[\w.]+(?:\([\w.]+\))?)*)", re.IGNORECASE
)

# "the Principal Act", "this Act", "the Act" -- refers to whichever Act
# is currently in scope rather than naming one (see resolve_em_links).
_ACT_SELF_ALIAS_RE = re.compile(r"\b(?:the\s+principal\s+act|this\s+act|the\s+act)\b", re.IGNORECASE)

# The action an amending clause takes -- surfaced in each link record so
# a reviewer sees at a glance what kind of change is being described,
# matching the OCPC clause-note checklist's own question ("does each
# clause note refer to whether inserting/substituting/repealing?").
_ACTION_VERB_RE = re.compile(r"\b(inserts?|substitutes?|repeals?|amends?)\b", re.IGNORECASE)

TEXT_SIMILARITY_MATCH_THRESHOLD = 0.6


def _collapse_whitespace(text: str) -> str:
    """Joins the source PDF's own line-wrap "\n"s back into flowing text
    -- case preserved, unlike _normalize_text below -- so a title or
    reference that happens to wrap across one (see extract_em_target)
    still matches whole."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _normalize_text(text: str) -> str:
    return _collapse_whitespace(text).lower()


def text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_text(a), _normalize_text(b)).ratio()


def _unit_full_text(nodes: list[dict], root_idx: int) -> str:
    """The full text of a Section/Clause -- its own lead-in plus every
    node nested under it up to the next boundary-type node (see
    hierarchy.py's UNIT_BOUNDARY_TYPES) -- joined into one blob for
    similarity comparison. A clause/section's own "text" field alone is
    frequently near-empty (a lead-in sentence, or nothing at all, with the
    substantive content sitting in its nested subsections/paragraphs as
    separate flat nodes), so comparing only that would tell two provisions
    apart on almost no signal."""
    parts = [nodes[root_idx].get("text") or ""]
    i = root_idx + 1
    while i < len(nodes) and nodes[i]["type"] not in UNIT_BOUNDARY_TYPES:
        parts.append(nodes[i].get("text") or "")
        i += 1
    return "\n".join(p for p in parts if p)


def match_bill_to_act(bill_nodes: list[dict], act_nodes: list[dict]) -> list[dict]:
    """[{"clause_number", "bill_node_index", "act_node_index",
    "act_section_number", "similarity", "status", "verified_at"}, ...] for
    every Bill clause, in Bill document order. act_section_number is the
    matched section's own number rather than only its position: a stored
    node index goes stale the moment a reviewer merges a node away, and
    anything reading these records back later (see
    ai_pipeline/commentary.py) needs a handle on the section that
    survives that. status is "matched" (a same-numbered Act section exists and
    reads similarly enough), "flagged" (a same-numbered section exists
    but the text has diverged enough that a human should look -- a House
    amendment likely touched this provision, or shifted what sits at this
    number), or "unmatched" (no section with this number exists in the
    Act at all)."""
    act_by_number: dict[str, int] = {}
    for idx, node in enumerate(act_nodes):
        if node["type"] in UNIT_ROOT_TYPES and node.get("number"):
            act_by_number.setdefault(node["number"].lower(), idx)

    links = []
    for idx, node in enumerate(bill_nodes):
        if node["type"] not in UNIT_ROOT_TYPES or not node.get("number"):
            continue
        act_idx = act_by_number.get(node["number"].lower())
        record = {
            "clause_number": node["number"],
            "bill_node_index": idx,
            "act_node_index": act_idx,
            "act_section_number": act_nodes[act_idx]["number"] if act_idx is not None else None,
            "similarity": None,
            "status": "unmatched",
            "verified_at": None,
        }
        if act_idx is not None:
            similarity = text_similarity(_unit_full_text(bill_nodes, idx), _unit_full_text(act_nodes, act_idx))
            record["similarity"] = round(similarity, 3)
            record["status"] = "matched" if similarity >= TEXT_SIMILARITY_MATCH_THRESHOLD else "flagged"
        links.append(record)
    return links


def extract_em_target(entry_text: str) -> dict:
    """Pulls whatever an EM entry's own text reveals about what it
    describes: the first action verb (insert/substitute/repeal/amend, if
    any), the first Act name or self-referencing alias, and the first
    section reference. Returns {"action", "act_name", "act_is_self_alias",
    "section_ref"} -- any of which may be None if that entry's text
    doesn't mention it (a purely explanatory note with no amendment at
    all, for instance, names no Act and no section). The entry's stored
    text has the source PDF's own line-wrap points as literal "\n"s (the
    convention throughout this pipeline -- see markdown_export.py's
    _reflow), which would otherwise break a match for an Act name that
    happens to wrap across one; matched here against a whitespace-
    normalised copy instead."""
    text = _collapse_whitespace(entry_text)
    action_m = _ACTION_VERB_RE.search(text)
    act_m = _ACT_NAME_RE.search(text)
    section_m = _SECTION_REF_RE.search(text)
    act_is_self_alias = False
    act_name = None
    if act_m:
        act_name = act_m.group(1)
    else:
        alias_m = _ACT_SELF_ALIAS_RE.search(text)
        if alias_m:
            act_is_self_alias = True
    return {
        "action": action_m.group(1).lower() if action_m else None,
        "act_name": act_name,
        "act_is_self_alias": act_is_self_alias,
        "section_ref": section_m.group(1) if section_m else None,
    }


def resolve_em_links(
    em_nodes: list[dict],
    bill_slug: str,
    act_slug: str,
    bill_to_act: list[dict],
    known_acts: dict[str, str] | None = None,
    act_registry: dict[str, dict] | None = None,
) -> list[dict]:
    """[{"em_node_index", "clause_number", "target", "verified_at"}, ...]
    for every EM entry, in document order. `target` is one of:

      - {"kind": "act_section", "act_slug", "act_title", "section_ref",
        "in_force"} -- an explicitly-named Act (found in `known_acts`,
        slug -> title) or the currently-in-scope alias ("the Principal
        Act"/"this Act"), tracked across entries in order: whichever Act
        was most recently named explicitly stays in scope until a
        different one is named. "in_force" is only present when the name
        was also found in `act_registry` (see below) -- not every real
        Act citation will be.
      - {"kind": "bill_clause", "act_slug", "clause_number"} -- no Act
        name and no alias were found, so this entry is explaining one of
        the Bill's own provisions; resolved to the Act this Bill itself
        becomes via `bill_to_act`.
      - None -- nothing in the entry's text identified a target at all
        (a general/overview note, for instance).

    known_acts (slug -> title, e.g. ai_pipeline.link_targets.
    load_known_acts()) resolves a *named* Act to a slug an eventual link
    can point *into* (this pipeline has actually parsed that Act).
    act_registry (title -> metadata, e.g. ai_pipeline.act_registry.
    load_act_registry()) is the fallback for a real Act this pipeline
    hasn't parsed -- confirms the citation names a genuine Act and its
    current in-force status, still with no slug to link into. A name in
    neither is kept as act_slug=None with the raw title still recorded
    rather than silently discarded, so a reviewer can see what needs
    adding to one registry or the other."""
    known_acts = known_acts or {}
    act_registry = act_registry or {}
    title_to_slug = {title: slug for slug, title in known_acts.items()}
    bill_clause_numbers = {link["clause_number"] for link in bill_to_act}

    def act_section_target(slug: str | None, title: str | None, section_ref: str | None) -> dict:
        target = {"kind": "act_section", "act_slug": slug, "act_title": title, "section_ref": section_ref}
        registry_entry = act_registry.get(title) if title else None
        if registry_entry is not None:
            target["in_force"] = registry_entry["in_force"]
        return target

    links = []
    # Starts as the Bill's own eventual Act -- see the module docstring:
    # an alias with nothing explicit named yet always means "this Bill",
    # since every EM opens with clauses explaining the Bill's own
    # provisions before any consequential amendment to another Act.
    current_act_slug: str | None = act_slug
    current_act_title: str | None = None

    for idx, node in enumerate(em_nodes):
        # "em_entry" is what EMs parsed before the type change emitted --
        # see em_parser.py's docstring; still accepted so an older parse
        # links the same way.
        if node["type"] not in ("clause", "em_entry"):
            continue
        found = extract_em_target(node.get("text") or "")
        target = None

        if found["act_name"]:
            current_act_title = found["act_name"]
            current_act_slug = title_to_slug.get(found["act_name"])
            target = act_section_target(current_act_slug, current_act_title, found["section_ref"])
        elif found["act_is_self_alias"] or found["section_ref"]:
            # "the Principal Act"/"this Act", or a bare "section N" with
            # no Act named at all -- both mean whichever Act is currently
            # in scope.
            target = act_section_target(current_act_slug, current_act_title, found["section_ref"])
        elif node.get("number") in bill_clause_numbers:
            # No Act named, no section referenced, but this entry's own
            # number matches a real Bill clause -- it's explaining the
            # Bill's own provision at that clause.
            target = {"kind": "bill_clause", "act_slug": act_slug, "clause_number": node["number"]}

        links.append({"em_node_index": idx, "clause_number": node.get("number"), "target": target, "verified_at": None})

    return links
