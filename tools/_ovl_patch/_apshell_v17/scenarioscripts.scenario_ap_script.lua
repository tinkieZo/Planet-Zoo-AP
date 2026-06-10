-- Archipelago scenario script (Design B). The park bin is an UNPATCHED byte-copy of
-- Scenario_01.bin, so the save's code 'scenario01' never resolves in careerdata and the
-- engine's parksettings/objectives merge is skipped (vanilla-tolerated path). This
-- script is activated via the scriptutils hijack and performs both merges itself in
-- Init, which runs AFTER scenariomanager:CompleteWorldSerialisationLoad (darwinworld
-- calls InitScript later in the same sequence), so nothing stomps what we apply here.
-- Errors are kept on the module table with an "APERR:" prefix so the post-bounce heap
-- scanner (tools/_lua_error_scan.py --needle APERR:) can recover them.
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
  self.startParams = {
    placementRestricted = false,
    browserHideAll = false,
    browserShowBlueprints = true,
    wallDilapidation = false
  }
  -- Objectives: same call scenariomanager would have made had the save code resolved.
  -- (Now redundant with Design C's native merge, kept as harmless belt-and-braces.)
  _appcall("objmerge", function()
    local tWorldAPIs = (api.world.GetWorldAPIs)()
    local OS = require("ObjectiveSettings.Scenario_AP_Objectives")
    tWorldAPIs.ObjectiveManager:MergeParkSettingData(OS.Settings, true, true)
  end)
  -- ANIMAL MARKET: deliberately NO market calls here (v16-stable). Probe history
  -- v11-v15 (detail in ap-custom-scenario memory): markets are natively EMPTY in
  -- this scenario until any Set*ActiveWhitelist call activates them - then they
  -- FLOOD ungated (whitelist arg is inert; named whitelists exist only in vanilla
  -- park bins). SetLocalAnimalExchangeScenarioData CRASHES natively in this
  -- context even with a faithful clone of its own Get output (v15) - per-species
  -- market control is an open native-RE task (Ghidra the exchange executors).
  -- Empty-by-default matches the AP gating intent for now.
  -- Park settings (v7): inside InitScript's load coroutine the ambient context is
  -- dead (api.game.GetEnvironment() = nil, GetScript(GetActive()) = the UI script -
  -- both heap-scan proven), so scriptutils captures the ENV OBJECT during
  -- ScenarioScriptManager.Init and stashes it on this class as _tAPContext. The
  -- IScenarioManager interface is NOT yet registered at capture time (boot test 9:
  -- v6's RequireInterface there returned nil and killed the hijack), so resolve it
  -- HERE, lazily, when the scenario manager is definitely up.
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
        .. ",hire2=" .. tostring((smp:GetSettings()).bCanHireNewStaff)
        .. ",terraindis=" .. tostring(smp:GetDisableTerrainEdit()))
    end)
  else
    -- Deferred apply (v8): no scenario-manager handle exists in ANY load-time
    -- context (heap-proven across v3-v7: the captured env is the game env and
    -- lacks IScenarioManager; GetScript(GetActive()) is the UI script until the
    -- world activates). So spawn a cooperative task that polls until the active
    -- world script carries the scenarioManager field (darwinworld, post-activate),
    -- then applies. Capped so a semantics surprise can't spin forever.
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
                .. ":canhire=" .. tostring(sm2:GetCanHireNewStaff())
                .. ",terraindis=" .. tostring(sm2:GetDisableTerrainEdit()))
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
