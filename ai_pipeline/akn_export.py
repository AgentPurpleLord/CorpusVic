"""
Exports a parsed Act (data/ai_parsed/<act>.json, merged with whatever
review.py has verified so far in data/legislation.db) to Akoma Ntoso
XML (OASIS AKN v1.0, schema namespace akn/3.0).

This is an export step, not a rewrite of the pipeline: extraction, the
rule parser, history-note linking, diagnostics and review.py's editing
workflow are all unchanged -- this module just turns whichever node
list you point it at into a valid .xml file.

Structural mapping (checked against the actual OASIS akomantoso30.xsd
schema file, not just remembered):
  chapter/part/division/subdivision/section/subsection/paragraph/
      subparagraph -> the native AKN elements of the same name. All
      eight exist directly in the core vocabulary's hierarchy group, so
      no generic <hcontainer> workaround is needed for any of them.
      ("chapter" only appears for Acts whose profile puts it in the
      hierarchy -- see hierarchy.py.)
  heading_group (a bare topical heading with no number, e.g. "Fraud and
      blackmail") -> <crossHeading>, AKN's own element for exactly
      this: "a heading placed side by side with hierarchical
      containers."
  note / definition / example / repealed / schedule / sub_subparagraph
      -> <hcontainer name="...">, the generic fallback for a
      jurisdiction-specific container with no matching core element.
      schedule and sub_subparagraph are part of this pipeline's own
      hierarchy_order (see hierarchy.py -- both still nest correctly
      with the rest via build_hierarchy_tree's own logic below), but
      have no *confirmed* native AKN element: a real Schedule properly
      belongs in AKN as a separate <attachment> document component,
      not a body hierarchy element, and nesting one level past AKN's
      own native subparagraph isn't a documented element either -- see
      _NATIVE_HIERARCHY_TYPES.
  A node's own text becomes <intro> if it has children (text
  introducing the nested list) or <content><p> if it's a leaf.

Amendment history maps to full lifecycle/analysis modelling, not
temporalGroup/period: temporalGroup+period is for encoding *multiple
alternate wordings* of the same provision in one file (point-in-time
versioning), which doesn't apply here -- these PDFs are a single
consolidated snapshot, not a series of alternate versions in one file.
What the margin notes actually describe is a plain "provision X was
later amended by Act Y" fact, which AKN represents as:
  <meta><lifecycle>            one dated <eventRef> per distinct amending Act
  <meta><analysis>
    <passiveModifications>     one <textualMod> per (note, provision) link,
                                 source=the amending Act, destination=the
                                 provision's eId
  <meta><references>           one <passiveRef>/<TLCOrganization> per
                                 distinct amending Act / issuing authority

A real limitation shows up here: <eventRef>'s date attribute needs a
full YYYY-MM-DD under the schema, but a citation like "No. 49/1991"
only gives a year, and pre-1970s Victorian Act numbers ("No. 8679")
don't even give that. Where a year is known, YYYY-01-01 is used as a
clearly-documented day/month-unknown placeholder. Where it isn't, the
note is kept as a plain <meta><notes><note> annotation instead of a
made-up dated event -- inventing a date would be worse than not having
one.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .extract import reflow
from .hierarchy import HIERARCHY_ORDER, make_ranks
from .tables import split_rows
from .versions import read_front_matter

AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
ET.register_namespace("", AKN_NS)

# The eight levels checked against the real OASIS schema (see the
# module docstring). "schedule" and "sub_subparagraph" are also part of
# this pipeline's own hierarchy_order (see hierarchy.py) but have no
# confirmed native AKN element of their own. ("clause" is in the native
# set below on the same footing as the rest: AKN 3.0 defines <clause>
# as a hierarchy element -- checked against the schema in
# tests/fixtures, same as every other name here.) A real Schedule
# properly belongs in AKN as an <attachment>, a separate document
# component outside the main body's hierarchy entirely, which this
# export doesn't attempt to model, and nesting one level past AKN's
# own native subparagraph isn't a documented element either. Both
# render using the same generic <hcontainer> fallback "note",
# "definition", "example" and "penalty" already use (see
# render_tree_node) rather
# than guessing at an unverified element name -- they still nest
# correctly through build_hierarchy_tree's logic below regardless,
# since that's a question of tree *structure*, separate from which XML
# element ends up wrapping each node.
_NATIVE_HIERARCHY_TYPES = {"chapter", "part", "division", "subdivision", "section", "clause", "subsection", "paragraph", "subparagraph"}

# Native AKN element name per hierarchy type (identical to the type name
# here, but kept explicit in case a profile ever needs to remap one).
HIERARCHY_ELEMENT = {level: level for level in _NATIVE_HIERARCHY_TYPES}

EID_PREFIX = {
    "schedule": "sched", "chapter": "chp", "part": "part", "division": "div", "subdivision": "subdiv",
    "section": "sec", "clause": "cl", "subsection": "subsec", "definition": "def",
    # AKN's own name for the text that resumes a provision's sentence
    # after its list has finished -- the counterpart to the "intro" the
    # provision opened with.
    "continuation": "wrapup",
    "paragraph": "para", "subparagraph": "subpara", "sub_subparagraph": "subsubpara",
}

# Part/Division/Section/Schedule numbers are written bare in the
# source ("Part I", "Division 1", "3 Punishment for murder", "Schedule
# 1"); Subdivision/Subsection/Paragraph/Subparagraph/Sub-subparagraph
# are always bracketed ("(1)", "(a)", "(i)", "(A)") -- the rule parser
# strips the brackets when capturing the number, so they're restored
# here to match both the source text and standard AKN <num> style.
BRACKETED_LEVELS = {"subdivision", "subsection", "paragraph", "subparagraph", "sub_subparagraph"}


def _format_num(node_type: str, number: str) -> str:
    return f"({number})" if node_type in BRACKETED_LEVELS else number


def _q(tag: str) -> str:
    return f"{{{AKN_NS}}}{tag}"


def _sanitize_token(s: str | None) -> str:
    if not s:
        return "u"
    s = s.strip("()").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "u"


# ---------------------------------------------------------------------------
# Tree reconstruction: the node list is flat and ordered (see tree.py);
# this replays the same stack-based logic to get real parent/child
# nesting, which XML needs but the JSON form deliberately doesn't
# carry.
# ---------------------------------------------------------------------------

def build_hierarchy_tree(nodes: list[dict], hierarchy_order: list[str] = HIERARCHY_ORDER) -> list[dict]:
    """Rebuilds real nesting from the flat, ordered node list. eId
    collisions can happen, and that's not actually a bug in this
    function: e.g. a "Definitions" section can contain several
    independent defined terms, each with its own unnumbered (a)/(b)
    list, directly under the section with no numbered subsection
    between them to tell them apart -- the source text itself doesn't
    distinguish these, so two separate lists can both legitimately
    produce "para_b". AKN requires every eId to be unique across the
    whole document, so a collision here gets a numeric suffix added to
    make it unique; the caller flags this as worth a human look, rather
    than silently treating it as if it were never ambiguous."""
    rank = make_ranks(hierarchy_order)
    root = {"node": None, "eid": None, "children": []}
    level_stack = [(-1, root)]
    used_eids: set[str] = set()
    collisions: list[str] = []

    def unique(eid: str) -> str:
        if eid not in used_eids:
            used_eids.add(eid)
            return eid
        n = 2
        while f"{eid}_{n}" in used_eids:
            n += 1
        collisions.append(eid)
        used_eids.add(f"{eid}_{n}")
        return f"{eid}_{n}"

    for node in nodes:
        t = node["type"]
        if t in rank:
            # depth_rank, where the parser recorded one, is where this
            # node actually sits: a definition introduced by a lead-in
            # inside a subsection belongs under that subsection, not
            # beside it at the depth its type alone implies (see
            # rule_parser._note_definitions_lead_in).
            idx = node.get("depth_rank", rank[t])
            while level_stack and level_stack[-1][0] >= idx:
                level_stack.pop()
            parent_level, parent = level_stack[-1]
            token = f"{EID_PREFIX[t]}_{_sanitize_token(node.get('number'))}"
            eid = unique(f"{parent['eid']}__{token}" if parent["eid"] else token)
            tree_node = {"node": node, "eid": eid, "children": []}
            parent["children"].append(tree_node)
            level_stack.append((idx, tree_node))
        else:
            if t == "heading_group":
                # A bare topical heading grouping a run of sections
                # always sits between two Sections (or before the
                # first one in a Division/Part) -- never inside one --
                # no matter how deep the stack happened to be when the
                # rules engine noticed it (it's appended without going
                # through the open_node/stack machinery above, since it
                # isn't itself a hierarchy level). Pop back the same
                # way a new Section opening would, so it attaches as a
                # sibling of sections under the enclosing Division/Part
                # instead of getting buried inside whatever subsection
                # happened to be open.
                section_idx = rank["section"]
                while level_stack and level_stack[-1][0] >= section_idx:
                    level_stack.pop()
            elif t == "penalty":
                # A penalty attaches to the provision that creates the
                # offence, and that is a Section or a Subsection -- never
                # a lettered list item, and never the wrap-up line that
                # closes one. It is printed after the whole provision, so
                # whatever happened to be open when the rules engine
                # reached it is usually the deepest thing in the list
                # above it: left alone, s 4's penalty came out as
                # "sec_4__wrapup_u__pnlty_1" and a subsection's as
                # "...subsec_1__para_b__pnlty_1", where no consumer
                # looking for that provision's penalty would find it.
                subsection_idx = rank.get("subsection", rank["section"])
                while level_stack and (
                    level_stack[-1][0] > subsection_idx
                    or (level_stack[-1][1]["node"] or {}).get("type") == "continuation"
                ):
                    level_stack.pop()
            parent_level, parent = level_stack[-1]
            # "definition" reaches here only if `rank` has no
            # "subsection" entry at all for it to line up with (see
            # hierarchy.py's make_ranks) -- an unusual profile missing
            # that level entirely; the ordinary case is handled by the
            # `if t in rank:` branch above instead, with proper popping
            # and nesting so a defined term's own (a)/(b) list attaches
            # under it.
            prefix = {
                "note": "note", "example": "ex", "heading_group": "hd", "definition": "def",
                # AKN has no native element for a penalty, so it renders
                # through the same <hcontainer name="penalty"> fallback
                # a note does; the eId still names it for what it is.
                "penalty": "pnlty", "table": "tbl",
            }.get(t, "el")
            token = f"{prefix}_{sum(1 for c in parent['children'] if c['node']['type'] == t) + 1}"
            eid = unique(f"{parent['eid']}__{token}" if parent["eid"] else token)
            parent["children"].append({"node": node, "eid": eid, "children": []})

    return root["children"], collisions


def _render_p(parent_el, text: str) -> None:
    p = ET.SubElement(parent_el, _q("p"))
    # Reflowed, not raw: the stored text carries the source PDF's own
    # line-wrap points, and an AKN consumer should get the provision's
    # actual words, not the page's layout (see extract.reflow).
    p.text = reflow(text)


def render_tree_node(tree_node: dict, top_level: bool = False, hierarchy_order: list[str] = HIERARCHY_ORDER) -> ET.Element:
    node = tree_node["node"]
    t = node["type"]

    if t in _NATIVE_HIERARCHY_TYPES:
        el = ET.Element(_q(HIERARCHY_ELEMENT.get(t, t)), {"eId": tree_node["eid"]})
    elif t == "heading_group" and not top_level:
        # <crossHeading> ("a heading placed side by side with
        # hierarchical containers") is only valid nested inside a
        # hierarchy element's own content -- not as a direct child of
        # <body> itself, which a heading_group can end up as if it
        # occurs before any Part is opened (e.g. leftover front-matter
        # text). Falls back to a generic hcontainer there instead.
        el = ET.Element(_q("crossHeading"), {"eId": tree_node["eid"]})
        el.text = node.get("heading") or node.get("text") or ""
        return el
    else:
        el = ET.Element(_q("hcontainer"), {"eId": tree_node["eid"], "name": t})

    if node.get("number"):
        ET.SubElement(el, _q("num")).text = _format_num(t, node["number"])
    if node.get("heading"):
        ET.SubElement(el, _q("heading")).text = node["heading"]

    if tree_node["children"]:
        if node.get("text"):
            intro = ET.SubElement(el, _q("intro"))
            _render_p(intro, node["text"])
        for child in tree_node["children"]:
            el.append(render_tree_node(child, hierarchy_order=hierarchy_order))
    elif t == "table":
        _render_table(ET.SubElement(el, _q("content")), node.get("text") or "")
    else:
        content = ET.SubElement(el, _q("content"))
        _render_p(content, node.get("text") or "")

    return el


def _render_table(parent_el, text: str) -> None:
    """A table's rows, as a real AkomaNtoso <table>.

    Not one <p>: the rows are stored as text (see ai_pipeline/tables.py)
    and reflow would join them into a single paragraph, throwing away the
    one thing a table is -- which is exactly the state the rows were
    recovered from in the first place. AKN takes HTML's own table
    elements for this."""
    rows = split_rows(text)
    if not rows:
        _render_p(parent_el, "")
        return
    table_el = ET.SubElement(parent_el, _q("table"))
    for index, row in enumerate(rows):
        row_el = ET.SubElement(table_el, _q("tr"))
        for cell in row:
            cell_el = ET.SubElement(row_el, _q("th" if index == 0 else "td"))
            _render_p(cell_el, cell)


# ---------------------------------------------------------------------------
# Amendment history -> lifecycle / analysis / references / notes
# ---------------------------------------------------------------------------

# A modern citation is matched on its "NN/YYYY" shape alone, without
# requiring the word "No." in front: a note citing several Acts writes
# the word once and then lists bare numbers ("amended by Nos 26/2014 s.
# 455(Sch. item 8.1), 19/2019 s. 258(a), 39/2022 s. 39"), so a pattern
# anchored on the prefix would silently find only the first -- or, with
# "Nos", none at all. Nothing else in a margin note takes this shape
# (checked against every note in the Criminal Procedure Act: 52
# distinct bare matches, all of them real Acts in its own Table of
# Amendments).
_MODERN_CITATION_RE = re.compile(r"\b(\d{1,5})\s*/\s*((?:18|19|20)\d{2})\b")
# A pre-1970s Act has no year in its number at all, and a bare 4-5
# digit number isn't safely a citation on its own -- so this one does
# need the "No."/"Nos" in front, and a second, unprefixed number in
# such a list ("Nos 8679, 9576") is left unmatched rather than guessed
# at.
_OLD_CITATION_RE = re.compile(r"Nos?\.?\s*(\d{3,6})\b(?!\s*/)")

_MOD_TYPE_KEYWORDS = [
    ("inserted", "insertion"),
    ("insertion", "insertion"),
    ("substituted", "substitution"),
    ("substitution", "substitution"),
    ("repealed", "repeal"),
    ("repeal", "repeal"),
    ("renumbered", "renumbering"),
    ("amended", "substitution"),  # AKN's TextualMods has no generic "amendment" value;
                                   # a bare "amended by" is treated as a substitution,
                                   # the most common concrete form an amendment takes.
]


# The "No."/"Nos" a modern citation is usually introduced by. Not part
# of _MODERN_CITATION_RE itself -- the plural form writes it once and
# then lists bare numbers ("Nos 26/2014 s. 455, 68/2009 s. 3"), so
# requiring it there would only find the first. Matched separately,
# purely to widen a citation's reported *span* to cover the words a
# reader would consider part of it.
_CITATION_PREFIX_RE = re.compile(r"Nos?\.?\s*$")


def _extract_citations(raw: str) -> list[dict]:
    """Every amending-Act citation named in one note, each as {label,
    act_no, year (or None if undatable), start, end}. A note commonly
    cites more than one Act ("substituted by Nos 8679 s. 2, 37/1986 s.
    8, amended by ...").

    start/end mark the citation as it's actually written in `raw`,
    which isn't the same string as `label`: the label is normalised to
    "No. 68/2009", while the note itself may write "Nos 26/2014,
    68/2009" and give the second citation no "No." of its own. A
    caller marking up the note (linking each citation where it stands
    -- see amendments.linkify_note) needs the span, not the label."""
    citations = []
    seen_spans = set()
    for m in _MODERN_CITATION_RE.finditer(raw):
        prefix = _CITATION_PREFIX_RE.search(raw, 0, m.start())
        citations.append({
            "label": f"No. {m.group(1)}/{m.group(2)}", "act_no": m.group(1), "year": int(m.group(2)),
            "start": prefix.start() if prefix else m.start(), "end": m.end(),
        })
        seen_spans.add(m.span())
    for m in _OLD_CITATION_RE.finditer(raw):
        if any(s[0] <= m.start() < s[1] for s in seen_spans):
            continue  # already captured as part of a modern "NN/YYYY" match
        citations.append({
            "label": f"No. {m.group(1)}", "act_no": m.group(1), "year": None,
            "start": m.start(), "end": m.end(),
        })
    return sorted(citations, key=lambda c: c["start"])


def _mod_type_for(raw: str) -> str:
    lower = raw.lower()
    for keyword, mod_type in _MOD_TYPE_KEYWORDS:
        if keyword in lower:
            return mod_type
    return "substitution"


def collect_history_events(tree_roots: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Walks the tree once, returning (citation_refs, event_refs,
    textual_mods) for every history note with a datable citation. A
    note with no datable citation (no year to embed -- pre-1970s
    Victorian Act numbers give no year at all) can't honestly become a
    dated <eventRef>, so instead of making one up, this appends it
    directly into the tree as an inline body annotation right where it
    occurred (mutates tree_node in place) -- the same mechanism the
    rule parser's own inline "note" nodes use, rather than a made-up
    <meta><notes> cross-reference."""
    citation_refs: dict[str, dict] = {}  # act_no -> {eid, label, year, act_no}
    event_refs: dict[str, dict] = {}  # act_no -> {eid, date, act_eid}
    textual_mods: list[dict] = []

    def walk(tree_node):
        node = tree_node["node"]
        for h in node.get("history") or []:
            citations = _extract_citations(h["raw"])
            mod_type = _mod_type_for(h["raw"])
            datable = [c for c in citations if c["year"] is not None]
            if not datable:
                idx = sum(1 for c in tree_node["children"] if c["node"]["type"] == "note") + 1
                undated_eid = f"{tree_node['eid']}__note_undated_{idx}"
                tree_node["children"].append({
                    "node": {"type": "note", "number": None, "heading": None, "text": h["raw"]},
                    "eid": undated_eid, "children": [],
                })
                continue
            for c in datable:
                ref_key = c["act_no"]
                if ref_key not in citation_refs:
                    citation_refs[ref_key] = {"eid": f"act-{c['act_no']}", "label": c["label"], "year": c["year"], "act_no": c["act_no"]}
                    event_refs[ref_key] = {"eid": f"evt-{c['act_no']}", "date": f"{c['year']:04d}-01-01", "act_eid": f"act-{c['act_no']}"}
                textual_mods.append({
                    "eid": f"mod_{len(textual_mods) + 1}",
                    "type": mod_type,
                    "source_eid": citation_refs[ref_key]["eid"],
                    "destination_eid": tree_node["eid"],
                })
        for child in list(tree_node["children"]):
            walk(child)

    for root in tree_roots:
        walk(root)

    return list(citation_refs.values()), list(event_refs.values()), textual_mods


# ---------------------------------------------------------------------------
# Top-level document assembly
# ---------------------------------------------------------------------------

def _detect_act_citation(source_pdf: str | None) -> dict:
    """Best-effort Act title, number and year for the FRBR metadata
    block, read from the PDF's own front matter rather than guessed.

    versions.read_front_matter reads the same block (it also carries
    the Authorised Version number and as-at date, which the FRBR
    expression layer will want -- see versions.py), so this just narrows
    that down, rather than running a second set of patterns over the
    same six lines. It's also much cheaper: this used to re-extract the
    whole PDF through the body-line pipeline just to read two lines off
    page 1."""
    if not source_pdf:
        return {"title": None, "act_no": None, "year": None}
    meta = read_front_matter(source_pdf)
    return {k: meta[k] for k in ("title", "act_no", "year")}


def export_to_akn(parsed: dict, source_pdf: str | None = None) -> ET.ElementTree:
    nodes = parsed["nodes"]
    act_slug = parsed.get("act", "act")
    hierarchy_order = parsed.get("hierarchy") or HIERARCHY_ORDER
    citation = _detect_act_citation(source_pdf or parsed.get("source"))
    work_year = citation["year"] or "unknown-year"
    work_no = citation["act_no"] or act_slug

    tree_roots, eid_collisions = build_hierarchy_tree(nodes, hierarchy_order)
    if eid_collisions:
        print(
            f"  {len(eid_collisions)} eId collision(s) auto-disambiguated with a numeric suffix "
            "-- usually a section with more than one unnumbered (a)/(b)-style list directly "
            "inside it (e.g. several definitions each with their own list); worth a look.",
            file=sys.stderr,
        )
    citation_refs, event_refs, textual_mods = collect_history_events(tree_roots)

    akn = ET.Element(_q("akomaNtoso"))
    act = ET.SubElement(akn, _q("act"), {"name": "act", "contains": "singleVersion"})
    meta = ET.SubElement(act, _q("meta"))

    ident = ET.SubElement(meta, _q("identification"), {"source": "#source-pdf"})
    work_uri = f"/akn/au-vic/act/{work_year}/{work_no}"
    work_date = f"{citation['year']:04d}-01-01" if citation["year"] else "9999-01-01"

    # coreProperties (shared by Work/Expression/Manifestation, in this
    # exact order per the schema): FRBRthis, FRBRuri, FRBRdate,
    # FRBRauthor, then each level's own properties (FRBRcountry /
    # FRBRlanguage / FRBRformat).
    frbr_work = ET.SubElement(ident, _q("FRBRWork"))
    ET.SubElement(frbr_work, _q("FRBRthis"), {"value": f"{work_uri}/main"})
    ET.SubElement(frbr_work, _q("FRBRuri"), {"value": work_uri})
    ET.SubElement(frbr_work, _q("FRBRdate"), {"date": work_date, "name": "generation"})
    ET.SubElement(frbr_work, _q("FRBRauthor"), {"href": "#victoria-parliament"})
    ET.SubElement(frbr_work, _q("FRBRcountry"), {"value": "au"})

    frbr_expr = ET.SubElement(ident, _q("FRBRExpression"))
    ET.SubElement(frbr_expr, _q("FRBRthis"), {"value": f"{work_uri}/eng@/main"})
    ET.SubElement(frbr_expr, _q("FRBRuri"), {"value": f"{work_uri}/eng@"})
    ET.SubElement(frbr_expr, _q("FRBRdate"), {"date": work_date, "name": "generation"})
    ET.SubElement(frbr_expr, _q("FRBRauthor"), {"href": "#victoria-parliament"})
    ET.SubElement(frbr_expr, _q("FRBRlanguage"), {"language": "eng"})

    frbr_manif = ET.SubElement(ident, _q("FRBRManifestation"))
    ET.SubElement(frbr_manif, _q("FRBRthis"), {"value": f"{work_uri}/eng@/main.xml"})
    ET.SubElement(frbr_manif, _q("FRBRuri"), {"value": f"{work_uri}/eng@/main.xml"})
    ET.SubElement(frbr_manif, _q("FRBRdate"), {"date": work_date, "name": "generation"})
    ET.SubElement(frbr_manif, _q("FRBRauthor"), {"href": "#source-pdf"})
    ET.SubElement(frbr_manif, _q("FRBRformat"), {"value": "xml"})

    if event_refs:
        lifecycle = ET.SubElement(meta, _q("lifecycle"), {"source": "#source-pdf"})
        for ev in sorted(event_refs, key=lambda e: e["date"]):
            ET.SubElement(lifecycle, _q("eventRef"), {
                "eId": ev["eid"], "date": ev["date"], "type": "amendment", "source": f"#{ev['act_eid']}",
            })

    if textual_mods:
        analysis = ET.SubElement(meta, _q("analysis"), {"source": "#source-pdf"})
        passive = ET.SubElement(analysis, _q("passiveModifications"))
        for mod in textual_mods:
            tmod = ET.SubElement(passive, _q("textualMod"), {"eId": mod["eid"], "type": mod["type"]})
            ET.SubElement(tmod, _q("source"), {"href": f"#{mod['source_eid']}"})
            ET.SubElement(tmod, _q("destination"), {"href": f"#{mod['destination_eid']}"})

    references = ET.SubElement(meta, _q("references"), {"source": "#source-pdf"})
    ET.SubElement(references, _q("TLCOrganization"), {
        "eId": "source-pdf", "href": "/ontology/organization/au-vic/chief-parliamentary-counsel", "showAs": "Chief Parliamentary Counsel (Victoria)",
    })
    ET.SubElement(references, _q("TLCOrganization"), {
        "eId": "victoria-parliament", "href": "/ontology/organization/au-vic/parliament", "showAs": "Parliament of Victoria",
    })
    for ref in citation_refs:
        ET.SubElement(references, _q("passiveRef"), {
            "eId": ref["eid"], "href": f"/akn/au-vic/act/{ref['year']}/{ref['act_no']}/main", "showAs": ref["label"],
        })

    body = ET.SubElement(act, _q("body"))
    for root_node in tree_roots:
        body.append(render_tree_node(root_node, top_level=True, hierarchy_order=hierarchy_order))

    return ET.ElementTree(akn)


def write_akn(parsed: dict, out_path: str, source_pdf: str | None = None) -> None:
    tree = export_to_akn(parsed, source_pdf=source_pdf)
    ET.indent(tree, space="  ")
    tree.write(out_path, encoding="utf-8", xml_declaration=True)
