"""
Exports a parsed Act (data/ai_parsed/<act>.json, merged with whatever
review.py has since verified in data/legislation.db) to Akoma Ntoso XML
(OASIS AKN v1.0, schema namespace akn/3.0).

This is an export step, not a rewrite of the pipeline: extraction, the rule
parser, history-note linking, diagnostics, and review.py's editing workflow
are unchanged -- this module just serializes whichever node list you point
it at into a conformant .xml file.

Structural mapping (verified against the actual OASIS akomantoso30.xsd,
not recalled from memory):
  chapter/part/division/subdivision/section/subsection/paragraph/subparagraph
      -> the native AKN elements of the same name. All eight exist directly
      in the core vocabulary's hierarchy group, so no generic <hcontainer>
      workaround is needed for any of them. ("chapter" only appears for
      Acts whose profile puts it in the hierarchy -- see hierarchy.py.)
  heading_group (a bare topical heading with no number, e.g. "Fraud and
      blackmail") -> <crossHeading>, AKN's element for exactly this: "a
      heading placed side by side with hierarchical containers."
  note / definition / example / repealed / schedule / sub_subparagraph ->
      <hcontainer name="...">, the generic escape hatch for a
      jurisdiction-specific container with no matching core element.
      schedule and sub_subparagraph are part of this pipeline's own
      hierarchy_order (see hierarchy.py -- both still nest with full
      parent/child fidelity via build_hierarchy_tree's own rank-based
      logic below) but have no *confirmed* native AKN element: a real
      Schedule properly belongs in AKN as a separate <attachment>
      document component, not a body hierarchy element, and nesting one
      level past AKN's own native subparagraph isn't a documented
      element either -- see _NATIVE_HIERARCHY_TYPES.
  A node's own text becomes <intro> if it has children (text introducing
  the nested list) or <content><p> if it's a leaf.

Amendment history -> full lifecycle/analysis modelling, not
temporalGroup/period: temporalGroup+period is for encoding *multiple
alternate wordings* of the same provision in one file (point-in-time
versioning), which doesn't apply here -- these PDFs are a single
consolidated snapshot, not a multi-expression series. What the margin notes
actually describe is a straightforward "provision X was later amended by
Act Y" fact, which AKN represents as:
  <meta><lifecycle>            one dated <eventRef> per distinct amending Act
  <meta><analysis>
    <passiveModifications>     one <textualMod> per (note, provision) link,
                                 source=the amending Act, destination=the
                                 provision's eId
  <meta><references>           one <passiveRef>/<TLCOrganization> per
                                 distinct amending Act / issuing authority

Real limitation this surfaces: <eventRef>'s date attribute requires a full
YYYY-MM-DD per the schema, but a citation like "No. 49/1991" only gives a
year, and pre-1970s Victorian Act numbers ("No. 8679") don't even give
that. Where a year is known, YYYY-01-01 is used as a documented
day/month-unknown placeholder. Where it isn't, the note is kept as a plain
<meta><notes><note> annotation instead of a fabricated dated event --
faking a date would be worse than not having one.
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .extract import reflow
from .hierarchy import HIERARCHY_ORDER, make_ranks

AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
ET.register_namespace("", AKN_NS)

# The eight levels verified against the real OASIS schema (see the module
# docstring). "schedule" and "sub_subparagraph" are also part of this
# pipeline's own hierarchy_order (see hierarchy.py) but have no confirmed
# native AKN element of their own. ("clause" is in the native set below on
# the same footing as the rest: AKN 3.0 defines <clause> as a hierarchy
# element -- verified against the schema in tests/fixtures, same as every
# other name here.) -- a real Schedule is properly an AKN
# <attachment>, a separate document component outside the main body's
# hierarchy entirely, which this export doesn't attempt to model, and
# nesting one level past AKN's own native subparagraph isn't a documented
# element either. Both render as the same generic <hcontainer> escape
# hatch "note"/"definition"/"example" already use (see render_tree_node)
# rather than guessing at an unverified element name -- they still
# participate fully in build_hierarchy_tree's own rank-based nesting
# below, since that's a question of tree *structure*, independent of
# which XML element ends up wrapping each node.
_NATIVE_HIERARCHY_TYPES = {"chapter", "part", "division", "subdivision", "section", "clause", "subsection", "paragraph", "subparagraph"}

# Native AKN element name per hierarchy type (identical to the type name
# here, but kept explicit in case a profile ever needs to remap one).
HIERARCHY_ELEMENT = {level: level for level in _NATIVE_HIERARCHY_TYPES}

EID_PREFIX = {
    "schedule": "sched", "chapter": "chp", "part": "part", "division": "div", "subdivision": "subdiv",
    "section": "sec", "clause": "cl", "subsection": "subsec", "definition": "def",
    "paragraph": "para", "subparagraph": "subpara", "sub_subparagraph": "subsubpara",
}

# Part/Division/Section/Schedule numbers are written bare in the source
# ("Part I", "Division 1", "3 Punishment for murder", "Schedule 1");
# Subdivision/Subsection/Paragraph/Subparagraph/Sub-subparagraph are
# always bracketed ("(1)", "(a)", "(i)", "(A)") -- the rule parser strips
# the brackets when capturing the number, so restore them here to match
# both the source text and standard AKN <num> style.
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
# Tree reconstruction: the node list is flat and ordered (see tree.py); this
# replays the same stack-based logic to get real parent/child nesting, which
# XML needs but the JSON form deliberately doesn't carry.
# ---------------------------------------------------------------------------

def build_hierarchy_tree(nodes: list[dict], hierarchy_order: list[str] = HIERARCHY_ORDER) -> list[dict]:
    """Reconstructs real nesting from the flat, ordered node list. eId
    collisions are possible and not actually a bug in this function: e.g. a
    "Definitions" section can contain several independent defined terms,
    each with its own unnumbered (a)/(b) list, directly under the section
    with no numbered subsection between them to disambiguate -- the source
    text itself doesn't distinguish these, so two separate lists can both
    legitimately produce "para_b". AKN requires every eId to be unique
    document-wide, so collisions here get a disambiguating numeric suffix;
    this is flagged by the caller as worth a human look, not silently
    resolved as if it were unambiguous."""
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
            idx = rank[t]
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
                # A bare topical heading grouping a run of sections always
                # sits between two Sections (or before the first one in a
                # Division/Part) -- never inside one -- however deep the
                # stack happened to be when the rules engine noticed it
                # text-wise (it's appended without going through the
                # open_node/stack machinery above, since it isn't itself a
                # hierarchy level). Pop down the same way a new Section
                # opening would, so it attaches as a sibling of sections
                # under the enclosing Division/Part instead of getting
                # buried inside whatever subsection happened to be open.
                section_idx = rank["section"]
                while level_stack and level_stack[-1][0] >= section_idx:
                    level_stack.pop()
            parent_level, parent = level_stack[-1]
            # "definition" reaches here only if `rank` has no "subsection"
            # entry at all to alias it onto (see hierarchy.py's
            # make_ranks) -- a pathological profile missing that level
            # entirely; the ordinary case is handled by the `if t in
            # rank:` branch above instead, with proper popping/nesting so
            # a defined term's own (a)/(b) list attaches under it.
            prefix = {"note": "note", "example": "ex", "heading_group": "hd", "definition": "def"}.get(t, "el")
            token = f"{prefix}_{sum(1 for c in parent['children'] if c['node']['type'] == t) + 1}"
            eid = unique(f"{parent['eid']}__{token}" if parent["eid"] else token)
            parent["children"].append({"node": node, "eid": eid, "children": []})

    return root["children"], collisions


def _render_p(parent_el, text: str) -> None:
    p = ET.SubElement(parent_el, _q("p"))
    # Reflowed, not raw: the stored text carries the source PDF's own
    # line-wrap points, and an AKN consumer should get the provision's
    # words, not the page's layout (see extract.reflow).
    p.text = reflow(text)


def render_tree_node(tree_node: dict, top_level: bool = False, hierarchy_order: list[str] = HIERARCHY_ORDER) -> ET.Element:
    node = tree_node["node"]
    t = node["type"]

    if t in _NATIVE_HIERARCHY_TYPES:
        el = ET.Element(_q(HIERARCHY_ELEMENT.get(t, t)), {"eId": tree_node["eid"]})
    elif t == "heading_group" and not top_level:
        # <crossHeading> ("a heading placed side by side with hierarchical
        # containers") is only valid nested inside a hierarchy element's own
        # content model -- not as a direct child of <body> itself, which a
        # heading_group can land as if it occurs before any Part is opened
        # (e.g. leftover front-matter text). Fall back to a generic
        # hcontainer there instead.
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
    else:
        content = ET.SubElement(el, _q("content"))
        _render_p(content, node.get("text") or "")

    return el


# ---------------------------------------------------------------------------
# Amendment history -> lifecycle / analysis / references / notes
# ---------------------------------------------------------------------------

# A modern citation is matched on its "NN/YYYY" shape alone, without
# requiring the "No." in front: a note citing several Acts writes the word
# once and then lists bare numbers ("amended by Nos 26/2014 s. 455(Sch.
# item 8.1), 19/2019 s. 258(a), 39/2022 s. 39"), so a prefix-anchored
# pattern silently found only the first -- or, with "Nos", none at all.
# Nothing else in a margin note takes this shape (checked against every
# note in the Criminal Procedure Act: 52 distinct bare matches, all of them
# real Acts in its own Table of Amendments).
_MODERN_CITATION_RE = re.compile(r"\b(\d{1,5})\s*/\s*((?:18|19|20)\d{2})\b")
# A pre-1970s Act has no year in its number at all, and a bare 4-5 digit
# number is not safely a citation on its own -- so this one does need the
# "No."/"Nos" in front, and a second, unprefixed number in such a list
# ("Nos 8679, 9576") is left unmatched rather than guessed at.
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


def _extract_citations(raw: str) -> list[dict]:
    """Every amending-Act citation named in one note, each as {label, act_no,
    year (or None if undatable)}. A note commonly cites more than one Act
    ("substituted by Nos 8679 s. 2, 37/1986 s. 8, amended by ...")."""
    citations = []
    seen_spans = set()
    for m in _MODERN_CITATION_RE.finditer(raw):
        citations.append({"label": f"No. {m.group(1)}/{m.group(2)}", "act_no": m.group(1), "year": int(m.group(2))})
        seen_spans.add(m.span())
    for m in _OLD_CITATION_RE.finditer(raw):
        if any(s[0] <= m.start() < s[1] for s in seen_spans):
            continue  # already captured as part of a modern "NN/YYYY" match
        citations.append({"label": f"No. {m.group(1)}", "act_no": m.group(1), "year": None})
    return citations


def _mod_type_for(raw: str) -> str:
    lower = raw.lower()
    for keyword, mod_type in _MOD_TYPE_KEYWORDS:
        if keyword in lower:
            return mod_type
    return "substitution"


def collect_history_events(tree_roots: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Walks the tree once, returning (citation_refs, event_refs,
    textual_mods) for every history note with a datable citation. A note
    with no datable citation (no embeddable year -- pre-1970s Victorian Act
    numbers give no year at all) can't honestly become a dated <eventRef>,
    so instead of faking one, this appends it directly into the tree as an
    inline body annotation right where it occurred (mutates tree_node in
    place) -- same mechanism as the rule parser's own inline "note" nodes,
    rather than a fabricated <meta><notes> cross-reference."""
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
    """Best-effort Act title/number/year for the FRBR metadata block, read
    from the PDF's own front matter rather than guessed."""
    result = {"title": None, "act_no": None, "year": None}
    if not source_pdf or not Path(source_pdf).exists():
        return result
    from .extract import extract_pages

    pages = extract_pages(source_pdf)
    text = "\n".join(p.body for p in pages[:2])
    title_m = re.search(r"^([A-Z][\w' \-]+?\sAct\s(\d{4}))\s*$", text, re.MULTILINE)
    if title_m:
        result["title"] = title_m.group(1)
        result["year"] = int(title_m.group(2))
    no_m = re.search(r"No\.\s*(\d+)\s+of\s+(\d{4})", text)
    if no_m:
        result["act_no"] = no_m.group(1)
        result["year"] = int(no_m.group(2))
    return result


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

    # coreProperties (shared by Work/Expression/Manifestation, in this exact
    # order per the schema): FRBRthis, FRBRuri, FRBRdate, FRBRauthor, then
    # each level's own properties (FRBRcountry / FRBRlanguage / FRBRformat).
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
