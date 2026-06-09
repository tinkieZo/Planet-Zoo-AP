"""Cross-check that data.json (Track A client) and the Planet Zoo APWorld (Track B) agree on the
item/location id<->name mappings.

A mismatch here is the bug where the server (authoritative, using the APWorld's ids) hands the client
an item id that data.json resolves to a DIFFERENT item -> the wrong effect is applied (e.g. the server
sends "Permit: Bengal Tiger" but the client grants Saltwater Crocodile). The APWorld assigns ids
positionally: item id = 1000 + index of data/items.json, location id = 2000 + index of data/location.json.

Skips if the APWorld tree isn't checked out next to this repo (set PZ_APWORLD_DATA to its data/ dir).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _apworld_data_dir():
    env = os.environ.get("PZ_APWORLD_DATA")
    repo = Path(__file__).resolve().parent.parent  # Planet-Zoo-AP
    candidates = ([Path(env)] if env else []) + [
        repo.parent / "ArchipelagoPZ" / "worlds" / "planetzoo" / "data",
        repo / "vendor" / "Archipelago" / "worlds" / "planetzoo" / "data",
    ]
    for c in candidates:
        if (c / "items.json").exists() and (c / "location.json").exists():
            return c
    return None


_DATA = _apworld_data_dir()
pytestmark = pytest.mark.skipif(_DATA is None, reason="Planet Zoo APWorld not found (set PZ_APWORLD_DATA)")


def _names(path: Path):
    return [e["name"] for e in json.loads(path.read_text(encoding="utf-8"))]


def test_item_ids_match_apworld(gd):
    ap = _names(_DATA / "items.json")
    by_id = {it.id: it.name for it in gd.items}
    assert len(gd.items) == len(ap), f"item-table size {len(gd.items)} != APWorld {len(ap)}"
    for i, name in enumerate(ap):
        assert by_id.get(1000 + i) == name, \
            f"item id {1000 + i}: client {by_id.get(1000 + i)!r} != APWorld {name!r}"


def test_location_ids_match_apworld(gd):
    ap = _names(_DATA / "location.json")
    by_id = {loc.id: loc.name for loc in gd.locations}
    assert len(gd.locations) == len(ap), f"location count {len(gd.locations)} != APWorld {len(ap)}"
    for i, name in enumerate(ap):
        assert by_id.get(2000 + i) == name, \
            f"location id {2000 + i}: client {by_id.get(2000 + i)!r} != APWorld {name!r}"
