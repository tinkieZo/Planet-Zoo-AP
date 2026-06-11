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
    code = "Scenario_01_Empty",
    parkImages = {"scenarioPreview_01"},
    icon = {"scenarioIcon_01"},
    title = "ARCHIPELAGO",
    label = "ARCHIPELAGO ZOO",
    description = "Archipelago multiworld scenario (AP shell v19).",
    creator = "[FrontEndMenu_ParkCreator1]",
    parkToLoad = "/run/Zoos/Scenarios_Empty/Scenario_01_Empty.bin",
    parkToLoadTerrainOnly = "/run/Zoos/Scenarios_Empty/Scenario_01_Empty.bin",
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
  {code = "PRKG", parks = {"Scenario_01", "Scenario_02", "Scenario_03", "Scenario_01_Empty"}}
}

ArchipelagoCareerData.AddCareerData = function(_fnAddScenario, _fnAddSet, _fnAddPack)
  for _, tScenario in ipairs(ArchipelagoCareerData.tScenarioData) do
    _fnAddScenario(tScenario)
  end
  for _, tSet in ipairs(ArchipelagoCareerData.tSetData) do
    _fnAddSet(tSet)
  end
end
