"""
Works out, for one Act, which Bill clause each of its sections came from
and what that Bill's Explanatory Memorandum says about it -- the "so
what" of the link records run_bill_linking.py writes into
data/bill_links/.

Those records are built from the Bill's and EM's side (per Bill clause,
per EM entry, in their own document order). Reading an Act, you want the
opposite index: standing on section 28, what explains it? That inversion
is all this module does. Two routes reach a section, both of them
already-computed link data -- nothing here re-reads any PDF or re-runs
any matching:

    EM entry --(target: bill_clause N)--> Bill clause N --> Act section M
    EM entry --(target: act_section, section_ref "44A")--> section 44A

The first is the common case for the Act the Bill actually became: an
entry explaining one of the Bill's own provisions names no Act at all
(see bill_linking.resolve_em_links), so it resolves through the Bill's
own clause->section match. The second is how an EM for *some other* Bill
reaches this Act: a consequential-amendment note that names it
explicitly.

Sections are keyed by number, not node index. A link record's
act_node_index is a position in the parse as it stood when the link was
made, and review.py's merges shift those; the section's own number is
what survives -- and is what a reader is looking at anyway.
"""
import re

# "44A" on its own, or the leading section of a pinpoint like "44A(2)".
# Deliberately strict: extract_em_target's section_ref is whatever
# followed the word "section" in running prose, which is sometimes not a
# section number at all ("section in which..." yields "in").
_SECTION_NUMBER_RE = re.compile(r"^(\d+[A-Za-z]*)")


def section_numbers_in_ref(section_ref: str | None) -> list[str]:
    """The section number(s) a raw reference names: "44A" -> ["44A"],
    "3(1)" -> ["3"], "22 and 23" -> ["22", "23"]. A range ("19A to 19C")
    yields only its first endpoint -- the sections in between are real but
    aren't named, and inventing them would attach commentary to sections
    the EM never actually mentioned."""
    if not section_ref:
        return []
    head = re.split(r"\s+to\s+", section_ref, maxsplit=1)[0]  # a range: first endpoint only
    numbers: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"\s*(?:,|\band\b)\s*", head):
        m = _SECTION_NUMBER_RE.match(token.strip())
        if m and m.group(1).lower() not in seen:
            seen.add(m.group(1).lower())
            numbers.append(m.group(1))
    return numbers


def _match_rank(entry: dict) -> tuple:
    """How good a clause->section match is, for picking between repeats:
    a confirmed match beats a flagged one, and among equals the higher
    text similarity wins."""
    return (entry.get("status") == "matched", entry.get("similarity") or 0.0)


def build_commentary_index(act_slug: str, bill_link_docs: list[dict], em_link_docs: list[dict]) -> dict[str, dict]:
    """section number (lower-cased) -> {"bill": [...], "em": [...]}.

    A "bill" entry is {bill_slug, clause_number, status, similarity} --
    the Bill clause this section was enacted from, with the match's own
    confidence carried through so the reader can see a "flagged" match for
    what it is rather than being told a shaky guess as fact.

    An "em" entry is {em_slug, em_node_index, clause_number, via} --
    "via" being "bill_clause" or "act_section", i.e. which of the two
    routes above reached this section.

    Both input lists are the documents run_bill_linking.py writes; docs
    about other Acts are ignored, so a caller can simply hand over
    everything in data/bill_links/.
    """
    index: dict[str, dict] = {}

    def bucket(number: str) -> dict:
        return index.setdefault(number.lower(), {"bill": [], "em": []})

    # clause number -> Act section number, per Bill that became this Act.
    section_by_clause: dict[str, dict[str, str]] = {}
    for doc in bill_link_docs:
        if doc.get("act_slug") != act_slug:
            continue
        bill_slug = doc.get("bill_slug")
        mapping: dict[str, str] = {}
        for link in doc.get("links") or []:
            section_number = link.get("act_section_number")
            if not section_number:
                continue
            mapping.setdefault(str(link["clause_number"]), section_number)
            candidate = {
                "bill_slug": bill_slug,
                "clause_number": link["clause_number"],
                "status": link.get("status"),
                "similarity": link.get("similarity"),
            }
            # A Bill numbers its Schedules' own clauses from 1 again, just
            # as an Act does its Schedules' items (see assign_filenames'
            # own note), so several link records can carry the same clause
            # number and land on the same section. They can't all be the
            # provision this section came from; keep the best-matching one
            # rather than showing the reader the same chip three times
            # with three different confidences.
            entries = bucket(section_number)["bill"]
            existing = next(
                (e for e in entries if e["bill_slug"] == bill_slug and e["clause_number"] == candidate["clause_number"]),
                None,
            )
            if existing is None:
                entries.append(candidate)
            elif _match_rank(candidate) > _match_rank(existing):
                entries[entries.index(existing)] = candidate
        section_by_clause[bill_slug] = mapping

    for doc in em_link_docs:
        em_slug = doc.get("em_slug")
        bill_slug = doc.get("bill_slug")
        for link in doc.get("links") or []:
            target = link.get("target")
            if not target or target.get("act_slug") != act_slug:
                continue
            if target["kind"] == "bill_clause":
                section_number = section_by_clause.get(bill_slug, {}).get(str(target["clause_number"]))
                numbers = [section_number] if section_number else []
                via = "bill_clause"
            elif target["kind"] == "act_section":
                numbers = section_numbers_in_ref(target.get("section_ref"))
                via = "act_section"
            else:
                continue
            for number in numbers:
                bucket(number)["em"].append({
                    "em_slug": em_slug,
                    "em_node_index": link["em_node_index"],
                    "clause_number": link.get("clause_number"),
                    "via": via,
                })

    return index
