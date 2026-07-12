"""The registry-based species attribution: welfare + birth/acquire detection for species that were
NEVER captured into SPECIES_WELFARE_ITEM, using only the live symbol registry + the data.json
engine_token bridge. This is what removes the per-species capture requirement.

Game-free: a fake ResearchReader memory (the research items map) + a fake RegistryResolver
(symbol id -> engine token). No game, no pymem.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.research import ResearchReader, ANIMAL_CATEGORY, ADVANCED_LEVEL  # noqa: E402

# A species NOT in SPECIES_WELFARE_ITEM (the captured 11). aardvark proves the registry path
# covers the other 67 with zero capture.
H_AARDVARK = 0x3ABC          # its live symbol id == research-map handle this session
AARDVARK_BASE = 0x55A0       # arbitrary level-0 welfare research-item id (we do NOT hardcode it)


class FakeMem:
    """Backs ResearchReader._snapshot via the real read path? No - we stub _snapshot instead, since
    the map-walk is covered elsewhere. Here we focus on the registry attribution logic."""
    module_base = 0x140000000
    attached = True

    def attach(self):
        return True


class FakeRegistry:
    """symbol id -> interned engine name (what RegistryResolver.id_to_name returns live)."""

    def __init__(self, id2name):
        self._id2name = id2name

    def id_to_name(self, sid):
        return self._id2name.get(sid)


def _reader():
    # 6 consecutive welfare levels for aardvark at the (arbitrary) base, all complete except level 6;
    # handle H_AARDVARK, category 7. No entry for 'aardvark' in SPECIES_WELFARE_ITEM.
    by_item = {}
    by_handle = {H_AARDVARK: []}
    for lvl in range(6):
        status = 4 if lvl < 5 else 2          # levels 1-5 complete, level 6 researching
        by_item[AARDVARK_BASE + lvl] = (H_AARDVARK, lvl, status, ANIMAL_CATEGORY)
        by_handle[H_AARDVARK].append((lvl, status, ANIMAL_CATEGORY))
    rr = ResearchReader(FakeMem(),
                        registry=FakeRegistry({H_AARDVARK: "Aardvark"}),
                        token_to_key={"aardvark": "aardvark"})  # engine_token -> species_key
    rr._snapshot = lambda: (by_item, by_handle)   # stub the map read
    assert "aardvark" not in rr.items, "precondition: aardvark is NOT a captured welfare species"
    return rr


def test_handle_resolves_to_uncaptured_species():
    rr = _reader()
    assert rr.species_key_for_handle(H_AARDVARK) == "aardvark"
    assert rr.handle_key_map().get(H_AARDVARK) == "aardvark"


def test_welfare_item_id_auto_derived_from_registry():
    rr = _reader()
    # the level-0 item id is DERIVED (min cat-7 item of the resolved handle), not captured
    assert rr._welfare_item("aardvark") == AARDVARK_BASE
    assert rr.current_handle("aardvark") == H_AARDVARK


def test_per_level_welfare_for_uncaptured_species():
    rr = _reader()
    assert rr.is_research_complete("welfare_aardvark", level=1) is True   # level 1 complete
    assert rr.is_research_complete("welfare_aardvark", level=5) is True   # level 5 complete
    assert rr.is_research_complete("welfare_aardvark", level=6) is False  # level 6 researching
    # "all standard levels complete" is False while level 6 is still researching
    assert rr.is_welfare_complete("aardvark") is False


def test_per_level_welfare_exhibit_id_layout():
    """Regression (live 2026-07-12, 'Research Welfare 1 - Malabar Rose' never fired while 2/3 fired
    shifted-early): for exhibit species the LOWEST cat-7 item id is NOT the level-1 record, so the
    old id-arithmetic (level N @ min_id + N-1) read the wrong records. Levels must resolve by RANK
    over the level FIELDS. Layout here: the advanced record holds the smallest item id; the three
    standard levels sit at non-contiguous higher ids; levels 1-2 complete, 3 researching."""
    H = 0x3017
    by_item = {
        0x7000: (H, ADVANCED_LEVEL, 2, ANIMAL_CATEGORY),   # advanced/infinite record @ MIN item id
        0x7002: (H, 0, 4, ANIMAL_CATEGORY),                # level 1 (field 0) complete
        0x7003: (H, 1, 4, ANIMAL_CATEGORY),                # level 2 complete
        0x7005: (H, 2, 2, ANIMAL_CATEGORY),                # level 3 researching (id gap before it)
    }
    by_handle = {H: [(lvl, st, cat) for (_h, lvl, st, cat) in by_item.values()]}
    rr = ResearchReader(FakeMem(),
                        registry=FakeRegistry({H: "MalabarRose"}),
                        token_to_key={"malabarrose": "mrose"})
    rr._snapshot = lambda: (by_item, by_handle)
    assert rr.is_research_complete("welfare_mrose", level=1) is True, \
        "level 1 fires despite the advanced record owning the min item id"
    assert rr.is_research_complete("welfare_mrose", level=2) is True
    assert rr.is_research_complete("welfare_mrose", level=3) is False, "level 3 still researching"
    assert rr.is_research_complete("welfare_mrose", level=4) is False, "no 4th standard level"


def test_degrades_without_registry():
    # No registry + not in SPECIES_WELFARE_ITEM -> no resolution, no crash, no false positive.
    by_item = {AARDVARK_BASE: (H_AARDVARK, 0, 4, ANIMAL_CATEGORY)}
    by_handle = {H_AARDVARK: [(0, 4, ANIMAL_CATEGORY)]}
    rr = ResearchReader(FakeMem())          # no registry/token_to_key
    rr._snapshot = lambda: (by_item, by_handle)
    assert rr.species_key_for_handle(H_AARDVARK) is None
    assert rr.handle_key_map() == {}
    assert rr.is_research_complete("welfare_aardvark", level=1) is False
