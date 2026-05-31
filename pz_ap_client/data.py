"""Loader + typed view over the shared ``data.json`` contract.

This is the single seam between Track A (this client) and Track B (the APWorld).
Both sides code against the same file; IDs are stable ints and never reused.

The loader is deliberately strict: it fails loudly at startup if the contract is
malformed (duplicate IDs, out-of-range IDs, item/location count mismatch, unknown
effect/trigger types). Better to crash on launch than to silently mis-map a check
or an item during a live multiworld session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Effect types the client knows how to apply (see ARCHIPELAGO_PLAN.md).
EFFECT_TYPES = {
    "tool_unlock",
    "facility_unlock",
    "species_unlock",
    "program_unlock",
    "cash",
    "cc",
    "staff_training",
    "marketing",
    "enrichment_pack",
}

# Trigger types the client knows how to detect.
TRIGGER_TYPES = {
    "research_complete",
    "first_breed",
    "milestone",
}

CLASSIFICATIONS = {"progression", "useful", "filler"}


@dataclass(frozen=True)
class Item:
    id: int
    name: str
    classification: str
    effect_type: str
    effect_args: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Location:
    id: int
    name: str
    trigger_type: str
    trigger_args: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Species:
    key: str
    name: str
    gate: str
    flagship: bool = False


class DataError(Exception):
    """Raised when ``data.json`` violates the contract."""


@dataclass
class GameData:
    """Parsed, validated view over ``data.json`` with fast lookup tables."""

    meta: Dict[str, Any]
    species: List[Species]
    items: List[Item]
    locations: List[Location]
    slot_data: Dict[str, Any]

    # lookup tables (built in __post_init__)
    item_by_id: Dict[int, Item] = field(default_factory=dict, repr=False)
    location_by_id: Dict[int, Location] = field(default_factory=dict, repr=False)
    location_by_name: Dict[str, Location] = field(default_factory=dict, repr=False)
    item_name_by_id: Dict[int, str] = field(default_factory=dict, repr=False)
    species_by_key: Dict[str, Species] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.item_by_id = {i.id: i for i in self.items}
        self.location_by_id = {l.id: l for l in self.locations}
        self.location_by_name = {l.name: l for l in self.locations}
        self.item_name_by_id = {i.id: i.name for i in self.items}
        self.species_by_key = {s.key: s for s in self.species}

    # -- convenience accessors -------------------------------------------------

    @property
    def name_to_location_id(self) -> Dict[str, int]:
        return {l.name: l.id for l in self.locations}

    def locations_by_trigger(self, trigger_type: str) -> List[Location]:
        return [l for l in self.locations if l.trigger_type == trigger_type]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise DataError(msg)


def load(path: str | Path | None = None) -> GameData:
    """Load and validate ``data.json``.

    Defaults to the ``data.json`` sitting next to the project root (one level up
    from this package).
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / "data.json"
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    meta = raw.get("meta", {})
    items = [
        Item(
            id=i["id"],
            name=i["name"],
            classification=i["classification"],
            effect_type=i["effect_type"],
            effect_args=i.get("effect_args", {}),
        )
        for i in raw["items"]
    ]
    locations = [
        Location(
            id=l["id"],
            name=l["name"],
            trigger_type=l["trigger_type"],
            trigger_args=l.get("trigger_args", {}),
        )
        for l in raw["locations"]
    ]
    species = [
        Species(
            key=s["key"],
            name=s["name"],
            gate=s["gate"],
            flagship=s.get("flagship", False),
        )
        for s in raw.get("species", [])
    ]

    _validate(meta, items, locations)

    return GameData(
        meta=meta,
        species=species,
        items=items,
        locations=locations,
        slot_data=raw.get("slot_data", {}),
    )


def _validate(meta: Dict[str, Any], items: List[Item], locations: List[Location]) -> None:
    # Unique IDs.
    item_ids = [i.id for i in items]
    loc_ids = [l.id for l in locations]
    _require(len(item_ids) == len(set(item_ids)), "duplicate item id in data.json")
    _require(len(loc_ids) == len(set(loc_ids)), "duplicate location id in data.json")

    # Archipelago requires item count == location count.
    _require(
        len(items) == len(locations),
        f"item count ({len(items)}) must equal location count ({len(locations)})",
    )

    # ID ranges (if declared in meta).
    ranges = meta.get("id_ranges", {})
    for label, ids, key in (("item", item_ids, "items"), ("location", loc_ids, "locations")):
        rng = ranges.get(key)
        if not rng:
            continue
        lo, hi = (int(x) for x in str(rng).split("-"))
        for _id in ids:
            _require(lo <= _id <= hi, f"{label} id {_id} outside declared range {rng}")

    # Known enum values.
    for i in items:
        _require(
            i.classification in CLASSIFICATIONS,
            f"item {i.id} has unknown classification {i.classification!r}",
        )
        _require(
            i.effect_type in EFFECT_TYPES,
            f"item {i.id} ({i.name}) has unknown effect_type {i.effect_type!r}",
        )
    for l in locations:
        _require(
            l.trigger_type in TRIGGER_TYPES,
            f"location {l.id} ({l.name}) has unknown trigger_type {l.trigger_type!r}",
        )


if __name__ == "__main__":
    # Smoke test: python -m pz_ap_client.data
    gd = load()
    print(f"Loaded OK: {len(gd.items)} items, {len(gd.locations)} locations")
    print(f"  goal: {gd.slot_data.get('goal')}")
    by_trigger = {t: len(gd.locations_by_trigger(t)) for t in TRIGGER_TYPES}
    print(f"  locations by trigger: {by_trigger}")
