-- PZArchipelago content pack: ADDITIVE career data (no vanilla file replaced).
-- MainCareerData.Init fans out CallOnContent("AddCareerData", fnAddScenario,
-- fnAddSet, fnAddPack):
--   * fnAddScenario asserts the code is UNIQUE and appends - our entry's code
--     "Scenario_01_Empty" is the Design-C key: it equals the sScenarioCode baked
--     into the game's own shipped empty-terrain bin, so save code == careerdata
--     code and the engine natively merges our park/objective settings at load.
--   * fnAddSet MERGES by set code (table.merge, shallow, our keys win) - so we
--     extend the first career set's park list with our scenario while its
--     name/icon/coords survive untouched. The merge is shallow, hence the park
--     list must be restated in full (four scenario code identifiers).
-- Load order: content packs initialize alphabetically, "PZArchipelago" sorts
-- after "Content0", so the vanilla set exists before our merge lands.
local global = _G
local ipairs = global.ipairs
local ArchipelagoCareerData = module(...)

ArchipelagoCareerData.tScenarioData = {
  {
    -- BASE-SWAP: Scenario_22_Empty - a TEMPERATE release-enabled empty (16 baked career objectives,
    -- which set up the ObjectiveManager so Release-to-Wild works; runtime-merged objectives don't).
    -- Chosen for a nicer temperate map. (History: Scenario_01_Empty was a sandbox save -> release blocked;
    -- Scenario_15_Empty fixed release but had an awkward Himalayan terrain; this swaps to Scenario_22_Empty
    -- for the terrain.) Its sScriptType scenario_22_script is registered in our scriptutils list, and code
    -- "Scenario_22_Empty" is unclaimed (the _Empty terrain bin isn't a registered scenario).
    code = "Scenario_22_Empty",
    parkImages = {"scenarioPreview_01"},
    icon = {"scenarioIcon_01"},
    -- Bracketed loc keys: the career UI localises these (plain strings render
    -- EMPTY); the strings ship in our pack's Localised tree (ovl_src/pack_loc/).
    -- The scenario-name key also natively names new parks "ARCHIPELAGO ZOO"
    -- (careerselectmode: SetParkName(GetLocalisedText(name key))) = the AP
    -- session marker; the scenario script re-plants it as belt-and-braces.
    title = "[FrontEndMenu_ScenarioTitle_Scenario_AP]",
    label = "[FrontEndMenu_ScenarioName_Scenario_AP]",
    description = "[FrontEndMenu_ScenarioDetails_Scenario_AP]",
    creator = "[FrontEndMenu_ParkCreator1]",
    -- parkToLoad provides BOTH the terrain AND the baked objectives that release-enable conservation, so
    -- both paths point at the SAME bin (parkToLoadTerrainOnly is NOT honored for our start-new path -
    -- tested 2026-06-22). Scenario_22_Empty = a release-enabled TEMPERATE empty (in Scenarios_Content18_Empty).
    parkToLoad = "/run/Zoos/Scenarios_Content18_Empty/Scenario_22_Empty.bin",
    parkToLoadTerrainOnly = "/run/Zoos/Scenarios_Content18_Empty/Scenario_22_Empty.bin",
    parkSettings = "ParkSettings.Scenario_AP_ParkSettings",
    objectiveSettings = "ObjectiveSettings.Scenario_AP_Objectives",
    longitude = 8.0,
    latitude = 50.0,
    geome = "temperate",
    continent = "europe",
    difficultytocomplete = 1,
    pins = {
      "UIPlanetMarker_West_African_Lion_Stone", "UIPlanetMarker_West_African_Lion_Bronze",
      "UIPlanetMarker_West_African_Lion_Silver", "UIPlanetMarker_West_African_Lion_Gold"
    }
  }
}

ArchipelagoCareerData.tSetData = {
  -- Set-code merge: only `parks` is replaced on the vanilla PRKG set (shallow
  -- merge keeps every other field). Slot 4 = the ARCHIPELAGO entry.
  {code = "PRKG", parks = {"Scenario_01", "Scenario_02", "Scenario_03", "Scenario_22_Empty"}}
}

ArchipelagoCareerData.AddCareerData = function(_fnAddScenario, _fnAddSet, _fnAddPack)
  for _, tScenario in ipairs(ArchipelagoCareerData.tScenarioData) do
    _fnAddScenario(tScenario)
  end
  for _, tSet in ipairs(ArchipelagoCareerData.tSetData) do
    _fnAddSet(tSet)
  end
end
