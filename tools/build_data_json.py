"""build_data_json.py - regenerate the client's data.json from the Track B APWorld.

data.json is the single Track A<->B seam: it maps the APWorld's item/location IDs
(authoritative) to the client's effect/trigger semantics. The APWorld assigns IDs
positionally, so hand-maintaining data.json drifts the moment Track B changes. This
tool rebuilds it deterministically from the APWorld's own data:

  * item IDs/names      <- worlds/planetzoo/Items.item_name_to_id
  * location IDs/names  <- worlds/planetzoo/Locations.location_name_to_id
  * species keys        <- data/specieses.json (stringid == our species_key namespace)
  * decoupled-reward content tokens <- data/research_catalog.json, recovered by
    REPLAYING the APWorld's own convert_readable() (no guessing - exact inverse).

Run (APWorld beside this repo, or set PZ_APWORLD):
    python tools/build_data_json.py [--out data.json]

Effect mapping (effect_type the client applies):
  Permit: <label>           -> species_unlock {species_key}        (78)
  Research Centre / Workshop -> facility_unlock {facility_key}
  Water Habitat Tools        -> tool_unlock {tool_key: water_tools}
  Conservation Program       -> program_unlock {program_key: conservation}
  Cash/Conservation Credits  -> cash / cc {amount}
  Progressive * Level        -> progressive_research_reward {family}
  everything else            -> research_reward {content: <raw token>}  (decoupled rewards)

Trigger mapping (trigger_type the client detects):
  welfareN_<sp> -> research_complete {research_key: welfare_<sp>, level: N, species_key}
  fb_<sp>       -> first_breed {species_key}
  fa_<sp>       -> first_acquire {species_key}
  cr_<sp>       -> conservation_release {species_key}
  <mechanic>    -> research_complete {research_key, mechanic: true}
  zoo_rating N  -> milestone {metric: zoo_rating, threshold: N}
  guests_N      -> milestone {metric: guest_count, threshold: N}
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def find_apworld() -> Path:
    env = os.environ.get("PZ_APWORLD")
    cands = ([Path(env)] if env else []) + [
        REPO.parent / "ArchipelagoPZ",
        REPO.parent / "Archipelago",
    ]
    for c in cands:
        if (c / "worlds" / "planetzoo" / "World.py").is_file():
            return c
    sys.exit("APWorld not found - set PZ_APWORLD to the Archipelago checkout containing "
             "worlds/planetzoo (e.g. the sibling ArchipelagoPZ).")


def convert_readable(name: str) -> str:
    """EXACT copy of worlds/planetzoo/generate_items.py:convert_readable - so replaying it
    over the research catalog reproduces the APWorld's item names, giving us name->token."""
    name = re.sub(r"^EN_", "Enrichment: ", name)
    name = name.replace("_", " ")
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", name)
    name = name.replace(" L ", " Level ")
    return name.strip()


# Static effect args the APWorld doesn't carry (client-side economy tuning; preserved from the
# original slice data.json so amounts don't silently change).
CASH = {"Cash Injection (Small)": 10000, "Cash Injection (Medium)": 25000, "Cash Injection (Large)": 50000}
CC = {"Conservation Credits (Small)": 1000, "Conservation Credits (Medium)": 2000, "Conservation Credits (Large)": 5000}
PROGRESSIVE = {
    "Progressive Supplement Level": "supplement",
    "Progressive Education Level": "education",
    "Progressive Breeding Level": "breeding",
    "Progressive Exhibit Enrichment Level": "exhibit_enrichment",
}
FIXED = {
    "Research Centre": ("facility_unlock", {"facility_key": "research_centre"}),
    "Workshop": ("facility_unlock", {"facility_key": "workshop"}),
    "Water Habitat Tools": ("tool_unlock", {"tool_key": "water_tools"}),
    "Conservation Program": ("program_unlock", {"program_key": "conservation"}),
}


def classification(ap_class: str) -> str:
    return {"Progression": "progression", "Filler": "filler"}.get(ap_class, "useful")


