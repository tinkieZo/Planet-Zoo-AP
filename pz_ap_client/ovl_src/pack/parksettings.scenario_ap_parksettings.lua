-- Archipelago scenario shell: MINIMAL park settings - only deliberate AP choices.
-- Everything omitted falls back to the engine's own defaults, which the
-- scenariomanager decompile shows are already permissive (bDisable* = false,
-- bCanHireNewStaff = true, multipliers = 1, tExchangeSpawns = {}). Field rules
-- derived from ScenarioManager.WorldLoad:
--   * boolean fields are assigned UNGUARDED (nil reads as false) -> every
--     true-boolean we rely on must be stated explicitly;
--   * numeric/table fields are nil-guarded -> omitted = engine default;
--   * MergeParkSettingData reads nRefundMultiplier / nTrackRefundMultiplier
--     UNGUARDED right after WorldLoad -> both must be present.
-- MarketingSettings / ParkDemographicsSettings / ParkRatingSettings sub-tables
-- are skipped by their managers when absent (defaults: campaigns per data
-- bDefault, standard demographics, no guest cap).
local ParkSettings = module(...)
ParkSettings.Settings = {
  ScenarioSettings = {
    -- normal-play behaviours kept ON (nil would silently turn them off)
    bCanEditOpeningTimes = true, bCanHireNewStaff = true, bCanStaffQuitOrBeFired = true,
    bDelayInspectionSpawnTillFirstAnimalAdded = true, bDifficultyTogglesEnabled = true,
    bEnableDefecation = true, bEnableEscapes = true, bEnableFoodSpoiling = true,
    bEnableGuestFleeing = true, bEnableHardShelter = true, bEnableLitter = true,
    bEnableMating = true, bEnableNegativeEffectFacilities = true, bEnableOvercrowding = true,
    bEnablePickpockets = true, bEnablePlantWelfare = true, bEnableProtesters = true,
    bEnableSocialGroups = true, bEnableStaffFleeing = true, bEnableStress = true,
    bEnableTemperature = true, bEnableTerrain = true, bKeepClutter = true,
    bKeepGuests = true, bStartOpen = true,
    -- AP economy start (cash is engine cents: $150,000)
    nInitialCash = 15000000,
    nInitialCCs = 0,
    tZooReputationCCRewards = {[1] = 0, [2] = 50, [3] = 100, [4] = 150, [5] = 200, [6] = 250},
    -- the scenario market stays natively DORMANT; the AP client arms schedule
    -- slots for unlocked species at runtime (memory/market.py)
    tExchangeSpawns = {},
    -- read unguarded by ScenarioManager.MergeParkSettingData - must exist
    nRefundMultiplier = 1,
    nTrackRefundMultiplier = 0.75,
    -- settings-blob version + difficulty bookkeeping (vanilla-current values)
    nSaveVersion = 7,
    sGameDifficulty = "Default",
    tGameDifficultiesActivatedInPark = {Default = true}
  }
}
