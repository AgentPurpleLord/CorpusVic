"""
Loads the comprehensive Victorian Act registry extracted from the OCPC's
own "List of Acts in chronological order" (em/List-of-Acts-in-
chronological-order.pdf) -- see extract_act_registry.py for how the JSON
file this reads was produced.

This is a different, complementary thing to ai_pipeline/known_acts.yaml:
known_acts.yaml is a small, hand-curated slug registry for Acts this
pipeline has actually parsed (has real node data to link *into*).
act_registry.json is instead comprehensive but shallow -- it can confirm
"yes, this is a real Act, here's its Act number and current in-force
status" for essentially any Victorian Act ever passed, but has no parsed
content behind it. Consulted as the fallback once known_acts.yaml itself
doesn't have an answer -- see link_targets.resolve_act_citation and
bill_linking.resolve_em_links.
"""
import json
from pathlib import Path

ACT_REGISTRY_PATH = Path(__file__).parent / "act_registry.json"


def load_act_registry() -> dict[str, dict]:
    """short title (e.g. "Sentencing Act 1991") -> {"year", "act_no",
    "repealed_by", "repealed_provision", "in_force"}. Empty dict if the
    registry hasn't been generated yet (see extract_act_registry.py)."""
    if not ACT_REGISTRY_PATH.exists():
        return {}
    return json.loads(ACT_REGISTRY_PATH.read_text(encoding="utf-8"))
