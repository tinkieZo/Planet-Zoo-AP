-- Archipelago scenario script (Design C, v18). The careerdata code 'Scenario_01_Empty'
-- equals the sScenarioCode baked into the shipped empty-terrain bin, so the ENGINE
-- natively merges our parksettings/objectivesettings at load; this script (activated
-- via the scriptutils hijack) runs at darwinworld InitScript AFTER that merge and only
-- adds belt-and-braces merges, startParams, and the AP-session park-name marker.
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
  -- NOT a tutorial. ScenarioScriptManager calls _EnableScenarioScopedSettings(not complete and
  -- oActiveScript.isTutorial) right AFTER this Init; a truthy isTutorial FREEZES the park
  -- (SetPlayerAnimalMovementEnabled(false) + disables mating/ageing/death/breakdowns), which greys
  -- out "Release to Wild" ("Animal must stay in zoo for this scenario") and blocks the AP
  -- conservation loop (the cr_<species> checks). An AP challenge zoo is normal play, so clear the
  -- flag -> the engine runs the else branch (movement + death + ageing + breakdowns all enabled).
  self.isTutorial = false
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
  -- AP-SESSION MARKER: name the park natively. SetParkName (executor 0x14667A8F0)
  -- interns the string at park-info+0x1E8, which PERSISTS in saves; the AP client
  -- reads it back (pz_ap_client/memory/session.py) to detect "this loaded park IS
  -- the AP scenario" and stays idle in every other park. Keep the string in sync
  -- with session.AP_PARK_NAME and the careerdata entry label. Also fixes the blank
  -- zoo-name display (the frontend's GetLocalisedText(scenarioName) is nil for our
  -- plain-string label, so the load path named the park nil).
  _appcall("parkname", function()
    local tWorldAPIs = (api.world.GetWorldAPIs)()
    local parkAPI = tWorldAPIs.park
    parkAPI:SetParkName("ARCHIPELAGO ZOO")
  end)
  -- ANIMAL MOVEMENT: belt-and-braces alongside isTutorial=false - explicitly enable player animal
  -- movement so Release to Wild / trade are available (the AP conservation loop needs releases),
  -- in case the scoped-settings pass is skipped on some load path.
  _appcall("animalmovement", function()
    local tWorldAPIs = (api.world.GetWorldAPIs)()
    tWorldAPIs.animals:SetPlayerAnimalMovementEnabled(true)
    tWorldAPIs.exhibits:SetPlayerAnimalMovementEnabled(true)
  end)
  -- RELEASE-TO-WILD ENABLE (the working lever): register an objective hint carrying
  -- playerAnimalMovement=true. ScenarioScriptManager.AddObjectiveTrigger stores it as
  -- tHintData.bPlayerAnimalMovement (scenarioscriptmanager.dec ln992), and
  -- _ValidateScriptStateAfterLoad - run right after this Init (bValidateScriptState set at
  -- InitScript ln670) - applies the current objective's hints via the ACTIVE manager
  -- (ln379: animals/exhibits:SetPlayerAnimalMovementEnabled(true)). So gate A is set true on the
  -- ACTIVE world APIs the release UI actually checks (animaldatabasetab.dec ln248), skipping the
  -- whole "must stay in zoo" block. This is the in-the-right-context fix: our Init/GetWorldAPIs
  -- runs in the loading env and can't reach the active OM/animals, but the trigger is processed
  -- by the active ScenarioScriptManager. (Supersedes the loading-env animalmovement call above +
  -- the objenable override experiment below, which never reached the active instances.)
  _appcall("movementtrigger", function()
    self:AddObjectiveTrigger({difficulty = 1, objective = 1, playerAnimalMovement = true})
    _apnote("APDBG:movementtrigger registered (obj 1, playerAnimalMovement=true)")
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
  -- OBJECTIVE MANAGER ENABLE: in a normal scenario the ScenarioIntroManager enables it
  -- (scenariointromanager.dec Activate line 56 / post-cinematic line 100:
  -- ObjectiveManager:SetEnabled(true, sCode, ...)). Our hijacked empty bin never runs that
  -- intro/activate path, so bEnabled stays FALSE and the engine's IsMovementForAnimalAllowed
  -- (objectivemanager.dec line 1435: "if not self.bEnabled then return false") short-circuits
  -- to false for EVERY animal -> "Release to Wild"/Quick-Trade greyed ("Animal must stay in
  -- zoo for this scenario"), which blocks the AP conservation loop (cr_<species> checks).
  -- Fix: enable it ourselves. Deferred a few frames so it lands AFTER any scenario-activate
  -- pass (which would set bEnabled=false for us); one proper SetEnabled(true) loads the
  -- objectives GUI, then we hold bEnabled true against a late override (field-only re-assert,
  -- no GUI reload; a no-op if the world API is a proxy rather than the lua component table).
  -- The release gate (IsMovementForAnimalAllowed, objectivemanager.dec line 1435) short-circuits
  -- to false unless ObjectiveManager.bEnabled is true. Our hijacked bin never runs
  -- ScenarioIntroManager's enable (Activate ln56 / post-cinematic ln100), so it stays false ->
  -- "Release to Wild" greyed, blocking the AP conservation loop. SetEnabled(true) throws here (it
  -- loads the objectives GUI we never set up), so set the field DIRECTLY. Done SYNCHRONOUSLY in
  -- Init using the same GetWorldAPIs() objmerge proved resolves the PARK manager - a deferred
  -- api.task runs in the frontend-pinned context where GetWorldAPIs().ObjectiveManager is nil
  -- (the v1 deferred attempt never fired). Report the real gate state (bEnabled / OM type /
  -- GetMoveableAnimals) before+after so a heap scan tells us exactly what happened.
  -- Do NOT set bEnabled=true: that hard-crashes on load (v3) - downstream load code does
  -- `if self.bEnabled then ... guiWrapper:X()` and our guiWrapper is nil (SetEnabled's GUI setup
  -- never ran). Instead OVERRIDE the single gate the release/trade UI calls -
  -- IsMovementForAnimalAllowed (objectivemanager.dec ln1435; it returns false while bEnabled is
  -- false) - so it always permits movement. Pure-lua instance-method override (the v3 crash on a
  -- field WRITE proved OM is a table): shadows the class method for THIS OM only, no mid-load side
  -- effect (nothing calls it during load), no GUI, no bEnabled change.
  _appcall("objenable", function()
    local tWorldAPIs = (api.world.GetWorldAPIs)()
    -- gate B: ObjectiveManager:IsMovementForAnimalAllowed (animaldatabasetab.dec ln252 -> the
    -- "bBlockedByScenario" / "must stay in zoo" issue). Override -> always allow. A v4 INSTANCE
    -- override on GetWorldAPIs().ObjectiveManager didn't reach the UI, so the UI's OM is a
    -- DIFFERENT instance (loading-world vs active-world). Override the CLASS via the instance
    -- metatable (__index) so EVERY instance of that class inherits it. fnAllow logs its first call
    -- so a heap scan tells us whether the UI actually hits our override.
    local fnAllow = function(_self, _nAnimalID, _sDest)
      if not Scenario_AP_Script._bMoveCalled then
        Scenario_AP_Script._bMoveCalled = true
        _apnote("APDBG:moveallow CALLED dest=" .. global.tostring(_sDest))
      end
      return true
    end
    local OM = tWorldAPIs.ObjectiveManager
    _apnote("APDBG:objstate type=" .. global.type(OM))
    if global.type(OM) == "table" then
      OM.IsMovementForAnimalAllowed = fnAllow                       -- this instance
      local mt = global.getmetatable(OM)
      local cls = mt and mt.__index
      if global.type(cls) == "table" then
        cls.IsMovementForAnimalAllowed = fnAllow                    -- the class -> all instances
        _apnote("APDBG:objpatch class+instance set")
      else
        _apnote("APDBG:objpatch instance only (cls=" .. global.tostring(cls) .. ")")
      end
    end
    -- gate A: animals:IsPlayerAnimalMovementEnabled (animaldatabasetab.dec ln248). TRUE there
    -- skips the whole per-animal block. Likely native userdata (override a no-op) but harmless.
    global.pcall(function()
      local an = tWorldAPIs.animals
      if global.type(an) == "table" then
        an.IsPlayerAnimalMovementEnabled = function()
          return true
        end
        _apnote("APDBG:objpatch animals gateA set")
      else
        _apnote("APDBG:objpatch animals type=" .. global.type(an))
      end
    end)
  end)
  -- DIAGNOSTIC: report the LIVE game mode via IScenarioManager (the interface DOES expose
  -- GetGameMode/IsScenarioCareerMode/IsSandboxMode/IsScenarioEditMode). This tells us whether our
  -- scenario is truly ScenarioCareer at runtime (=> objectives SHOULD enable via ScenarioIntroManager;
  -- fix = force the enable) or stuck Sandbox (=> fix = the mode). smp was resolved above; re-resolve
  -- fresh if nil.
  _appcall("ps_modereport", function()
    local sm = smp
    if sm == nil and tCtx and tCtx.env then
      global.pcall(function()
        sm = (tCtx.env):RequireInterface("Interfaces.IScenarioManager")
      end)
      if sm == nil then
        global.pcall(function()
          sm = (tCtx.env):RequestInterface("Interfaces.IScenarioManager")
        end)
      end
    end
    if sm ~= nil then
      _apnote("APDBG:mode gm=" .. tostring(sm:GetGameMode())
        .. " career=" .. tostring(sm:IsScenarioCareerMode())
        .. " sandbox=" .. tostring(sm:IsSandboxMode())
        .. " edit=" .. tostring(sm:IsScenarioEditMode())
        .. " scen=" .. tostring(sm:IsScenarioMode())
        .. " code=" .. tostring(sm:GetScenarioCode()))
    else
      _apnote("APDBG:mode UNRESOLVED (no IScenarioManager handle at Init)")
    end
  end)
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
