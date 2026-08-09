"""Cross-check that data.json (Track A client) and the Planet Zoo APWorld (Track B) agree on the
item/location id<->name mappings.

A mismatch here is the bug where the server (authoritative, using the APWorld's ids) hands the client
an item id that data.json resolves to a DIFFERENT item -> the wrong effect is applied (e.g. the server
sends "Permit: Bengal Tiger" but the client grants Saltwater Crocodile). The APWorld assigns ids
positionally:
  item id     = 1000 + index of (data/items.json + data/old_items.json)   [Items.item_name_to_id]
  location id = 2000 + index of (data/specieslocations.json + data/mech_n_milestones.json), keyed by
                each entry's `label` (== the location NAME the AP server uses, via Locations
                .location_name_to_id, which is `{label: 2000+index}`)  [Locations]

data.json is regenerated from exactly these by tools/build_data_json.py. Skips if the APWorld tree
isn't checked out next to this repo (set PZ_APWORLD_DATA to its data/ dir).
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
        if (c / "items.json").exists() and (c / "locations.json").exists():
            return c
    return None


_DATA = _apworld_data_dir()
pytestmark = pytest.mark.skipif(_DATA is None, reason="Planet Zoo APWorld not found (set PZ_APWORLD_DATA)")


def _entries(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_item_ids_match_apworld(gd):
    # Since apworld bd4cc1cd, items.json carries EXPLICIT per-entry ids (the positional 1000+index
    # scheme over items.json+old_items.json is gone). Names are stripped (Track B has had
    # trailing-space name bugs before).
    ap = {e["id"]: e["name"].strip() for e in _entries(_DATA / "items.json")}
    by_id = {it.id: it.name for it in gd.items}
    assert by_id == ap, (
        f"item id/name tables diverge: only-client={sorted(set(by_id) - set(ap))[:5]} "
        f"only-apworld={sorted(set(ap) - set(by_id))[:5]} "
        f"renamed={[i for i in (set(ap) & set(by_id)) if ap[i] != by_id[i]][:5]}")


def test_location_ids_match_apworld(gd):
    # The AP server keys locations by LABEL (Locations.location_name_to_id = {label: id}), so
    # data.json's location name MUST be the label - not the stringid. Ids are explicit per entry.
    ap = {e["id"]: e["label"] for e in _entries(_DATA / "locations.json")}
    by_id = {loc.id: loc.name for loc in gd.locations}
    assert by_id == ap, (
        f"location id/label tables diverge: only-client={sorted(set(by_id) - set(ap))[:5]} "
        f"only-apworld={sorted(set(ap) - set(by_id))[:5]} "
        f"renamed={[i for i in (set(ap) & set(by_id)) if ap[i] != by_id[i]][:5]}")


def test_every_permit_has_a_species(gd):
    """Every species_unlock item's species_key must be a real species in data.json (so the
    permit/market gates can resolve it)."""
    keys = {s.key for s in gd.species}
    for it in gd.items:
        if it.effect_type == "species_unlock":
            sk = it.effect_args.get("species_key")
            assert sk in keys, f"permit {it.name!r} -> unknown species_key {sk!r}"
