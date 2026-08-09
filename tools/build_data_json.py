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


# Money items: the ROOM decides the actual amounts via the APWorld options (slot_data
# `filler_amounts_cash` / `filler_amounts_conservation` = the MEDIUM amount; Small = half,
# Large = double - see Options.FillerAmountsCash/-Conservation). data.json carries each item's
# SIZE (what the client scales by) plus a fallback `amount` = the option DEFAULT resolved per
# size, used only when slot_data lacks the option (e.g. console/dev rooms).
CASH = {"Cash Inject Small": ("small", 250),
        "Cash Inject Medium": ("medium", 500),      # FillerAmountsCash default
        "Cash Inject Large": ("large", 1000)}
CC = {"Conservation Credits Small": ("small", 100),
      "Conservation Credits Medium": ("medium", 200),  # FillerAmountsConservation default
      "Conservation Credits Large": ("large", 400)}
PROGRESSIVE = {
    "Progressive Supplement Level": "supplement",
    "Progressive Education Level": "education",
    "Progressive Breeding Level": "breeding",
    "Progressive Exhibit Enrichment Level": "exhibit_enrichment",
    # Barriers: 6 levels -> 6 barrier grades, unlocked in grade order by the client (rewards.py
    # BARRIER_GRADE_CONTENT). Without this, the item fell through to a bogus research_reward token.
    "Progressive Barrier Level": "barrier",
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
# catalog's species key. For most species norm(label) IS the engine token; these 6 diverge (catalog/
# label dropped/abbreviated words, incl. typo "brazillian" and the apworld label corrupting
# "Black-and-White" -> "Black a White" while the engine drops "and" entirely -> BlackWhiteRuffedLemur),
# so alias them explicitly.
# (4 more - aleopard/cpeccary/rdeer/mrose - have no catalog welfare tree, but their norm(label) DOES
# match the engine token, so runtime registry attribution still works without an alias.)
ENGINE_TOKEN_ALIAS = {
    "aelephant": "africanelephant",        # label "African Savannah Elephant"
    "acentipede": "amazongiantcentipede",  # label "Amazonian Giant Centipede"
    "liguana": "antilleaniguana",          # label "Lesser Antillean Iguana"
    "btarantula": "brazilliansalmonpinktarantula",  # catalog typo "brazillian"
    "lfrog": "lehmannspoisonfrog",         # label "Lehmann Poison Frog" (catalog "lehmanns")
    "blemur": "blackwhiteruffedlemur",     # engine = BlackWhiteRuffedLemur (no "and"); apworld label "Black a White"
}


def engine_token(stringid: str, label: str) -> str:
    return ENGINE_TOKEN_ALIAS.get(stringid) or _norm(label)


# APWorld item DISPLAY names that no longer round-trip (convert_readable) to their ENGINE content
# token: the 2026-07-17 APWorld renamed the "Orient" theme to "East Asian" and fixed the "Just
# AMomento" typo for DISPLAY, but the engine's research branch/content names are unchanged
# (verified against the DLC-complete tools/research_catalog.json: only OrientThemeSets* /
# SouvenirShopsJustAMomento exist). Without the alias these fell back to the display name as the
# token, which breaks the /pz_install ApGate minting AND the client's mechanic-content reconcile
# (both resolve gates by 'apgate' + norm(content)).
ITEM_TOKEN_ALIAS = {
    "East Asian Theme Sets Scenery": "OrientThemeSetsScenery",
    "East Asian Theme Sets Blueprints Level 1": "OrientThemeSetsBlueprintsL1",
    "East Asian Theme Sets Blueprints Level 2": "OrientThemeSetsBlueprintsL2",
    "East Asian Theme Sets Blueprints Level 3": "OrientThemeSetsBlueprintsL3",
    "Souvenir Shops Just A Momento": "SouvenirShopsJustAMomento",
}


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
        size, amount = CASH[name]
        return {"effect_type": "cash", "effect_args": {"size": size, "amount": amount}}
    if name in CC:
        size, amount = CC[name]
        return {"effect_type": "cc", "effect_args": {"size": size, "amount": amount}}
    if name.strip() in PROGRESSIVE:
        return {"effect_type": "progressive_research_reward", "effect_args": {"family": PROGRESSIVE[name.strip()]}}
    token = ITEM_TOKEN_ALIAS.get(name) or token_index.get(name)
    if token is None:
        # Decoupled reward whose token didn't round-trip through the catalog (e.g. an item added
        # outside the catalog-derived set). Fall back to the display name; flag it loudly.
        print(f"  WARN: no content token for decoupled item {name!r} - using name as token", file=sys.stderr)
        token = name
    return {"effect_type": "research_reward", "effect_args": {"content": token}}


def map_location(entry: dict, token_index: dict) -> dict:
    stringid, sp, ltype = entry["stringid"], entry["species_type"], entry["type"]
    if ltype == "research welfare":
        # Exhibit-species welfare stringids carry an "e_" prefix (e_welfare1_gdscorpian); habitat
        # ones don't (welfare1_aardvark). Accept either so the per-level location gets its level.
        m = re.match(r"(?:e_)?welfare(\d+)_", stringid)
        level = int(m.group(1)) if m else None
        return {"trigger_type": "research_complete",
                "trigger_args": {"research_key": f"welfare_{sp}", "level": level, "species_key": sp}}
    if ltype == "firsts":
        if stringid.startswith("fa_"):
            return {"trigger_type": "first_acquire", "trigger_args": {"species_key": sp}}
        return {"trigger_type": "first_breed", "trigger_args": {"species_key": sp}}
    if ltype == "conservation":
        return {"trigger_type": "conservation_release", "trigger_args": {"species_key": sp}}
    if ltype == "milestones":
        if stringid.startswith("zoo_rating"):
            return {"trigger_type": "milestone",
                    "trigger_args": {"metric": "zoo_rating", "threshold": int(stringid.replace("zoo_rating", ""))}}
        if stringid.startswith("guests_"):
            return {"trigger_type": "milestone",
                    "trigger_args": {"metric": "guest_count", "threshold": int(stringid.split("_")[1])}}
    # mechanic (and any fallthrough): a per-item mechanic-research completion. Since the 2026-08-09
    # apworld the stringids are opaque codes (A1, C3, staff_a_large) - the LABEL is the readable form
    # of the engine research-item name, so recover the ENGINE NAME the same way item content tokens
    # are recovered (convert_readable replay + the rename aliases). research.py resolves it against
    # the live cat-3 records by name; an unrecovered key degrades to not-firing (flagged loudly).
    token = ITEM_TOKEN_ALIAS.get(entry["label"]) or token_index.get(entry["label"])
    if token is None:
        print(f"  WARN: no engine name for mechanic location {entry['label']!r} - using label as key",
              file=sys.stderr)
        token = entry["label"]
    return {"trigger_type": "research_complete",
            "trigger_args": {"research_key": token, "mechanic": True}}


def main() -> None:
    out_path = REPO / "data.json"
    if "--out" in sys.argv:
        out_path = Path(sys.argv[sys.argv.index("--out") + 1])
    ap = find_apworld()
    sys.path.insert(0, str(ap))
    os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
    pzdata = ap / "worlds" / "planetzoo" / "data"

    # APWorld is authoritative on IDs. Since the 2026-08-09 apworld (bd4cc1cd "runs based on the
    # jsons") items.json and locations.json carry EXPLICIT per-entry `id` fields - the old positional
    # 1000+index / 2000+index scheme over the (items+old_items / specieslocations+mech_n_milestones)
    # concatenations is gone (those files moved to data/old_data/). Read the two sources directly.
    item_entries = json.loads((pzdata / "items.json").read_text(encoding="utf-8"))
    loc_entries = json.loads((pzdata / "locations.json").read_text(encoding="utf-8"))

    specieses = json.loads((pzdata / "specieses.json").read_text(encoding="utf-8"))
    lab2sid = {s["label"]: s["stringid"] for s in specieses}
    catalog = json.loads((pzdata / "research_catalog.json").read_text(encoding="utf-8"))
    token_index = build_token_index(catalog)

    # --- items ---
    items = []
    for e in sorted(item_entries, key=lambda e: e["id"]):
        name = e["name"].strip()
        eff = map_item(name, lab2sid, token_index)
        items.append({"id": e["id"], "name": name,
                      "classification": classification(e["ap_classification"]), **eff})

    # --- locations ---
    locations = []
    for e in sorted(loc_entries, key=lambda e: e["id"]):
        trig = map_location(e, token_index)
        # name = the APWorld LABEL (Locations keys location_name_to_id by `label`, so the label IS the
        # name the AP server uses). The stringid only drives the trigger mapping (welfare/fa/fb/...).
        locations.append({"id": e["id"], "name": e["label"], **trig})

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
            "notes": ("IDs/names mirror the APWorld EXACTLY: the explicit per-entry `id` fields of "
                      "worlds/planetzoo/data/items.json and locations.json (sources of truth since "
                      "apworld bd4cc1cd). Do not hand-edit - regenerate with tools/build_data_json.py. "
                      "Species keys = specieses.json stringid."),
            "id_ranges": {
                "items": f"{min(i['id'] for i in items)}-{max(i['id'] for i in items)}",
                "locations": f"{min(l['id'] for l in locations)}-{max(l['id'] for l in locations)}",
            },
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
