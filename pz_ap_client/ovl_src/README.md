# ovl_src - the AP scenario shell sources (deployed by /pz_install)

**Everything here is authored and committed** - no Frontier content. The v19
rewrite (2026-06-11) eliminated the two derived files the earlier shell carried
(the vanilla career-table copy and the Scenario_01 park-settings copy) by using
the engine's own extension points, discovered from gamescript decompiles:

## `pack/` -> the standalone `ovldata\PZArchipelago\` content pack

Built with cobra `new` into a ~6 KB Main.ovl + a Manifest.xml (the engine
discovers content packs by folder scan - the same mechanism every DLC uses).

- `database.pzarchipelagoluadatabase.lua` - the GameDatabase hot-plug hook
  (`Database.<PackName>LuaDatabase` is tryrequired per pack by `Database.Main`).
- `database.archipelagocareerdata.lua` - ADDITIVE career data: our scenario
  entry (career codes are unique-asserted, sets MERGE by code, so we extend the
  first career set's park list without touching Frontier's table).
- `parksettings.scenario_ap_parksettings.lua` - MINIMAL park settings: only
  deliberate AP choices. Engine defaults cover the rest (`ScenarioManager.Init`
  is permissive); the file documents the WorldLoad nil-guard rules that decide
  what must be stated explicitly.
- `objectivesettings.scenario_ap_objectives.lua` - placeholder objectives
  (real AP location-check objectives are a later milestone).
- `scenarioscripts.scenario_ap_script.lua` - the in-game AP brain (session
  marker, settings merge, APERR notes).

## `pack_loc/` -> the pack's `Localised\<Language>\<Region>\Loc.ovl`

One `.txt` per localisation key (filename = lowercased key, content = the
string; the game's own loc format). The career UI **localises** the careerdata
title/label/description fields - plain strings render empty - so the entry uses
bracketed keys and these files supply the text. Built into one Loc.ovl (cobra
`new`) and mirrored by the installer into every language leaf that the user's
`Content0\Localised` ships (same English text everywhere until translated).
Bonus: the scenario-name key also makes the career flow natively name new parks
"ARCHIPELAGO ZOO" (`careerselectmode` calls `SetParkName(GetLocalisedText(...))`),
which is the AP-session marker - the scenario script re-plants it on every load
as belt-and-braces.

## `content0/` -> injected into `Content0\Main.ovl` (the one remaining inject)

- `scenarioscripts.scenarioscriptutils.lua` - the script-table hijack
  (hand-rewritten from scratch around a decompile's trivial scaffold). It must
  REPLACE the vanilla module - the script-type table is built there - so it
  cannot ride in the pack; this is what keeps the ~4-minute cobra inject step.

## Invariants

- The installer (`pz_ap_client/ovl.py`) checks all three subdir manifests
  (`PACK_SRC_FILES` / `PACK_LOC_SRC_FILES` / `CONTENT0_SRC_FILES`) and fails
  loudly if incomplete.
- Only `.lua`/`.txt` sources are packed/injected/digested - this README never
  reaches an ovl. Loc `.txt` files must be UTF-8 WITHOUT BOM (a BOM corrupts
  the displayed string).
- The park settings must state EVERY boolean explicitly (true and false): the
  park loads from the game's shipped `Scenario_01_Empty.bin`, an old
  sandbox-mode save whose serialised blob is the baseline - WorldLoad even
  force-enables unlimited cash/CCs for sandbox saves older than version 5, so
  omitted booleans inherit sandbox behaviour, not engine defaults.
- The park-name marker string ("ARCHIPELAGO ZOO") must stay in sync three ways:
  the scenario script, the careerdata `label`, and `memory/session.AP_PARK_NAME`.
- The careerdata `code` must remain `Scenario_01_Empty` (Design C: it equals the
  sScenarioCode baked into the game's shipped empty-terrain bin, which makes the
  engine natively merge our settings at load).
