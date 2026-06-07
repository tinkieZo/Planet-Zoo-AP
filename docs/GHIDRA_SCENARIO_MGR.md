# Ghidra handoff — pin the IScenarioManager terrain-gate methods

Goal: find the **scenario field(s)** read by the per-tool greying predicates, so the client can write
them to grey/un-grey terrain tools semi-live (effect applies on the next terrain-mode entry).

## What's already proven (decompiled `TerrainEditUIMode.lua` via tools/luaparse.py)

The terrain menu's greying is computed on EVERY terrain-mode entry by Lua `main.1` → `main.2`:

```
main.1:  scenarioMgr = RequestInterface("Interfaces.IScenarioManager")
         self.bTerrainEditDisabled = scenarioMgr:IsTerrainEditDisabled()
         self.bLakeEditDisabled    = scenarioMgr:IsRemoveLakesDisabled() AND scenarioMgr:IsAddLakesDisabled()
         self.bIsScenario          = scenarioMgr:IsScenarioMode()
main.2:  deformation(sculpt).enabled = not bTerrainEditDisabled
         shapestamp(stamp).enabled   = not bTerrainEditDisabled   (only present if global bEnableShapeStamps)
         painting(paint)             = ALWAYS enabled (no gate)
         water.enabled               = not bLakeEditDisabled
         (greyed item also gets toolTip [HUD_DisabledByScenario])
```

So to gate **water_tools (item 1003)** = make `IsRemoveLakesDisabled` AND `IsAddLakesDisabled` both
return true. To gate sculpt/stamp = make `IsTerrainEditDisabled` return true. (paint can't be greyed.)

NOTE: `rules+0x6a1` (the SetEnableTerrain byte / sandbox "Enable Terrain") is a DIFFERENT, GLOBAL flag —
live-tested 2026-06-04: forcing it 0 did NOT change the menu greying. The per-tool gate is these methods.

## The native dispatch I pinned (menu-entry getarg trace, tools/menu_entry_trace.py)

The `Is*Disabled` methods are **C++ virtual methods** called through reflection wrappers. During a
terrain-mode entry, between `RequestInterface` and `GetTerrainMenuConfig`, these wrapper functions fired
(getarg-caller RVAs, module base 0x140000000):

- `0x14041E020`  — small wrapper (getarg(1) → unwrap → `call [rdi+8].vtable[0]` chain)
- `0x14041DF11`  — wrapper (in fn ~0x14041DE_)
- `0x14041E932`  — wrapper that does the actual virtual dispatch:
    `0x14041EA3C: call [rax]`        (get scenario-manager sub-object → rbx)
    `0x14041EA50: call [rax+0x190]`
    `0x14041EA61: call [rax+0x190]`  ← **C++ method @ vtable slot 0x190** (index 0x190/8 = 50)

## Ghidra tasks

1. Open `0x14041E932` (and `0x14041E020`, `0x14041DF11`). These are the reflection wrappers the menu-build
   used for the 4 IScenarioManager calls. Identify which **vtable slot** each dispatches to
   (`call [reg+0xNNN]`). `0x14041E932` uses slot **0x190**.
2. Determine the object type / vtable. `rbx` at `0x14041EA61` is the scenario-manager sub-object; its
   vtable = `*rbx`. Find that vtable in `.rdata` (array of fn ptrs); slot 0x190 is one `Is*Disabled` method.
3. For `IsTerrainEditDisabled`, `IsRemoveLakesDisabled`, `IsAddLakesDisabled`: open each C++ method. They
   should be tiny — `movzx eax, byte [this+OFF]` (maybe XOR/CMP) `; ret`. **Report (method addr, field OFF)**
   for each, and which register/offset chain reaches `this` from the scenario object.
4. Bonus: find what WRITES `[this+OFF]` at scenario load (it's set from parksettings `bEnableTerrain` /
   `bDisableAddLakes` / `bDisableRemoveLakes`) — confirms the field + gives a second lever.

## After Ghidra

Report `(method addr, this+OFF)` per method. I'll: resolve the scenario-manager object live, write the
field(s), and you re-enter terrain mode to confirm the tool greys/un-greys. Then wire it into the client
(a TerrainGate that flips the field from AP item state, like PresenceGate/PermitGate).

OPTIONAL ACCELERATOR (safe, read-only): I can hook `0x14041EA61` for ONE terrain-mode entry to capture the
exact resolved C++ method address + the live scenario-object pointer — turning task 1-2 into "just open
this address." Say the word and I'll grab it; otherwise this is pure-Ghidra as you chose.
