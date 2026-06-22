-- Archipelago scenario script (Design C). The AP careerdata entry's code 'Scenario_22_Empty'
-- equals the base bin we load (Scenarios_Content18_Empty/Scenario_22_Empty.bin) - a TEMPERATE
-- BLANK-terrain save that, unlike a sandbox bin, carries BAKED career objectives. Those baked
-- objectives are what makes the engine release-enable the ObjectiveManager at WorldLoad, so
-- "Release to Wild" / trade work natively (the AP conservation loop needs releases). The whole
-- runtime fight to enable release on the old sandbox base is GONE - it's solved by the base bin.
-- This script (activated via the scriptutils hijack of scenario_22_script) runs at InitScript and
-- adds: the AP-session park-name marker, a belt-and-braces objective merge, and the permissive
-- scenario-settings apply. Errors are kept on the module table with an "APERR:" prefix so the
-- post-bounce heap scanner (tools/_lua_error_scan.py --needle APERR:) can recover them.
local global = _G
local api = global.api
local require = global.require
local tostring = global.tostring
local Object = require("Common.object")
local SUPER = require("ScenarioScripts.ScenarioScript")
local Scenario_AP_Script = module(..., Object.subclass(SUPER))
Scenario_AP_Script.tAPErrors = {}
local function _apnote(_s)
  Scenario_AP_Script.tAPErrors[#Scenario_AP_Script.tAPErrors + 1] = _s
end
local function _appcall(_sStage, _fn)
  local bOK, sErr = global.pcall(_fn)
  if not bOK then
    _apnote("APERR:" .. _sStage .. ": " .. global.tostring(sErr))
  end
  return bOK
end
Scenario_AP_Script.Init = function(self, _iScenarioScriptManager, _tSave)
  SUPER.Init(self, _iScenarioScriptManager, _tSave)
  -- NOT a tutorial: ScenarioScriptManager calls _EnableScenarioScopedSettings(not complete and
  -- oActiveScript.isTutorial) right after this Init; a truthy isTutorial freezes the park
  -- (disables movement/mating/ageing/death/breakdowns). An AP zoo is normal play, so clear it.
  self.isTutorial = false
  self.startParams = {
    placementRestricted = false,
    browserHideAll = false,
    browserShowBlueprints = true,
    wallDilapidation = false
  }
  -- Objectives: belt-and-braces merge of our ObjectiveSettings (redundant with Design C's native
  -- merge at load, harmless). Does NOT remove the bin's baked objectives, so the native
  -- release-enable is preserved.
  _appcall("objmerge", function()
    local tWorldAPIs = (api.world.GetWorldAPIs)()
    local OS = require("ObjectiveSettings.Scenario_AP_Objectives")
    tWorldAPIs.ObjectiveManager:MergeParkSettingData(OS.Settings, true, true)
  end)
  -- AP-SESSION MARKER: name the park natively. SetParkName interns the string at park-info+0x1E8,
  -- which PERSISTS in saves; the AP client reads it back (pz_ap_client/memory/session.py) to detect
  -- "this loaded park IS the AP scenario" and stays idle in every other park. Keep the string in
  -- sync with session.AP_PARK_NAME and the careerdata entry label.
  _appcall("parkname", function()
    local tWorldAPIs = (api.world.GetWorldAPIs)()
    local parkAPI = tWorldAPIs.park
    parkAPI:SetParkName("ARCHIPELAGO ZOO")
  end)
  -- ANIMAL MARKET: deliberately NO market calls here. The species-gating market mechanism is
  -- client-side and is being re-RE'd for this base (Scenario_22_Empty's exchange manager has no
  -- baked schedule like the old base - its market is autofill-populated, so the gate must filter
  -- the autofill rather than arm schedule slots). See [[ap-custom-scenario]] / market.py.
  --
  -- Scenario-settings apply: inside InitScript's load coroutine the ambient context is dead
  -- (api.game.GetEnvironment()=nil, GetScript(GetActive())=the UI script), so scriptutils captures
  -- the ENV OBJECT during ScenarioScriptManager.Init and stashes it as _tAPContext. IScenarioManager
  -- isn't registered at capture time, so resolve it HERE, lazily, when the manager is up.
  local tCtx = Scenario_AP_Script._tAPContext or global.g_tAPContext
  local smp = tCtx and tCtx.scenarioManager or nil
  if smp == nil and tCtx and tCtx.env then
    _appcall("ps_lazyresolve", function()
      smp = (tCtx.env):RequireInterface("Interfaces.IScenarioManager")
    end)
    if smp == nil then
      _appcall("ps_lazyresolve2", function()
        smp = (tCtx.env):RequestInterface("Interfaces.IScenarioManager")
      end)
    end
  end
  local function _apApplySettings(_sm)
    local tSettings = _sm:GetSettings()
    tSettings.bCanHireNewStaff = true
    tSettings.bCanStaffQuitOrBeFired = true
    tSettings.bCanEditOpeningTimes = true
    tSettings.bDisableTerrainEdit = false
    tSettings.bDisableRemoveLakes = false
    tSettings.bDisableAddLakes = false
    _sm:ApplySettings(tSettings)
  end
  if smp ~= nil then
    _appcall("ps_apply", function()
      _apApplySettings(smp)
    end)
    _appcall("ps_readback", function()
      _apnote("APDBG:canhire=" .. tostring(smp:GetCanHireNewStaff())
        .. ",terraindis=" .. tostring(smp:GetDisableTerrainEdit()))
    end)
  else
    -- Deferred apply: no scenario-manager handle exists at Init in any captured context, so spawn a
    -- cooperative task that polls until the active world script exposes scenarioManager, then applies.
    _apnote("APERR:ps_ctx: no scenarioManager at Init (ctx=" .. tostring(tCtx)
      .. ",env=" .. tostring(tCtx and tCtx.env) .. "); spawning deferred task")
    _appcall("ps_spawn", function()
      local coroutine = global.coroutine;
      (api.task).Spawn(function()
        local bDone = false
        for i = 1, 1800 do
          global.pcall(function()
            local ws = (api.world.GetScript)((api.world.GetActive)())
            local sm2 = ws and ws.scenarioManager or nil
            if sm2 then
              _apApplySettings(sm2)
              _apnote("APDBG:deferredapply_iter" .. tostring(i)
                .. ":canhire=" .. tostring(sm2:GetCanHireNewStaff()))
              bDone = true
            end
          end)
          if bDone then
            break
          end
          coroutine.yield()
        end
        if not bDone then
          _apnote("APERR:deferred: world script never exposed scenarioManager (1800 yields)")
        end
      end)
    end)
  end
end
