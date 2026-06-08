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
from typing import Any, Dict, Iterable, List, Optional

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

    @property
    def gate_tokens(self) -> tuple:
        """The gate expression split into required tokens (``"a + b"`` -> ``("a","b")``).
        Tokens are unlock identifiers: ``start`` (always), ``permit_<species>`` (a species
        permit), a tool key (e.g. ``water_tools``), or a facility key (e.g. ``research_centre``)."""
        return tuple(t.strip() for t in self.gate.split("+") if t.strip())


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

    # -- species purchase-gating (the PermitGate substitutes a purchase-block for any
    #    species gate token it can enforce: a species permit, or a tool we can't gate
    #    natively like water_tools). Facility tokens (research_centre/workshop) are NOT
    #    enforced here - they have their own gates - so this stays a no-op for them. ----

    def tool_keys(self) -> set:
        """All tool keys that have a ``tool_unlock`` item (e.g. ``{"water_tools"}``)."""
        return {i.effect_args.get("tool_key") for i in self.items
                if i.effect_type == "tool_unlock" and i.effect_args.get("tool_key")}

    def species_purchase_tokens(self, s: "Species") -> tuple:
        """The gate tokens of ``s`` that the purchase-block enforces: species permits
        (``permit_*``) and tool tokens (a ``tool_unlock`` key). Excludes facility/``start``
        tokens. Empty -> the species is never purchase-blocked."""
        tk = self.tool_keys()
        return tuple(t for t in s.gate_tokens if t.startswith("permit_") or t in tk)

    def satisfied_purchase_tokens(self, received_item_ids: Iterable[int]) -> set:
        """Given received item IDs, the set of purchase-relevant gate tokens now satisfied:
        ``permit_<species>`` from each received ``species_unlock``, and each received
        ``tool_unlock``'s tool key."""
        tokens = set()
        for iid in received_item_ids:
            it = self.item_by_id.get(iid)
            if it is None:
                continue
            if it.effect_type == "species_unlock":
                k = it.effect_args.get("species_key")
                if k:
                    tokens.add("permit_" + k)
            elif it.effect_type == "tool_unlock":
                k = it.effect_args.get("tool_key")
                if k:
                    tokens.add(k)
        return tokens

    def purchase_universe(self) -> set:
        """Species keys the purchase-block could ever block (those with a purchase token)."""
        return {s.key for s in self.species if self.species_purchase_tokens(s)}

    def purchase_blocked_species(self, received_item_ids: Iterable[int]) -> set:
        """Species keys whose purchase the gate should block now: any species with a
        purchase token that is not yet satisfied by the received items. AND-semantics -
        e.g. saltwater_croc (``water_tools + permit_saltwater_croc``) stays blocked until
        BOTH arrive; nile_hippo (``water_tools``) until water_tools arrives."""
        sat = self.satisfied_purchase_tokens(received_item_ids)
        blocked = set()
        for s in self.species:
            ptoks = self.species_purchase_tokens(s)
            if ptoks and not all(t in sat for t in ptoks):
                blocked.add(s.key)
        return blocked


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
