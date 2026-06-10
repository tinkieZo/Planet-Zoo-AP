-- AP-patched ScenarioScriptUtils (Design B): vanilla 6 script names +
-- ScenarioScripts.Scenario_AP_Script, hand-rewritten from the clean luadec decompile.
--
-- THE HIJACK: the AP career entry loads the shipped empty-terrain bin
-- Scenarios_Empty\Scenario_01_Empty.bin (NEVER patch park bins - any in-place string
-- patch corrupts the save string table). That save carries
-- sScriptType='scenarioscripts.scenario_01_script'. ScenarioScriptManager resolves
-- that name through the table WE build here, so when the career menu passed code
-- 'Scenario_01_Empty' (read from INextWorldDataManager BEFORE scenariomanager
-- consumes it), we map scenario_01's key to OUR script class for this world only.
-- Vanilla Scenario_01 starts pass code 'Scenario_01' -> no hijack.
local global = _G
local api = global.api
local tryrequire = global.tryrequire
local string = global.string
local table = require("Common.tableplus")
local ScenarioScriptUtils = module(...)
local tScenarioScriptNames = {
  "ScenarioScripts.Scenario_01_Script",
  "ScenarioScripts.Scenario_02_Script",
  "ScenarioScripts.Scenario_03_Script",
  "ScenarioScripts.Scenario_13_Script",
  "ScenarioScripts.Scenario_14_Script",
  "ScenarioScripts.Scenario_15_Script",
  "ScenarioScripts.Scenario_AP_Script"
}
ScenarioScriptUtils.ScenarioScriptTypes = function()
  local tScenarioScriptTypes = {}
  for i = 1, #tScenarioScriptNames do
    local sType = tScenarioScriptNames[i]
    local oType = tryrequire(sType)
    api.debug.Assert(oType ~= nil, "ScenarioScript type '" .. sType .. "' doesn't match a lua file")
    if oType then
      tScenarioScriptTypes[oType._NAME] = oType
    end
  end
  -- AP context capture: this function runs during ScenarioScriptManager.Init, where
  -- api.game.GetEnvironment() WORKS (proven: the career-code read below). Inside the
  -- later InitScript/load coroutine it returns nil and GetScript(GetActive()) is the
  -- UI script - so the AP script CANNOT acquire IScenarioManager itself; we capture
  -- it here and stash it on the class. Re-captured fresh on every world init.
  local oAP = tScenarioScriptTypes["scenarioscripts.scenario_ap_script"]
  if oAP then
    -- Capture facts (boot tests 8/9): in THIS context api.game.GetEnvironment() and
    -- INextWorldDataManager work, but RequireInterface("Interfaces.IScenarioManager")
    -- returns nil - IScenarioManager is not yet registered at manager-Init time (the
    -- manager itself uses the LAZY RequestInterface here). So: capture the ENV object
    -- + a RequestInterface proxy; the AP script resolves at Init time, when the
    -- scenario manager is definitely up. The hijack depends ONLY on the career code -
    -- coupling it to the manager capture is what silently disabled v6.
    local bOK, tCtx = global.pcall(function()
      local env = (api.game.GetEnvironment)()
      local nwdm = env:RequireInterface("Interfaces.INextWorldDataManager")
      local sm = nil
      global.pcall(function()
        sm = env:RequestInterface("Interfaces.IScenarioManager")
      end)
      return {sCode = nwdm:GetScenarioCode(), env = env, scenarioManager = sm}
    end)
    if bOK and tCtx and tCtx.sCode == "Scenario_01_Empty" then
      tScenarioScriptTypes["scenarioscripts.scenario_01_script"] = oAP
      oAP._tAPContext = tCtx
      -- belt-and-braces: also stash in _G in case the module table seen here is not
      -- the one the AP script's Init closure reads (per-env module registries)
      global.pcall(function()
        global.g_tAPContext = tCtx
      end)
    end
  end
  return tScenarioScriptTypes
end
ScenarioScriptUtils.ScenarioScriptNames = function()
  local tNames = table.copy(tScenarioScriptNames)
  for i = 1, #tNames do
    tNames[i] = string.lower(tNames[i])
  end
  return tNames
end
