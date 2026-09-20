"""
For one Act, works out which Bill clause each of its sections came from,
and what that Bill's Explanatory Memorandum says about it. This is the
useful, reader-facing side of the link records run_bill_linking.py
writes into data/bill_links/.

Those records are built from the Bill's and EM's point of view (per Bill
clause, per EM entry, in their own document order). Reading an Act,
though, you want the opposite: standing on section 28, what explains it?
Flipping the index around like that is all this module does. There are
two ways to reach a section, and both use data that's already been
worked out elsewhere -- nothing here re-reads a PDF or re-runs any
matching:

    EM entry, about Bill clause N   -> Bill clause N -> Act section M
    EM entry, about "section 44A"   -> section 44A directly

The first case is the common one, for the Act the Bill actually became:
an entry explaining one of the Bill's own provisions doesn't name any
Act at all (see bill_linking.resolve_em_links), so it's matched up
through the Bill's own clause-to-section mapping. The second case is how
an EM for a *different* Bill still reaches this Act -- through a
consequential-amendment note that names it directly.

Sections are looked up by number, not by position in the parse. A link
record's act_node_index is a position that was valid when the link was
made, and review.py's merges can shift those positions around -- the
section's own number stays valid, and it's what a reader is looking at
anyway.
"""
import re

# "44A" on its own, or the leading section of a pinpoint like "44A(2)".
# Deliberately strict: extract_em_target's section_ref is whatever
# followed the word "section" in running prose, which is sometimes not a
# section number at all ("section in which..." yields "in").
_SECTION_NUMBER_RE = re.compile(r"^(\d+[A-Za-z]*)")


def section_numbers_in_ref(section_ref: str | None) -> list[str]:
    """The section number(s) named in a raw reference: "44A" -> ["44A"],
    "3(1)" -> ["3"], "22 and 23" -> ["22", "23"]. A range ("19A to 19C")
    only returns its first endpoint -- the sections in between are real,
    but aren't actually named, and guessing them would attach commentary
    to sections the EM never mentioned."""
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
    """How good a clause-to-section match is, for choosing between
    duplicates: a confirmed match beats a flagged one, and between two
    confirmed (or two flagged) matches, the higher text similarity
    wins."""
    return (entry.get("status") == "matched", entry.get("similarity") or 0.0)


def provision_key(schedule: "str | None", number: "str | None") -> tuple:
    """How a provision is identified across documents: its number *and*
    which Schedule it's in. A Schedule starts numbering its own
    provisions from 1 again, so the Criminal Procedure Act's section 11
    and its Schedule 1 clause 11 are different provisions that happen to
    share a number. Keying by number alone would mix up their
    commentary."""
    return (str(schedule).lower() if schedule else None, str(number or "").lower())


def build_commentary_index(act_slug: str, bill_link_docs: list[dict], em_link_docs: list[dict]) -> dict[tuple, dict]:
    """provision_key(schedule, number) -> {"bill": [...], "em": [...]}.

    A "bill" entry is {bill_slug, clause_number, status, similarity} --
    the Bill clause this section was enacted from, keeping the match's
    own confidence so the reader can see a "flagged" match for what it
    is, rather than being told a shaky guess as settled fact.

    An "em" entry is {em_slug, em_node_index, clause_number, schedule,
    via} -- "via" says which of the two routes above reached this
    section ("bill_clause" or "act_section"), and "schedule" is the Bill
    Schedule the EM entry sits under (None if it's in the body), since a
    Schedule starts numbering its own clauses from 1 again.

    Both input lists are the documents run_bill_linking.py writes; any
    documents about other Acts are ignored, so a caller can just hand
    over everything in data/bill_links/.
    """
    index: dict[tuple, dict] = {}

    def bucket(schedule: "str | None", number: str) -> dict:
        return index.setdefault(provision_key(schedule, number), {"bill": [], "em": []})

    # (Bill Schedule, clause number) -> the Act provision it became, as
    # (Act Schedule, section number). One of these per Bill that became
    # this Act.
    act_provision_by_clause: dict[str, dict[tuple, tuple]] = {}
    for doc in bill_link_docs:
        if doc.get("act_slug") != act_slug:
            continue
        bill_slug = doc.get("bill_slug")
        mapping: dict[tuple, tuple] = {}
        for link in doc.get("links") or []:
            section_number = link.get("act_section_number")
            if not section_number:
                continue
            act_schedule = link.get("act_schedule")
            mapping.setdefault(
                provision_key(link.get("schedule"), link["clause_number"]),
                (act_schedule, section_number),
            )
            candidate = {
                "bill_slug": bill_slug,
                "clause_number": link["clause_number"],
                "schedule": link.get("schedule"),
                "status": link.get("status"),
                "similarity": link.get("similarity"),
            }
            # Two link records can still name the same clause of the same
            # Schedule -- a Bill can end up with the same clause number
            # twice in one Schedule if a House amendment renumbered
            # things around it. They can't both be the provision this
            # section came from, so keep only the best match rather than
            # showing the reader the same chip twice with two different
            # confidences.
            entries = bucket(act_schedule, section_number)["bill"]
            existing = next(
                (
                    e for e in entries
                    if e["bill_slug"] == bill_slug
                    and provision_key(e["schedule"], e["clause_number"])
                    == provision_key(candidate["schedule"], candidate["clause_number"])
                ),
                None,
            )
            if existing is None:
                entries.append(candidate)
            elif _match_rank(candidate) > _match_rank(existing):
                entries[entries.index(existing)] = candidate
        act_provision_by_clause[bill_slug] = mapping

    for doc in em_link_docs:
        em_slug = doc.get("em_slug")
        bill_slug = doc.get("bill_slug")
        for link in doc.get("links") or []:
            target = link.get("target")
            if not target or target.get("act_slug") != act_slug:
                continue
            if target["kind"] == "bill_clause":
                found = act_provision_by_clause.get(bill_slug, {}).get(
                    provision_key(target.get("schedule"), target["clause_number"])
                )
                provisions = [found] if found else []
                via = "bill_clause"
            elif target["kind"] == "act_section":
                # A section named directly in the entry's text ("section
                # 44A"). Prose like this always names one of the Act's
                # own body sections, never a Schedule item.
                provisions = [(None, n) for n in section_numbers_in_ref(target.get("section_ref"))]
                via = "act_section"
            else:
                continue
            for act_schedule, number in provisions:
                bucket(act_schedule, number)["em"].append({
                    "em_slug": em_slug,
                    "em_node_index": link["em_node_index"],
                    "clause_number": link.get("clause_number"),
                    "schedule": link.get("schedule"),
                    "via": via,
                })

    return index
