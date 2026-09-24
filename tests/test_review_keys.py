"""The review page's keys. Only a browser can press them, so what is
pinned here is the table they are declared in: that each asked-for move
has a key, no key does two things, and typing is never taken for one."""
import re

from corpus import PROJECT_ROOT

PAGE = (PROJECT_ROOT / "static" / "review.html").read_text(encoding="utf-8")
TABLE = re.search(r"const KEYS = \[(.*?)\n\];", PAGE, re.S).group(1)


def _bound() -> list[str]:
    return [k for keys in re.findall(r"\[([^\[\]]*)\]\],?$", TABLE, re.M)
            for k in re.findall(r'"([^"]+)"', keys)]


def test_the_asked_for_moves_have_keys():
    bound = _bound()
    for key in ("j", "k", "n", "p", "ArrowRight", "ArrowLeft"):
        assert key in bound, key
    assert "stepPiece(" in TABLE and "stepUnit(" in TABLE and "stepPage(" in TABLE


def test_no_key_does_two_things():
    bound = _bound()
    assert len(bound) == len(set(bound))


def test_every_key_calls_something_the_page_defines():
    called = set(re.findall(r"(\w+)\(", TABLE)) - {"includes", "getElementById", "focus", "find"}
    for name in called:
        assert re.search(rf"function {name}\(", PAGE), name


def test_typing_and_browser_shortcuts_are_left_alone():
    handler = re.search(r'document\.addEventListener\("keydown", \(e\) => \{\n  if \(e\.ctrlKey(.*?)\n\}\);', PAGE, re.S)
    assert handler, "the keys handler returns first on a modifier"
    body = handler.group(1)
    assert "input, select, textarea" in body
    assert '.overlay:not([hidden])' in body, "a key pressed in a dialog is the dialog's"


def test_the_keys_are_listed_on_the_page():
    assert 'id="keys-btn"' in PAGE and 'id="keys-overlay"' in PAGE
