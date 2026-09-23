"""
One provision followed through every version of a work held here: which
of its wordings a version carries, who has vouched for them, and what a
reviewer still has to look at.

A Principal Act is reviewed at its current version. Every other version
is mostly the same words, and a provision whose words are the same as a
version somebody already checked has nothing left to check. So review
decisions are shared along a *run*: consecutive versions whose raw parse
of the provision is identical. Two raw parses come from the same
deterministic parser, so their artefacts are identical too, and a
reviewer's correction to one is a correction to all of them. Comparing a
reviewed text with an unreviewed one would not be: it would report the
reviewer's own fix as an amendment by Parliament (see
dashboard._timeline).

Nothing here is stored. Which provisions a version inherits is derived
each time from the parses and the review rows, so there is no second
record to fall out of step with the first.

A provision moved elsewhere in the Act is, to a comparison by number, a
repeal and an insertion. A reviewer can say otherwise -- "s 464AA was
carried from s 464A" -- and `links` is those decisions:
{version: {new_key: key_in_the_version_before}}.
"""
from corpus.domain import amendments, diffing

REVIEWED = "reviewed"      # this version's own rows cover the provision
INHERITS = "inherits"      # same raw words as a version where it is reviewed
FOLLOWS = "follows"        # same raw words as the next version toward current;
                           # its review happens there, not here
TO_REVIEW = "to_review"    # different words, or nobody to vouch for them


def signatures(nodes: list[dict]) -> dict[tuple, tuple]:
    """{provision key -> (heading, whole-unit text)}, the thing two
    versions are compared on. diffing.provisions has already normalised
    away line wraps and the printer's whitespace."""
    return {key: (p["heading"], p["text"]) for key, p in diffing.provisions(nodes).items()}


def step(links: dict, key: tuple, frm: int, to: int) -> tuple:
    """What `key` in version `frm` is called in the adjacent version `to`,
    through any carried-from link across that boundary."""
    if to > frm:
        carried = {old: new for new, old in (links.get(to) or {}).items()}
        return carried.get(key, key)
    return (links.get(frm) or {}).get(key, key)


def _run(versions: list[int], raw: dict, links: dict, position: int, key: tuple, direction: int):
    """The versions beyond `position` in `direction` whose raw wording of
    the provision is identical to this one's, nearest first, each with
    the key it has there. Stops at the first difference or absence."""
    here = raw[versions[position]].get(key)
    i = position + direction
    while here is not None and 0 <= i < len(versions):
        key = step(links, key, versions[i - direction], versions[i])
        if raw[versions[i]].get(key) != here:
            return
        yield versions[i], key
        i += direction


def review_status(versions: list[int], raw: dict, reviewed: dict, links: "dict | None" = None,
                  blocked: "dict | None" = None) -> dict:
    """{version -> {key -> {"status", "source", "source_key"}}}.

    `versions` oldest first, the last being current. `raw` is
    {version: signatures(raw parse)}; `reviewed` is {version: set of keys
    whose unit that version's own rows cover}; `blocked` is {version: set
    of keys that version's rows cannot lend to another}, which is where a
    reviewer restructured the unit (see inheritance.py).

    A provision inherits from the nearest version, newer first, along a
    run of identical raw wording. Newer first because the current version
    is where review happens; older too, because a newly added newer
    version should start from what was checked in the one before it.

    What cannot inherit is the reviewer's to do -- except, in a version
    that is not current, a provision whose words are the same as the next
    version's: that is reviewed once, in the newer version, and this one
    follows it. Only a difference ever lands in an older version's queue.
    """
    links = links or {}
    blocked = blocked or {}
    out: dict = {}
    for position, version in enumerate(versions):
        statuses = {}
        is_current = position == len(versions) - 1
        for key in raw[version]:
            if key in reviewed.get(version, ()):
                statuses[key] = {"status": REVIEWED, "source": version, "source_key": key}
                continue
            source = None
            for direction in (1, -1):
                for other, other_key in _run(versions, raw, links, position, key, direction):
                    if other_key in reviewed.get(other, ()) and other_key not in blocked.get(other, ()):
                        source = (other, other_key)
                        break
                if source:
                    break
            if source:
                statuses[key] = {"status": INHERITS, "source": source[0], "source_key": source[1]}
                continue
            newer = next(_run(versions, raw, links, position, key, 1), None)
            # Following waits for a review still to come. One already done
            # that could not be lent (blocked) is never coming.
            if not is_current and newer is not None and newer[1] not in reviewed.get(newer[0], ()):
                statuses[key] = {"status": FOLLOWS, "source": newer[0], "source_key": newer[1]}
            else:
                statuses[key] = {"status": TO_REVIEW, "source": None, "source_key": None}
        out[version] = statuses
    return out


