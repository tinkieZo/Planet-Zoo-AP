"""research_catalog - build the research -> reward catalog from extracted ovl settings files.

    python -m tools.research_catalog [extract_dir] [out_json]

Input: a directory of files extracted from GameMain/Main.ovl via cobra-tools, e.g.
    py -3.11 ovl_tool_cmd.py extract -g "Planet Zoo" -o <dir> \
        --type .animalresearchunlockssettings \
        --type .animalresearchstartunlockedsettings \
        --type .mechanicresearchsettings  <ovldata>/GameMain/Main.ovl

Output: research_catalog.json with three sections:
    species         {species_file: [{level, next, rewards[]}]}        (incl. exhibit species)
    mechanic        {file: [{item, entry_level, enabled, completed, next[]}]}
    start_unlocked  {file: [{entity, level}]}                          (default/franchise/scenario_*)

This is the v1.0 data source for: per-level research locations, the vanilla-reward -> AP-item
pool (reward decoupling), and the initial build-menu lock sets (career-style gating).
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "_research_extract"
DEFAULT_OUT = Path(__file__).resolve().parent / "research_catalog.json"


def _texts(parent, path):
    return [el.text for el in parent.findall(path) if el.text]


def parse_species(path):
    root = ET.parse(path).getroot()
    levels = []
    for lvl in root.findall("./levels/researchlevel"):
        name_el = lvl.find("level_name")
        levels.append({
            "level": name_el.text if name_el is not None else None,
            "next": _texts(lvl, "./next_levels/pointer"),
            "rewards": _texts(lvl, "./children/pointer"),
        })
    return levels


def parse_mechanic(path):
    root = ET.parse(path).getroot()
    entries = []
    for res in root.findall("./levels/research"):
        name_el = res.find("item_name")
        entries.append({
            "item": name_el.text if name_el is not None else None,
            "entry_level": res.get("is_entry_level") == "1",
            "enabled": res.get("is_enabled") == "1",
            "completed": res.get("is_completed") == "1",
            "next": _texts(res, "./next_research/item_name/pointer"),
        })
    return entries


def parse_start_unlocked(path):
    root = ET.parse(path).getroot()
    states = []
    for st in root.findall("./states/unlockstate"):
        ent, lvl = st.find("entity_name"), st.find("level_name")
        states.append({
            "entity": ent.text if ent is not None else None,
            "level": lvl.text if lvl is not None else None,
        })
    return states


def build(extract_dir):
    catalog = {"species": {}, "mechanic": {}, "start_unlocked": {}}
    for path in sorted(Path(extract_dir).iterdir()):
        try:
            if path.suffix == ".animalresearchunlockssettings":
                catalog["species"][path.stem] = parse_species(path)
            elif path.suffix == ".mechanicresearchsettings":
                catalog["mechanic"][path.stem] = parse_mechanic(path)
            elif path.suffix == ".animalresearchstartunlockedsettings":
                catalog["start_unlocked"][path.stem] = parse_start_unlocked(path)
        except ET.ParseError as exc:
            print(f"WARN: failed to parse {path.name}: {exc}")
    return catalog


def main():
    extract_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    catalog = build(extract_dir)
    rewards = {r for lvls in catalog["species"].values() for l in lvls for r in l["rewards"]}
    print(f"species files:   {len(catalog['species'])}")
    print(f"mechanic files:  {len(catalog['mechanic'])}")
    print(f"start files:     {len(catalog['start_unlocked'])}")
    print(f"distinct species-level rewards: {len(rewards)}")
    out.write_text(json.dumps(catalog, indent=1), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
