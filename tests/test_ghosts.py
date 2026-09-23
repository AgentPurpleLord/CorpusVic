"""dashboard._ghosts: a provision this version no longer has, found from
the work's timeline and placed after the provision it used to follow."""
import corpus.web.dashboard as dashboard

S98 = ("provision", None, "98")
S99 = ("provision", None, "99")
S100 = ("provision", None, "100")


def _timeline(mixed=False, checked=True):
    present = {"absent": False, "versions": [1], "from": {"version": 1}, "to": {"version": 1},
               "key": S99, "keys": {1: S99}, "version": 1, "checked": checked,
               "provision": {"heading": "Old offence"},
               "ended_by": {"version": 2, "change": "repealed", "notes": []}}
    absent = {"absent": True, "versions": [2], "from": {"version": 2}, "to": {"version": 2}}
    return {"slugs": ["act-v1", "act-v2"], "chains": [{"wordings": [present, absent]}],
            "by_key": {(1, S99): 0}, "order": {1: [S98, S99, S100], 2: [S98, S100]},
            "mixed_parsers": mixed}


def _pages(slug):
    if slug == "act-v1":
        return {"by_node_index": {1: "s98", 2: "s99", 3: "s100"}, "by_key": {S98: "s98", S99: "s99", S100: "s100"}}
    return {"by_node_index": {1: "s98", 2: "s100"}, "by_key": {S98: "s98", S100: "s100"}}


def test_a_repealed_section_keeps_its_address_and_its_place(monkeypatch):
    monkeypatch.setattr(dashboard, "_timeline", lambda work: _timeline())
    monkeypatch.setattr(dashboard, "_page_index", _pages)

    [ghost] = dashboard._ghosts("act-v2")

    assert (ghost["page"], ghost["after_page"], ghost["label"]) == ("s99", "s98", "Section 99")
    assert ghost["history"]["at"] == 1


def test_the_version_that_still_has_it_has_no_ghost(monkeypatch):
    monkeypatch.setattr(dashboard, "_timeline", lambda work: _timeline())
    monkeypatch.setattr(dashboard, "_page_index", _pages)

    assert dashboard._ghosts("act-v1") == []


def test_no_ghost_from_an_unchecked_comparison_across_parsers(monkeypatch):
    """A section two parsers disagree about may simply be one the older
    parser missed; a register of the law must not call it repealed."""
    monkeypatch.setattr(dashboard, "_timeline", lambda work: _timeline(mixed=True, checked=False))
    monkeypatch.setattr(dashboard, "_page_index", _pages)

    assert dashboard._ghosts("act-v2") == []
