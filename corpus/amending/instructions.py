"""What an amending provision tells the principal Act to do.

Victorian amending Acts are drafted in a small set of fixed forms --
"In section 4(1)(f) of the Criminal Procedure Act 2009, for "x"
substitute "y"." -- so each is read by pattern, not by a model. A
sentence none of them fits is kept as `unparsed` with its words, so it
still reaches a reviewer rather than disappearing.

One instruction is {"provision", "target_act", "section", "path",
"heading", "definition", "action", "old", "new", "number", "raw"}:
`provision` is how margin notes cite it ("s. 82", "s. 67(1)(a)"),
`section` and `path` name the provision it changes ("4", ["1", "f"]),
and `action` is one of substitute, insert_after, insert_before, omit,
insert_at_end, insert_definition, replace_definition, replace_provision,
insert_provision, insert_section, repeal, unparsed.
"""
import re

_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-"})

# "section 4(1)(f)", "the heading to section 366", "section 3(1) (definition
# of recording)" -- the provision an instruction is about.
_TARGET = (r"(?P<heading>the heading to )?(?:[Ss]ection|[Cc]lause|[Ii]tem) (?P<section>\d+[A-Z]*(?:\.\d+[A-Z]*)?)"
           r"(?P<path>(?:\([0-9A-Za-z.]+\))*)(?P<also>(?:(?:,| and) (?:\([0-9A-Za-z.]+\))+)*)"
           r"(?: (?:of|in|to) Schedule (?P<schedule>\d+[A-Z]*))?")
_DEFINITION = r'the definition of (?P<term>"[^"]+"|[\w ]+?) in '
_ACT = r"(?: (?:of|to) the (?P<act>Principal Act|[A-Z][\w ,()'-]*? Act \d{4}))?"
_NAMES_ACT = re.compile(r" (?:of|to) the (?P<act>[A-Z][\w ,()'-]*? Act \d{4})")

_IN_TARGET = re.compile(rf"^In (?:{_DEFINITION})?{_TARGET}{_ACT}\s*[,—]?\s*(?P<ops>.+)$", re.S)
_FOR_TARGET = re.compile(rf"^For (?:{_DEFINITION})?{_TARGET}{_ACT} substitute\s*—?\s*(?P<block>.+)$", re.S)
_AFTER_TARGET = re.compile(rf"^(?P<where>After|Before) {_TARGET}{_ACT} insert\s*—?\s*(?P<block>.+)$", re.S)
_AT_END = re.compile(rf"^At the end of {_TARGET}{_ACT} insert\s*—?\s*(?P<block>.+)$", re.S)
_REPEAL = re.compile(rf"^{_TARGET}{_ACT} (?:is|are) repealed\.?$", re.S)
_REPEAL_DEF = re.compile(rf"^In {_TARGET}{_ACT}\s*,?\s*the definition of (?P<term>\"[^\"]+\"|[\w ]+?) is repealed\.?$", re.S)

# "for "accused" (wherever occurring) substitute ..." -- every occurrence,
# which the matcher checks the same way as one.
_EVERY = r"(?:\s*\((?:wherever|where \w+) occurring\))?"

_OPS = (
    (re.compile(rf'^for "(?P<old>.+?)"{_EVERY} substitute "(?P<new>.*?)"$', re.S), "substitute"),
    (re.compile(r'^after "(?P<old>.+?)" insert "(?P<new>.+?)"$', re.S), "insert_after"),
    (re.compile(r'^before "(?P<old>.+?)" insert "(?P<new>.+?)"$', re.S), "insert_before"),
    (re.compile(rf'^omit "(?P<old>.+?)"{_EVERY}$', re.S), "omit"),
    (re.compile(r'^"(?P<old>.+?)" (?:is|are) omitted$', re.S), "omit"),
    (re.compile(r'^at the end (?:of the paragraph |of the subsection )?insert "(?P<new>.+?)"$', re.S), "insert_at_end"),
    (re.compile(r'^insert the following definitions?(?: in (?:the appropriate )?alphabetical order)?\s*—?\s*(?P<new>.+)$', re.S),
     "insert_definition"),
    (re.compile(r'^for the definition of (?P<term>.+?) substitute\s*—?\s*(?P<new>.+)$', re.S), "replace_definition"),
)

# An item that narrows the provision further: "(a) in paragraph (b), for ...".
_NARROW = re.compile(r"^in (?:sub)?(?:section|paragraph|subparagraph|clause) (?P<path>(?:\([0-9A-Za-z.]+\))+),?\s*")
# ... or that puts a whole provision in: "(b) after paragraph (b) insert— "(c) ..."".
_ITEM_PROVISION = re.compile(r"^(?P<where>after|before|for) (?:sub)?(?:section|paragraph|subparagraph|clause) "
                             r"(?P<path>(?:\([0-9A-Za-z.]+\))+) (?:insert|substitute)\s*—?\s*(?P<block>.+)$", re.S)
