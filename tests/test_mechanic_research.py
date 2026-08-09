"""Game-free tests for mechanic-research (the 57 non-welfare research_complete locations).

Since the 2026-08-09 apworld the data.json research_key IS the engine research-item name (recovered
from the location label by build_data_json; the apworld stringids became opaque codes). Detection
resolves that name to a live cat-3 record via the record's +0x08 name-intern id and fires when the
record's status == 4; the legacy MECHANIC_RESEARCH_NAME stringid->name table remains as a fallback
for older data.json files. These tests guard the keys' engine-name validity against the DLC-complete
catalog and exercise the is_research_complete dispatch (both key styles) with a stubbed map/snapshot.
(The live name-bridge resolution is validated separately by tools/mechanic_probe.py.)
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


def test_mechanic_keys_are_known_engine_names():
    """Since the 2026-08-09 apworld, data.json mechanic research_keys ARE engine research-item names
    (recovered from the location label by build_data_json) - each must exist in the DLC-complete
    research catalog's mechanic token set, else the location's check could never fire (a recovery
    fallback wrote the raw label). Catches label-rename drift like Orient -> East Asian."""
    catalog = json.loads((ROOT / "tools" / "research_catalog.json").read_text(encoding="utf-8"))
    tokens = {_norm_token(o["item"]) for opts in catalog["mechanic"].values() for o in opts}
    unknown = {k for k in _data_mechanic_keys() if _norm_token(k) not in tokens}
    assert not unknown, f"mechanic research_keys with no engine token: {sorted(unknown)}"


def test_mechanic_keys_are_unique_per_location():
    """Two mechanic locations must not share a research_key, else one research completion would
    fire both checks."""
    data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    keys = [l["trigger_args"]["research_key"] for l in data["locations"]
            if l.get("trigger_type") == "research_complete"
            and not l["trigger_args"].get("research_key", "").startswith("welfare")]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicate mechanic research_keys: {sorted(dupes)}"


def test_is_research_complete_engine_name_key_fires():
    """The new-style key (engine item name, not in the legacy stringid table) resolves via the live
    mechanic map fallback and fires on status 4; stays False when incomplete or unresolvable."""
    name = _norm_token("IndiaThemeSetsBlueprintsL2")
    rr = _reader_with({name: 0x3001}, {0x3001: (0, 1, STATUS_COMPLETE, MECHANIC_CATEGORY)})
    assert rr.is_research_complete("IndiaThemeSetsBlueprintsL2") is True
    rr = _reader_with({name: 0x3001}, {0x3001: (0, 1, 2, MECHANIC_CATEGORY)})
    assert rr.is_research_complete("IndiaThemeSetsBlueprintsL2") is False
    rr = _reader_with({}, {})
    assert rr.is_research_complete("IndiaThemeSetsBlueprintsL2") is False


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
