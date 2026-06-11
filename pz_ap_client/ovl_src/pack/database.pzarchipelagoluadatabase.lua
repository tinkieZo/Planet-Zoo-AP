-- PZArchipelago content pack: GameDatabase hot-plug entry point.
-- Database.Main:InitContentToCall tryrequires "Database.<PackName>LuaDatabase" for
-- every content pack discovered via Manifest.xml (folder scan of ovldata\) and calls
-- AddContentToCall - the same mechanism every DLC uses (e.g. Content1's
-- Database.Content1LuaDatabase). Whatever we insert here receives the
-- CallOnContent("AddCareerData", ...) fan-out at database init.
local global = _G
local table = global.table
local require = require
local PZArchipelagoDatabase = module(...)

PZArchipelagoDatabase.AddContentToCall = function(_tContentToCall)
  table.insert(_tContentToCall, require("Database.ArchipelagoCareerData"))
end
