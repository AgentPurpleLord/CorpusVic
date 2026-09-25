"""dashboard._ghosts: a provision this version no longer has, found from
the work's timeline and placed after the provision it used to follow."""
import corpus.web.dashboard as dashboard

S98 = ("provision", None, "98")
S99 = ("provision", None, "99")
S100 = ("provision", None, "100")


def _timeline(checked=True):
    present = {"absent": False, "versions": [1], "from": {"version": 1}, "to": {"version": 1},
               "key": S99, "keys": {1: S99}, "version": 1, "checked": checked,
               "provision": {"heading": "Old offence"},
               "ended_by": {"version": 2, "change": "repealed", "notes": []}}
    absent = {"absent": True, "versions": [2], "from": {"version": 2}, "to": {"version": 2}}
    return {"slugs": ["act-v1", "act-v2"], "chains": [{"wordings": [present, absent]}],
            "by_key": {(1, S99): 0}, "order": {1: [S98, S99, S100], 2: [S98, S100]},
            # Each version's addresses, gathered as the timeline is built.
            "page_keys": {1: _pages("act-v1")["by_key"], 2: _pages("act-v2")["by_key"]}}


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


def test_an_unchecked_repeal_still_gets_its_page(monkeypatch):
    """Parsed means published, here as everywhere else on the site; a
    reviewer's check is reassurance, not a gate. This used to return no
    ghost until every wording was checked wherever the versions had been
    read by different parsers -- and with four of the five Criminal
    Procedure Act reprints predating parser_version, that hid everything.
    What carries the caution now is the page: the wording is marked as not
    yet checked (see html_view.render_history)."""
    monkeypatch.setattr(dashboard, "_timeline", lambda work: _timeline(checked=False))
    monkeypatch.setattr(dashboard, "_page_index", _pages)

    [ghost] = dashboard._ghosts("act-v2")

    assert ghost["history"]["wordings"][0]["checked"] is False, "it goes out marked, not hidden"


def test_an_unchecked_history_is_shown_on_the_provision_itself(monkeypatch):
    """The same rule on a provision that still exists. With four of the
    five Criminal Procedure Act reprints predating parser_version, the old
    gate hid all 47 of its histories."""
    monkeypatch.setattr(dashboard, "_timeline", lambda work: _timeline(checked=False))
    monkeypatch.setattr(dashboard, "_page_index", _pages)

    history, _urls = dashboard._provision_timeline("act-v1", "99", None)

    assert history is not None, "hidden until checked -- the gate is back"
    assert history["wordings"][0]["checked"] is False