# "In section 4 of the ... Act, in the definition of court— (a) ...".
_IN_DEFINITION = re.compile(r"^in the definition of (?P<term>.+?)\s*(?:—|,)\s*")

# A list of instructions to one provision: "In section 4— (a) for ...;
# (b) omit ...". The items are split on their own markers.
_ITEM = re.compile(r"(?:^|;\s*(?:and\s+)?|—\s*)\((?P<item>[a-z]{1,3})\)\s+")
_MARKER = re.compile(r"^\((?P<number>[0-9A-Za-z]+)\)\s*")


def clean(text: str) -> str:
    """One way of writing quotes, dashes and spaces, so a pattern need
    only know one. PDFs give curly quotes, and sometimes straight."""
    return re.sub(r"\s+", " ", (text or "").translate(_QUOTES)).strip()


def _path(raw: str) -> list[str]:
    return re.findall(r"\(([^()]+)\)", raw or "")


def _term(raw: "str | None") -> "str | None":
    return raw.strip().strip('"').strip() if raw else None


def _block(raw: str) -> tuple:
    """(its own number, its words) for provision text given in quotes after
    "insert—" or "substitute—": '"(ba) any other matter;".'"""
    body = clean(raw).rstrip(".").strip()
    if body.startswith('"') and body.endswith('"'):
        body = body[1:-1].strip()
    m = _MARKER.match(body) or _NEW_SECTION.match(body)
    return (m.group("number"), body[m.end():].strip()) if m else (None, body)


# A whole section put in: '"464 Transitional provision— ...'.
_NEW_SECTION = re.compile(r"^(?P<number>\d+[A-Z]*)\s+")


_STYLES = (("num", r"\d"), ("roman", r"^[ivxl]+$"), ("lower", r"^[a-z]"), ("upper", r"^[A-Z]"))


def _style(segment: str) -> str:
    return next(name for name, pattern in _STYLES + (("other", r""),) if re.search(pattern, segment))


def _targets(path: list[str], also: str) -> list[list[str]]:
    """Every provision "(2)(b)(ii)(A) and (iii)" names: a further target
    takes the place of the segment numbered in its own style -- (iii)
    replaces (ii), not (A)."""
    out = [path]
    for group in re.findall(r"(?:,| and) ((?:\([0-9A-Za-z.]+\))+)", also or ""):
        extra = _path(group)
        style = _style(extra[0])
        at = max((i for i, seg in enumerate(path) if _style(seg) == style), default=len(path))
        out.append(path[:at] + extra)
    return out


def _base(m, principal: "str | None", provision: str, raw: str) -> dict:
    act = m.groupdict().get("act")
    schedule = m.groupdict().get("schedule")
    # A Schedule's item or clause is the first step of the path within
    # that Schedule, the way a section's subsection is within the section.
    path = ([m.group("section")] if schedule else []) + _path(m.group("path"))
    return {
        "provision": provision,
        "target_act": principal if act in (None, "Principal Act") else clean(act),
        "schedule": schedule,
        "section": None if schedule else m.group("section"),
        "path": path,
        "heading": bool(m.groupdict().get("heading")),
        "definition": _term(m.groupdict().get("term")),
        "action": None, "old": None, "new": None, "number": None,
        "raw": raw,
    }


def _outside_quotes(text: str, at: int) -> bool:
    return text[:at].count('"') % 2 == 0


def _ops(text: str) -> list[tuple]:
    """(item, op text) for each instruction in an "In section X" body --
    split only on markers outside quotes, since the words being inserted
    carry markers of their own."""
    text = text.strip().rstrip(".").strip()
    starts = [m for m in _ITEM.finditer(text)
              if (m.start() == 0 or text[m.start()] in ";—") and _outside_quotes(text, m.start())]
    if not starts:
        return [(None, text)]
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        out.append((m.group("item"), text[m.end():end].strip().rstrip(";").strip()))
    return out


def _read_op(base: dict, path: list[str], cite: str, op: str) -> dict:
    definition = _IN_DEFINITION.match(op)
    if definition:
        base, op = {**base, "definition": _term(definition.group("term"))}, op[definition.end():]
    narrow = _NARROW.match(op)
    if narrow:
        path, op = path + _path(narrow.group("path")), op[narrow.end():]
    whole = _ITEM_PROVISION.match(op)
    if whole:
        number, words = _block(whole.group("block"))
        action = "replace_provision" if whole.group("where") == "for" else "insert_provision"
        return {**base, "provision": cite, "path": path + _path(whole.group("path")), "action": action,
                "number": number, "new": words, **({"before": True} if whole.group("where") == "before" else {})}
    for pattern, action in _OPS:
        found = pattern.match(op)
        if found:
            new = found.groupdict().get("new")
            if action in ("insert_definition", "replace_definition"):
                new = _block(new)[1]
            term = found.groupdict().get("term")
            return {**base, "provision": cite, "path": path, "action": action,
                    "old": found.groupdict().get("old"), "new": new,
                    **({"definition": _term(term)} if term else {})}
    return {**base, "provision": cite, "path": path, "action": "unparsed", "raw": op}


