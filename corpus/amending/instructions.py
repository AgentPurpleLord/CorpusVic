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
insert_at_end, insert_definition, replace_provision, insert_provision,
repeal, unparsed.
"""
import re

_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-"})

# "section 4(1)(f)", "the heading to section 366", "section 3(1) (definition
# of recording)" -- the provision an instruction is about.
_TARGET = (r"(?P<heading>the heading to )?(?:[Ss]ection|[Cc]lause) (?P<section>\d+[A-Z]*)"
           r"(?P<path>(?:\([0-9A-Za-z.]+\))*)")
_DEFINITION = r'the definition of (?P<term>"[^"]+"|[\w ]+?) in '
_ACT = r"(?: of the (?P<act>Principal Act|[A-Z][\w ,()'-]*? Act \d{4}))?"

_IN_TARGET = re.compile(rf"^In (?:{_DEFINITION})?{_TARGET}{_ACT}\s*[,—]?\s*(?P<ops>.+)$", re.S)
_FOR_TARGET = re.compile(rf"^For (?:{_DEFINITION})?{_TARGET}{_ACT} substitute\s*—?\s*(?P<block>.+)$", re.S)
_AFTER_TARGET = re.compile(rf"^(?P<where>After|Before) {_TARGET}{_ACT} insert\s*—?\s*(?P<block>.+)$", re.S)
_AT_END = re.compile(rf"^At the end of {_TARGET}{_ACT} insert\s*—?\s*(?P<block>.+)$", re.S)
_REPEAL = re.compile(rf"^{_TARGET}{_ACT} (?:is|are) repealed\.?$", re.S)
_REPEAL_DEF = re.compile(rf"^In {_TARGET}{_ACT}\s*,?\s*the definition of (?P<term>\"[^\"]+\"|[\w ]+?) is repealed\.?$", re.S)

_OPS = (
    (re.compile(r'^for "(?P<old>.+?)" substitute "(?P<new>.*?)"$', re.S), "substitute"),
    (re.compile(r'^after "(?P<old>.+?)" insert "(?P<new>.+?)"$', re.S), "insert_after"),
    (re.compile(r'^before "(?P<old>.+?)" insert "(?P<new>.+?)"$', re.S), "insert_before"),
    (re.compile(r'^omit "(?P<old>.+?)"$', re.S), "omit"),
    (re.compile(r'^"(?P<old>.+?)" (?:is|are) omitted$', re.S), "omit"),
    (re.compile(r'^at the end (?:of the paragraph |of the subsection )?insert "(?P<new>.+?)"$', re.S), "insert_at_end"),
    (re.compile(r'^insert the following definitions? in (?:the appropriate )?alphabetical order\s*—?\s*(?P<new>.+)$', re.S),
     "insert_definition"),
)

# An item that narrows the provision further: "(a) in paragraph (b), for ...".
_NARROW = re.compile(r"^in (?:sub)?(?:section|paragraph|subparagraph|clause) (?P<path>(?:\([0-9A-Za-z.]+\))+),?\s*")

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
    m = _MARKER.match(body)
    return (m.group("number"), body[m.end():].strip()) if m else (None, body)


def _base(m, principal: "str | None", provision: str, raw: str) -> dict:
    act = m.groupdict().get("act")
    return {
        "provision": provision,
        "target_act": principal if act in (None, "Principal Act") else clean(act),
        "section": m.group("section"),
        "path": _path(m.group("path")),
        "heading": bool(m.groupdict().get("heading")),
        "definition": _term(m.groupdict().get("term")),
        "action": None, "old": None, "new": None, "number": None,
        "raw": raw,
    }


def _ops(text: str) -> list[tuple]:
    """(item, op text) for each instruction in an "In section X" body."""
    text = text.strip().rstrip(".").strip()
    starts = [m for m in _ITEM.finditer(text) if m.start() == 0 or text[m.start()] in ";—"]
    if not starts:
        return [(None, text)]
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        out.append((m.group("item"), text[m.end():end].strip().rstrip(";").strip()))
    return out


def read_provision(text: str, provision: str, principal: "str | None" = None) -> list[dict]:
    """The instructions in one amending provision's words. `principal` is
    the Act "the Principal Act" means where this provision is."""
    raw = clean(text)
    m = _IN_TARGET.match(raw)
    if m:
        base = _base(m, principal, provision, raw)
        out = []
        for item, op in _ops(m.group("ops")):
            cite = f"{provision}({item})" if item else provision
            path = base["path"]
            narrow = _NARROW.match(op)
            if narrow:
                path, op = path + _path(narrow.group("path")), op[narrow.end():]
            for pattern, action in _OPS:
                found = pattern.match(op)
                if found:
                    new = found.groupdict().get("new")
                    if action == "insert_definition":
                        new = _block(new)[1]
                    out.append({**base, "provision": cite, "path": path, "action": action,
                                "old": found.groupdict().get("old"), "new": new})
                    break
            else:
                out.append({**base, "provision": cite, "path": path, "action": "unparsed", "raw": op})
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
            if pattern is _AT_END and number is None:
                out["action"] = "insert_at_end"
            elif pattern is _AFTER_TARGET and m.group("where") == "Before":
                out["before"] = True
            return [out]
    m = _REPEAL.match(raw)
    if m:
        return [{**_base(m, principal, provision, raw), "action": "repeal"}]
    return [{"provision": provision, "target_act": principal, "section": None, "path": [], "heading": False,
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
    section, sub, parts = None, None, []

    def flush():
        if section is not None and parts:
            cite = f"s. {section}" + (f"({sub})" if sub else "")
            out.extend(read_provision(" ".join(parts), cite, principal))

    for node in nodes:
        t, text = node.get("type"), clean(node.get("text") or "")
        heading = clean(node.get("heading") or "")
        if t in ("part", "division", "schedule", "heading_group"):
            flush()
            section, sub, parts = None, None, []
            found = _AMENDMENT_OF.search(heading)
            if found:
                principal = found.group("act")
            continue
        found = _PRINCIPAL.search(text)
        if found:
            principal = found.group("act")
        if t == "section":
            flush()
            section, sub, parts = node.get("number"), None, [text] if text else []
        elif t == "subsection" and section is not None:
            flush()
            sub, parts = node.get("number"), [text] if text else []
        elif section is not None and text:
            marker = f"({node['number']}) " if node.get("number") and t in ("paragraph", "subparagraph") else ""
            parts.append(marker + text)
    flush()
    # Definitions of "Principal Act" say what later instructions mean;
    # they are not themselves instructions.
    return [i for i in out if not (i["action"] == "unparsed" and _PRINCIPAL.search(i["raw"]))]
