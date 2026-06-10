-- Archipelago scenario shell: minimal objectives (one guestcount bronze objective so the
-- objectives UI has content). Real AP location-check objectives are generated later.
-- tData is field-for-field identical to a vanilla guestcount objective (Scenario_04
-- tier 1 [2]) with only nTarget changed, so the Init chain sees a known-good shape.
local ObjectiveSettings = module(...)
ObjectiveSettings.Settings = {
  ChallengeObjectiveSettings = {
    Default = {nGoldChallengeTimeSecs = 3600, nSilverChallengeTimeSecs = 5400},
    Easy = {nGoldChallengeTimeSecs = 5400, nSilverChallengeTimeSecs = 7200},
    Hard = {nGoldChallengeTimeSecs = 1800, nSilverChallengeTimeSecs = 3600}
  },
  ChallengeSettings = {nSpawnMax = 600, nSpawnMin = 360},
  ObjectiveSettings = {
    [1] = {
      [1] = {
        sType = "objectives.objectiveguestcount",
        tData = {
          tConditions = {},
          nMinGuestHappiness = 0.7,
          bHasDeadline = false,
          nDeadlineYear = 1,
          sVisitorDurationType = "Inspector",
          nMonthsToStayValid = 1,
          nDeadlineMonth = 3,
          sVisitorType = "Inspector",
          sDurationType = "Time",
          bHasAchievement = false,
          bUseDurationCheck = false,
          sAchievementEventName = "",
          bNeedMinGuestHappiness = false,
          sDeadlineType = "Time",
          -- placeholder must NOT auto-complete: the Goodwin House park starts with
          -- 2500+ guests, so 50 completed instantly (boot test 4)
          nTarget = 10000,
          bVisitorInPark = false,
          bVisitorArrived = false,
          bIsFailed = false
        }
      }
    },
    -- Tiers 2/3 must exist (even empty): objectivemanager.MergeParkSettingData does
    -- ipairs(ObjectiveSettings[d]) for d=1..3 unconditionally; nil tier = load crash.
    [2] = {},
    [3] = {}
  }
}