def read_provision(text: str, provision: str, principal: "str | None" = None) -> list[dict]:
    """The instructions in one amending provision's words. `principal` is
    the Act "the Principal Act" means where this provision is."""
    raw = clean(text)
    m = _IN_TARGET.match(raw)
    if m:
        base = _base(m, principal, provision, raw)
        ops = m.group("ops")
        definition = _IN_DEFINITION.match(ops)
        if definition:
            base["definition"], ops = _term(definition.group("term")), ops[definition.end():]
        out = []
        items = _ops(ops)
        lead = raw[:m.start("ops")].strip()
        for target in _targets(base["path"], m.group("also")):
            for item, op in items:
                # Each item keeps the words that are its own, under the
                # sentence they complete -- not the whole provision again.
                own = f"{lead} ({item}) {op}" if item and len(items) > 1 else raw
                out.append(_read_op({**base, "raw": own}, target, f"{provision}({item})" if item else provision, op))
        return out
    m = _REPEAL_DEF.match(raw)
    if m:
        return [{**_base(m, principal, provision, raw), "action": "repeal", "definition": _term(m.group("term"))}]
    for pattern, action in ((_FOR_TARGET, "replace_provision"), (_AFTER_TARGET, "insert_provision"),
                            (_AT_END, "insert_provision")):
        m = pattern.match(raw)
        if m:
            number, words = _block(m.group("block"))
            out = {**_base(m, principal, provision, raw), "action": action, "number": number, "new": words}
            # "After section 463 ... insert— "464 ..."" makes a provision of
            # its own, found by its own number rather than inside 463.
            if action == "insert_provision" and not out["path"] and not out["schedule"] and number \
                    and number[0].isdigit():
                out.update(action="insert_section", section=number)
            elif pattern is _AT_END and number is None:
                out["action"] = "insert_at_end"
            elif pattern is _AFTER_TARGET and m.group("where") == "Before":
                out["before"] = True
            return [out]
    m = _REPEAL.match(raw)
    if m:
        return [{**_base(m, principal, provision, raw), "action": "repeal"}]
    named = _NAMES_ACT.search(raw)
    return [{"provision": provision, "target_act": clean(named.group("act")) if named else principal,
             "schedule": None, "section": None, "path": [], "heading": False,
             "definition": None, "action": "unparsed", "old": None, "new": None, "number": None, "raw": raw}]


_PRINCIPAL = re.compile(r"Principal Act means the (?P<act>[A-Z][\w ,()'-]*? Act \d{4})")
_AMENDMENT_OF = re.compile(r"Amendment of (?:the )?(?P<act>[A-Z][\w ,()'-]*? Act \d{4})")


def read_act(nodes: list[dict]) -> list[dict]:
    """Every instruction in an amending Act, from its parse.

    Walks the Act in order, keeping what "the Principal Act" means (a Part
    headed "Amendment of the X Act", or a section saying "In this Part,
    Principal Act means the X Act") and reading each section's words: the
    section's own text where it has no subsections, else each subsection
    with its paragraphs as the list items they are.
    """
    principal = None
    out: list[dict] = []
    cite, parts = None, []
    section = schedule = None

    def flush():
        if cite and parts:
            out.extend(read_provision(" ".join(parts), cite, principal))

    for node in nodes:
        t, text = node.get("type"), clean(node.get("text") or "")
        heading = clean(node.get("heading") or "")
        if t in ("part", "division", "schedule", "schedule_act", "heading_group"):
            flush()
            cite, parts = None, []
            found = _AMENDMENT_OF.search(heading)
            if found:
                principal = found.group("act")
            if t == "schedule":
                schedule = (node.get("number"), node.get("section"))
            elif t == "schedule_act":
                principal = heading
            continue
        found = _PRINCIPAL.search(text)
        if found:
            principal = found.group("act")
        if t == "section":
            flush()
            section, schedule = node.get("number"), None
            cite, parts = f"s. {section}", [text] if text else []
        elif t == "subsection" and section is not None:
            flush()
            cite, parts = f"s. {section}({node.get('number')})", [text] if text else []
        elif t == "item" and schedule:
            flush()
            number, enacted_by = schedule
            cite, parts = f"s. {enacted_by}(Sch. {number} item {node.get('number')})", [text] if text else []
        elif cite and text:
            marker = f"({node['number']}) " if node.get("number") and t in ("paragraph", "subparagraph") else ""
            parts.append(marker + text)
    flush()
    # What names no Act to amend is not an instruction: purposes,
    # commencement, what "the Principal Act" means, repeal of this Act.
    return [i for i in out if not (i["action"] == "unparsed" and (i["target_act"] is None or _PRINCIPAL.search(i["raw"])))]