def _same_wording(a: dict, b: dict) -> bool:
    """Whether two versions' texts of a provision are one wording. Equal
    raw parses are the same words by construction, whatever a reviewer
    did to one of them; otherwise the reviewed texts decide, with a
    reordering set aside for the reason diffing.is_reordering gives."""
    if a["raw"] is not None and a["raw"] == b["raw"]:
        return True
    if a["heading"] != b["heading"]:
        return False
    diff = diffing.word_diff(a["text"], b["text"])
    return not any(s["op"] != "equal" for s in diff) or diffing.is_reordering(diff)


def provision_chains(docs: list[dict], links: "dict | None" = None) -> dict:
    """Every provision's wordings across the versions, oldest first.

    `docs` is [{"version", "as_at", "as_at_printed", "raw", "effective",
    "checked"}] in any order: `raw` is signatures() of the raw parse,
    `effective` is diffing.provisions() of what a reader is shown, and
    `checked` is the set of keys a human has vouched for in that version.
    `loose_notes` (optional) is loose_notes() of that version, and
    `amending_acts` (optional) the citations in its Table of Amendments
    (see _amended).

    Returns {"chains": [...], "by_key": {(version, key): chain number}}.
    A chain is {"wordings": [...]}, and a wording is one span of versions
    with the same words -- or with none, "absent": True, which is how an
    insertion and a repeal are both recorded. A provision that has always
    read the same in every version held has a single wording, and so no
    history to show.

    Each present wording carries the provision as its best version shows
    it: the newest member someone checked, else the newest. Where a
    wording ends, `ended_by` names the next version and the margin notes
    it prints that the ending wording did not -- the Act's own account
    of what changed it.
    """
    links = links or {}
    ordered = sorted(docs, key=lambda d: d["version"])
    versions = [d["version"] for d in ordered]
    by_key: dict = {}
    chains: list = []

    for start, doc in enumerate(ordered):
        for key in doc["effective"]:
            if (doc["version"], key) in by_key:
                continue
            # A provision's chain is found from where it first appears,
            # so walking forward from here meets every later version.
            members = {start: key}
            k = key
            for i in range(start + 1, len(ordered)):
                k = step(links, k, versions[i - 1], versions[i])
                if k not in ordered[i]["effective"]:
                    break
                members[i] = k
            chain_no = len(chains)
            for i, k in members.items():
                by_key[(versions[i], k)] = chain_no
            chains.append(_wordings(ordered, members))
    return {"chains": chains, "by_key": by_key}


def _stamp(doc: dict) -> dict:
    return {"version": doc["version"], "as_at": doc.get("as_at"), "as_at_printed": doc.get("as_at_printed")}


def _cited(notes) -> set[str]:
    return {f"{c['act_no']}/{c['year']}" if c.get("year") else str(c["act_no"])
            for note in notes or [] for c in amendments.citations_in(note)}


def _notes_for(doc: dict, key) -> list[str]:
    return [*(doc["effective"][key].get("history") or []), *doc.get("loose_notes", {}).get(key, [])]


def _amended(ordered: list[dict], a: int, members: dict, b: int) -> bool:
    """Whether the Act itself says a provision changed between reprints a
    and b, once the parser has said its text differs.

    The parser's word alone isn't enough: the same words wrapped at a
    different line end read "charge- sheet" in one reprint and
    "charge-sheet" in the next, and on the Criminal Procedure Act 37 of 48
    such differences had nothing in the Act behind them. So the
    provision's own notes have to cite an amending Act they didn't before,
    and the later reprint's Table of Amendments has to list it.

    Not that the table has gained an Act: a reprint is also made when more
    of an Act already listed commences. The CPA's 26 April 2026 reprint
    added nothing to its table, and nine of its provisions were amended by
    No. 1/2026, listed since the reprint before.

    Where the later reprint has no parsed table, the text decides: a table
    that failed to parse would otherwise hide every amendment behind it."""
    # Where a person has reviewed the work's history (corpus/history), a
    # change is one they confirmed and nothing else: not the margin notes,
    # not the amending Acts, which they were shown as evidence.
    confirmed = ordered[b].get("confirmed")
    if confirmed is not None:
        return members[b] in confirmed
    said = act_records_amendment(_notes_for(ordered[a], members[a]), _notes_for(ordered[b], members[b]),
                                 ordered[b].get("amending_acts"))
    return True if said is None else said