def _norm(s: str) -> str:
    """Canonicalise a name for matching the engine's interned species token / catalog key."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


# The client attributes births/acquisitions and resolves welfare-research handles by matching the
# live species symbol (RegistryResolver) to the species' ENGINE TOKEN. That token == the research
# catalog's species key. For 68/78 species norm(label) IS the catalog key; these 5 diverge (the
# catalog dropped/abbreviated words, incl. one typo "brazillian"), so alias them explicitly.
# (5 more species - aleopard/blemur/cpeccary/rdeer/mrose - have no catalog welfare tree at all;
# they fall back to norm(label), which still lets birth/acquire attribution work via the registry.)
ENGINE_TOKEN_ALIAS = {
    "aelephant": "africanelephant",        # label "African Savannah Elephant"
    "acentipede": "amazongiantcentipede",  # label "Amazonian Giant Centipede"
    "liguana": "antilleaniguana",          # label "Lesser Antillean Iguana"
    "btarantula": "brazilliansalmonpinktarantula",  # catalog typo "brazillian"
    "lfrog": "lehmannspoisonfrog",         # label "Lehmann Poison Frog" (catalog "lehmanns")
}


def engine_token(stringid: str, label: str) -> str:
    return ENGINE_TOKEN_ALIAS.get(stringid) or _norm(label)


def build_token_index(catalog: dict) -> dict:
    """{readable_name -> raw content token} by replaying convert_readable over every reward
    token (species rewards + mechanic items) in the research catalog."""
    idx: dict = {}
    for entries in catalog.get("species", {}).values():
        for entry in entries:
            for reward in entry.get("rewards", []):
                idx.setdefault(convert_readable(reward), reward)
    for options in catalog.get("mechanic", {}).values():
        for opt in options:
            tok = opt["item"]
            idx.setdefault(convert_readable(tok), tok)
    return idx


def map_item(name: str, lab2sid: dict, token_index: dict) -> dict:
    if name.startswith("Permit: "):
        label = name[len("Permit: "):]
        sid = lab2sid.get(label)
        if sid is None:
            sys.exit(f"permit {name!r}: no species stringid for label {label!r} in specieses.json")
        return {"effect_type": "species_unlock", "effect_args": {"species_key": sid}}
    if name in FIXED:
        et, args = FIXED[name]
        return {"effect_type": et, "effect_args": args}
    if name in CASH:
        return {"effect_type": "cash", "effect_args": {"amount": CASH[name]}}
    if name in CC:
        return {"effect_type": "cc", "effect_args": {"amount": CC[name]}}
    if name.strip() in PROGRESSIVE:
        return {"effect_type": "progressive_research_reward", "effect_args": {"family": PROGRESSIVE[name.strip()]}}
    token = token_index.get(name)
    if token is None:
        # Decoupled reward whose token didn't round-trip through the catalog (e.g. an item added
        # outside the catalog-derived set). Fall back to the display name; flag it loudly.
        print(f"  WARN: no content token for decoupled item {name!r} - using name as token", file=sys.stderr)
        token = name
    return {"effect_type": "research_reward", "effect_args": {"content": token}}


def map_location(stringid: str, loc) -> dict:
    sp = loc.species_type
    if loc.type.value == "research welfare":
        m = re.match(r"welfare(\d+)_", stringid)
        level = int(m.group(1)) if m else None
        return {"trigger_type": "research_complete",
                "trigger_args": {"research_key": f"welfare_{sp}", "level": level, "species_key": sp}}
    if loc.type.value == "firsts":
        if stringid.startswith("fa_"):
            return {"trigger_type": "first_acquire", "trigger_args": {"species_key": sp}}
        return {"trigger_type": "first_breed", "trigger_args": {"species_key": sp}}
    if loc.type.value == "conservation":
        return {"trigger_type": "conservation_release", "trigger_args": {"species_key": sp}}
    if loc.type.value == "milestones":
        if stringid.startswith("zoo_rating"):
            return {"trigger_type": "milestone",
                    "trigger_args": {"metric": "zoo_rating", "threshold": int(stringid.replace("zoo_rating", ""))}}
        if stringid.startswith("guests_"):
            return {"trigger_type": "milestone",
                    "trigger_args": {"metric": "guest_count", "threshold": int(stringid.split("_")[1])}}
    # mechanic (and any fallthrough): a per-item mechanic-research completion. research_key kept as
    # the stringid; mechanic-research detection is capture-gated (client RESEARCH_ITEM map), degrades.
    return {"trigger_type": "research_complete",
            "trigger_args": {"research_key": stringid, "mechanic": True}}


def main() -> None:
    out_path = REPO / "data.json"
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])
    ap = find_apworld()
    sys.path.insert(0, str(ap))
    os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
    pzdata = ap / "worlds" / "planetzoo" / "data"

    # APWorld is authoritative on IDs. Import its maps (read the data files directly to avoid the
    # world's hardcoded-relative-path open() calls; they only resolve from the AP parent dir).
    items_raw = json.loads((pzdata / "items.json").read_text(encoding="utf-8"))
    old_items_raw = json.loads((pzdata / "old_items.json").read_text(encoding="utf-8"))
    item_entries = items_raw + old_items_raw  # SAME order as Items.complete_item_list
    item_name_to_id = {e["name"]: 1000 + i for i, e in enumerate(item_entries)}
    item_class = {e["name"]: e["ap_classification"] for e in item_entries}

    species_locs = json.loads((pzdata / "specieslocations.json").read_text(encoding="utf-8"))
    mech_locs = json.loads((pzdata / "mech_n_milestones.json").read_text(encoding="utf-8"))
    loc_entries = species_locs + mech_locs  # SAME order as Locations.complete_location_list

    specieses = json.loads((pzdata / "specieses.json").read_text(encoding="utf-8"))
    lab2sid = {s["label"]: s["stringid"] for s in specieses}
    catalog = json.loads((pzdata / "research_catalog.json").read_text(encoding="utf-8"))
    token_index = build_token_index(catalog)

    # --- items ---
    items = []
    for name, iid in sorted(item_name_to_id.items(), key=lambda kv: kv[1]):
        eff = map_item(name, lab2sid, token_index)
        items.append({"id": iid, "name": name, "classification": classification(item_class[name]),
                      **eff})

    # --- locations ---
    from types import SimpleNamespace
    locations = []
    for i, e in enumerate(loc_entries):
        loc = SimpleNamespace(type=SimpleNamespace(value=e["type"]), species_type=e["species_type"])
        trig = map_location(e["stringid"], loc)
        locations.append({"id": 2000 + i, "name": e["stringid"], **trig})

    # --- species (gate = permit [+ water tools]; flagship = giant panda) ---
    species = []
    for s in specieses:
        sid, label = s["stringid"], s["label"]
        tokens = [f"permit_{sid}"]
        if s.get("water_needed"):
            tokens.append("water_tools")
        species.append({"key": sid, "name": label, "engine_token": engine_token(sid, label),
                        "gate": " + ".join(tokens),
                        **({"flagship": True} if sid == "gpanda" else {})})

    data = {
        "meta": {
            "game": "Planet Zoo",
            "schema_version": 2,
            "scope": "v1.0-full",
            "mode": "challenge",
            "generated_by": "tools/build_data_json.py from the Planet Zoo APWorld",
            "notes": ("IDs/names mirror the APWorld EXACTLY: items = 1000+index of "
                      "worlds/planetzoo/data/(items.json + old_items.json); locations = 2000+index of "
                      "(specieslocations.json + mech_n_milestones.json). Do not hand-edit - "
                      "regenerate with tools/build_data_json.py. Species keys = specieses.json stringid."),
            "id_ranges": {"items": "1000-1999", "locations": "2000-2999"},
        },
        "species": species,
        "items": items,
        "locations": locations,
        "slot_data": {
            "goal": {"type": "breed", "args": {"required_breed": ["gpanda"]}},
            "death_link": False, "escape_link": False, "options_echo": {},
        },
    }
    out_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    eff_counts: dict = {}
    for it in items:
        eff_counts[it["effect_type"]] = eff_counts.get(it["effect_type"], 0) + 1
    trig_counts: dict = {}
    for lo in locations:
        trig_counts[lo["trigger_type"]] = trig_counts.get(lo["trigger_type"], 0) + 1
    print(f"wrote {out_path}: {len(items)} items, {len(locations)} locations, {len(species)} species")
    print(f"  item effects:   {eff_counts}")
    print(f"  loc triggers:   {trig_counts}")


if __name__ == "__main__":
    main()
