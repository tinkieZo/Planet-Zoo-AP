"""Game-free tests for mechanic-research (the 57 non-welfare research_complete locations).

Detection maps each apworld stringid (drink_shop1, barrier1, sf_research_centre_l, ...) to an engine
research-item NAME (MECHANIC_RESEARCH_NAME), resolves that name to a live cat-3 record via the record's
+0x08 name-intern id, and fires when the record's status == 4. These tests guard the map's coverage
against data.json drift and exercise the is_research_complete dispatch with a stubbed map/snapshot.
(The live name-bridge + 57/57 resolution is validated separately by tools/mechanic_probe.py.)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pz_ap_client.memory.research import (  # noqa: E402
    ResearchReader, MECHANIC_RESEARCH_NAME, STATUS_COMPLETE, MECHANIC_CATEGORY, _norm_token,
)


def _data_mechanic_keys() -> set:
    data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    return {l["trigger_args"]["research_key"] for l in data["locations"]
            if l.get("trigger_type") == "research_complete"
            and not l["trigger_args"].get("research_key", "").startswith("welfare")}


def test_mechanic_map_covers_every_location_key():
    """MECHANIC_RESEARCH_NAME must map EXACTLY the data.json non-welfare research keys - no missing
    (a check that could never fire) and no stale extras. Catches apworld/data.json drift."""
    data_keys = _data_mechanic_keys()
    map_keys = set(MECHANIC_RESEARCH_NAME)
    assert not (data_keys - map_keys), f"unmapped mechanic locations: {sorted(data_keys - map_keys)}"
    assert not (map_keys - data_keys), f"stale mechanic map entries: {sorted(map_keys - data_keys)}"


def test_mechanic_engine_names_are_unique():
    """Each apworld key must map to a DISTINCT engine item (the mapping is a bijection onto the
    branch's levels), else two locations would resolve to one record."""
    names = list(MECHANIC_RESEARCH_NAME.values())
    assert len(names) == len(set(names)), "duplicate engine names in MECHANIC_RESEARCH_NAME"


def _reader_with(mech_map, by_item):
    rr = ResearchReader(scanner=None)
    rr._mechanic_item_map = lambda: mech_map           # {normalized name -> item id}
    rr._snapshot = lambda: (by_item, {})               # by_item[id] = (handle, level, status, cat)
    return rr


def test_is_research_complete_mechanic_fires_on_status_4():
    name = _norm_token(MECHANIC_RESEARCH_NAME["drink_shop1"])  # 'drinkshopsgulpeeslush'
    rr = _reader_with({name: 0x2727}, {0x2727: (0, 1, STATUS_COMPLETE, MECHANIC_CATEGORY)})
    assert rr.is_research_complete("drink_shop1") is True


def test_is_research_complete_mechanic_false_when_incomplete():
    name = _norm_token(MECHANIC_RESEARCH_NAME["drink_shop1"])
    rr = _reader_with({name: 0x2727}, {0x2727: (0, 1, 2, MECHANIC_CATEGORY)})  # status 2 = researching
    assert rr.is_research_complete("drink_shop1") is False


def test_is_research_complete_mechanic_false_when_name_absent():
    # branch not loaded in this scenario -> name not in the live map -> no false positive
    rr = _reader_with({}, {})
    assert rr.is_research_complete("drink_shop1") is False
