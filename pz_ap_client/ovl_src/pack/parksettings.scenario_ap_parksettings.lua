-- Archipelago scenario shell: MINIMAL park settings - only deliberate AP choices.
-- Omitted numerics/tables fall back to sane baselines; ALL BOOLEANS ARE EXPLICIT.
-- Why (from the scenariomanager decompile):
--   * The park loads from the game's shipped Scenario_01_Empty.bin, an OLD
--     SANDBOX-MODE save - its serialised blob is the real baseline, not
--     ScenarioManager.Init's defaults. WorldLoad even force-enables
--     bUnlimitedCash/CCs for sandbox saves below version 5 (= this bin), so
--     every boolean we care about must be overridden here, false included.
--   * Numeric/table fields are nil-guarded everywhere -> omitted = baseline
--     (Init defaults are multipliers=1 etc.; the fresh sandbox bin matches).
--   * MergeParkSettingData reads nRefundMultiplier / nTrackRefundMultiplier
--     UNGUARDED right after WorldLoad -> both must be present.
-- MarketingSettings / ParkDemographicsSettings / ParkRatingSettings sub-tables
-- are skipped by their managers when absent (defaults: campaigns per data
-- bDefault, standard demographics, no guest cap).
local ParkSettings = module(...)
ParkSettings.Settings = {
  ScenarioSettings = {
    -- normal-play behaviours ON
    bCanEditOpeningTimes = true, bCanHireNewStaff = true, bCanStaffQuitOrBeFired = true,
    bDelayInspectionSpawnTillFirstAnimalAdded = true, bDifficultyTogglesEnabled = true,
    bEnableDefecation = true, bEnableEscapes = true, bEnableFoodSpoiling = true,
    bEnableGuestFleeing = true, bEnableHardShelter = true, bEnableLitter = true,
    bEnableMating = true, bEnableNegativeEffectFacilities = true, bEnableOvercrowding = true,
    bEnablePickpockets = true, bEnablePlantWelfare = true, bEnableProtesters = true,
    bEnableSocialGroups = true, bEnableStaffFleeing = true, bEnableStress = true,
    bEnableTemperature = true, bEnableTerrain = true, bKeepClutter = true,
    bKeepGuests = true, bStartOpen = true,
    -- sandbox leftovers OFF (the empty bin is a sandbox save - override them all)
    bAnimalTogglesEnabled = false, bDisableAddLakes = false, bDisableAgeDeath = false,
    bDisableAging = false, bDisableAnimalWelfare = false, bDisableDeath = false,
    bDisableFenceDilapidation = false, bDisableFights = false, bDisableGuestEnergy = false,
    bDisableGuestHappiness = false, bDisableGuestHunger = false, bDisableGuestThirst = false,
    bDisableGuestToilet = false, bDisableHostileInteractions = false, bDisableInfection = false,
    bDisableInjury = false, bDisableInspection = false, bDisableMaintenance = false,
    bDisablePower = false, bDisableRemoveLakes = false, bDisableStaffChoosingToQuit = false,
    bDisableStaffEnergyDegradation = false, bDisableStaffHappinessDegradation = false,
    bDisableTerrainEdit = false, bDisableVandalism = false, bDisableWaterTreatment = false,
    bDisableZooEntrancePower = false, bEnableGuestInfiniteCash = false,
    bEnableGuestStayLonger = false, bEnableSandboxResearch = false, bFullyTrainStaff = false,
    bGamePlayTogglesEnabled = false, bMaximiseGuestEducation = false, bPowerCutsEnabled = false,
    bSpawnInspector = false, bSpawnReporter = false, bUnlimitedCCs = false,
    bUnlimitedCash = false,
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
