"""Where modules under corpus/ look for the project's own files.

The restructure moved every module into corpus/, one or two directories
deeper than it had been. Anything that reached a project file by counting
`.parent`s from its own location kept counting the old number, so it now
points at a path inside the package that has never existed -- and because
each one fails quietly, in its own way, they turned up one at a time in
production rather than all at once here.

corpus/__init__.py's PROJECT_ROOT exists so that nothing has to count.
These tests are the check that nothing does.
"""
import re
from pathlib import Path

import pytest

from corpus import PROJECT_ROOT

CORPUS = PROJECT_ROOT / "corpus"

# The project's own directories, as they sit beside corpus/. A path built
# from a module's location that lands on one of these names is reaching
# out of the package, which is the move that breaks.
PROJECT_DIRS = ("data", "deploy", "static", "acts", "em", "docs", "tests", "_backups")


def test_the_project_root_is_the_directory_the_project_is_in():
    assert (PROJECT_ROOT / "corpus" / "__init__.py").exists()
    assert (PROJECT_ROOT / "deploy").is_dir()


def test_nothing_counts_parents_to_reach_a_project_directory():
    """The bug, stated once. `Path(__file__).parent / "deploy"` was right
    when the module sat at the repo root and is wrong now; PROJECT_ROOT
    is right wherever the module is moved to next."""
    offenders = []
    for source in sorted(CORPUS.rglob("*.py")):
        if source.name == "__init__.py" and source.parent == CORPUS:
            continue   # where PROJECT_ROOT is defined, and the one place that may
        for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(
                r"Path\(__file__\)[\w.()]*\s*/\s*['\"](" + "|".join(PROJECT_DIRS) + r")['\"]", line)
            if match:
                offenders.append(f"{source.relative_to(PROJECT_ROOT)}:{line_no}: {line.strip()}")
    assert not offenders, (
        "these reach a project directory by counting parents, which the next move breaks -- "
        "use corpus.PROJECT_ROOT:\n  " + "\n  ".join(offenders))


# Each of these was pointing somewhere that does not exist. Named one by
# one rather than only caught by the sweep above, because what matters is
# not the shape of the line but that the file is actually found.

def test_the_sites_passphrase_is_looked_for_where_it_is_kept():
    """It was looked for at corpus/deploy/site.env. Nothing raises when
    it isn't there -- an ungated site is a supported configuration -- so
    the gate simply came off the published site."""
    from corpus.publishing import site_env

    assert site_env.SITE_ENV_FILE == PROJECT_ROOT / "deploy" / "site.env"


def test_the_act_registrys_source_pdf_is_looked_for_where_it_is():
    from corpus.domain import extract_act_registry

    assert extract_act_registry.PDF_PATH.parent == PROJECT_ROOT / "em"


def test_the_registry_itself_stays_beside_the_module_that_loads_it():
    """The other direction, so the sweep above is not read as "everything
    belongs at the root": act_registry.json is package data."""
    from corpus.domain import act_registry, extract_act_registry

    assert extract_act_registry.OUT_PATH == act_registry.ACT_REGISTRY_PATH
    assert act_registry.ACT_REGISTRY_PATH.exists()


# ---------------------------------------------------------------------------
# The same mistake in the other shape: a path resolved against the
# working directory rather than against the checkout being worked on.
# ---------------------------------------------------------------------------

def test_the_checkpoint_uses_the_checkout_it_was_given(tmp_path, monkeypatch):
    """sync.checkpoint_database took no repo and let db.db_path() fall
    back to the current directory. It only ever worked because the
    service sets WorkingDirectory -- run from anywhere else and it opens
    an empty database next to wherever the process is standing, which the
    export that follows then writes over the real review files with."""
    from corpus.review import sync

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(elsewhere)

    sync.checkpoint_database(checkout)

    assert (checkout / "data").exists(), "the database was not opened in the checkout"
    assert not (elsewhere / "data").exists(), "a database was created in the working directory"