def act_records_amendment(older_notes, newer_notes, listed) -> "bool | None":
    """The rule _amended applies, on its own inputs, so the review tool
    asks the same question of a unit that the public histories ask of a
    chain: do the newer notes cite an amending Act the older ones did not,
    and does the newer Table of Amendments (`listed`, its citations) list
    it? None where there is no table to ask."""
    if not listed:
        return None
    new = _cited(newer_notes) - _cited(older_notes)
    # An old note can cite by number alone ("No. 8679").
    listed = {*listed, *(c.split("/")[0] for c in listed)}
    return bool(new & listed)


def _wordings(ordered: list[dict], members: dict) -> dict:
    first, last = min(members), max(members)
    spans: list[dict] = []
    if first > 0:
        spans.append({"absent": True, "members": list(range(0, first))})
    for i in range(first, last + 1):
        doc, key = ordered[i], members[i]
        this = {"raw": doc["raw"].get(key), **{f: doc["effective"][key][f] for f in ("heading", "text")}}
        if spans and not spans[-1].get("absent") and (
                _same_wording(spans[-1]["probe"], this)
                or not _amended(ordered, spans[-1]["members"][-1], members, i)):
            spans[-1]["members"].append(i)
            continue
        spans.append({"absent": False, "members": [i], "probe": this})
    if last < len(ordered) - 1:
        spans.append({"absent": True, "members": list(range(last + 1, len(ordered)))})

    wordings = []
    for n, span in enumerate(spans):
        span_members = span["members"]
        wording = {
            "absent": span["absent"],
            "from": _stamp(ordered[span_members[0]]),
            "to": _stamp(ordered[span_members[-1]]),
            "versions": [ordered[i]["version"] for i in span_members],
        }
        if not span["absent"]:
            checked = [i for i in span_members if members[i] in ordered[i]["checked"]]
            best = (checked or span_members)[-1]
            key = members[best]
            wording.update({
                "key": key,
                "keys": {ordered[i]["version"]: members[i] for i in span_members},
                "version": ordered[best]["version"],
                "provision": ordered[best]["effective"][key],
                "checked": bool(checked),
            })
        if n + 1 < len(spans):
            nxt = spans[n + 1]
            after = nxt["members"][0]
            change = "repealed" if nxt["absent"] else "inserted" if span["absent"] else "changed"
            notes = []
            if nxt["absent"]:
                # A provision repealed outright leaves nothing in the next
                # version to hang its note on, so the note is loose there.
                notes = [note for note in ordered[after].get("loose_notes", {}).get(members[span_members[-1]], [])
                         if "repeal" in note.lower()]
            else:
                was = set()
                if not span["absent"]:
                    before = span_members[-1]
                    was = set(ordered[before]["effective"][members[before]].get("history") or [])
                notes = [note for note in ordered[after]["effective"][members[after]].get("history") or []
                         if note not in was]
            wording["ended_by"] = {**_stamp(ordered[after]), "change": change, "notes": notes}
        wordings.append(wording)
    return {"wordings": wordings}


def loose_notes(unattached: list[dict]) -> dict[tuple, list[str]]:
    """{provision key -> the margin notes about it that attach to no
    node}. That is where "S. 99 repealed by No. 48/2018 s. 20." ends up
    once section 99 is gone -- the only record in that version of what
    removed it."""
    out: dict = {}
    for note in unattached or []:
        if note.get("section") and note.get("raw"):
            key = diffing.provision_identity("section", note.get("schedule"), note["section"])
            out.setdefault(key, []).append(note["raw"])
    return out


def wording_at(chain: dict, version: int) -> "int | None":
    """Which of a chain's wordings a version carries."""
    for n, wording in enumerate(chain["wordings"]):
        if version in wording["versions"]:
            return n
    return None
