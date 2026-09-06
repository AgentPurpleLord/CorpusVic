"""
Loads the full list of Victorian Acts, taken from the OCPC's own "List
of Acts in chronological order" (em/List-of-Acts-in-chronological-
order.pdf) -- see extract_act_registry.py for how the JSON file this
reads was produced.

This is different from, and complements, ai_pipeline/known_acts.yaml:
known_acts.yaml is a small, hand-curated list of Acts this pipeline has
actually parsed (so there's real content to link to). act_registry.json
covers almost every Victorian Act ever passed, but only shallow details
-- it can confirm "yes, this is a real Act, here's its Act number and
whether it's still in force", not link to any actual parsed content.
It's used as a fallback whenever known_acts.yaml doesn't have an answer
-- see link_targets.resolve_act_citation and bill_linking.resolve_em_links.
"""
import json
from functools import lru_cache
from pathlib import Path

ACT_REGISTRY_PATH = Path(__file__).parent / "act_registry.json"


@lru_cache(maxsize=1)
def load_act_registry() -> dict[str, dict]:
    """short title (e.g. "Sentencing Act 1991") -> {"year", "act_no",
    "repealed_by", "repealed_provision", "in_force"}. Empty dict if the
    registry hasn't been generated yet (see extract_act_registry.py).

    Cached for as long as the process runs. This file only changes when
    someone regenerates it by hand (extract_act_registry.py) -- the
    dashboard never touches it while running. html_view.py's Act-name
    linking now calls this once for every page it renders, so without
    caching we'd re-read and re-parse an 8000-entry, ~1.4MB JSON file
    every time someone opens a page, for no reason."""
    if not ACT_REGISTRY_PATH.exists():
        return {}
    return json.loads(ACT_REGISTRY_PATH.read_text(encoding="utf-8"))
