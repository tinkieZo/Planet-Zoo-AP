# Planet Zoo × Archipelago - Implementation Plan

A multiworld randomizer integration for **Planet Zoo (Challenge mode)** built on the
[Archipelago](https://archipelago.gg) framework.

## Locked decisions

1. **Bridge:** memory-hooking external client (accept patch-fragility).
2. **Game mode:** Challenge (local saves, money + research + CC all matter).
3. **Primary location source:** the research tree.
4. **Scope:** thin vertical slice first (~10 species, ~20 locations, ~20 items, no traps).

---

## The core idea

Archipelago wants a **graph of discrete locations gated by discrete items**. Planet Zoo
already ships several such graphs - the **research tree**, **conservation credits (CC)**,
**ratings/milestones**, **breeding**. We don't invent the structure (as we'd have to in a
sandbox like Arma 3); we *re-route* the structure the game already has.

The hard part is integration: Planet Zoo has **no scripting / mod API**, so all logic lives
in an external **memory-hooking client** (the Cheat-Engine-style approach the community
already uses). This is fragile across Frontier patches - we mitigate with signature/AOB
scanning instead of hardcoded addresses.

**Key architectural fact:** the client never needs the progression *logic*. Logic lives
entirely in the APWorld (generation-side). The client only needs:
- item ID → "do this in the game" (apply effect)
- game event → location ID (detect & report a check)

That makes the seam between the two work tracks narrow and stable: it's just `data.json`.

---

## The three pieces

1. **APWorld (Python)** - items, locations, regions, access rules, options, goal. Consumed
   by the AP generator. (Track B)
2. **Hooking client (Python + `pymem`)** - subclasses Archipelago's `CommonClient` (network
   layer free), reads/writes game memory to detect checks and apply received items. (Track A)
3. **In-game scenario mod (v1.0; the slice assumed "impossible")** - the 2026-06 RE overturned
   this: AP mode ships as a custom career scenario injected into `Main.ovl` (cobra-tools), with
   a Lua scenario script as the in-game brain. See "The AP career scenario" pillar below.

---

## Vertical-slice scope (agreed in Phase 0)

- **Mode:** Challenge, fixed starting save.
- **10 species:** 4 ungated starters (sphere 0) + 6 gated behind water tools / permits / conservation.
- **20 locations:** 12 research + 5 first-breed + 3 milestones.
- **20 items:** 9 progression + 5 useful + 6 filler. (Item count == location count, as AP requires.)
- **No traps** in the slice.
- **Goal:** complete the flagship research + first-breed chain (see `data.json` `slot_data.goal`).

The canonical data is in **`data.json`** - both tracks code against it. Field reference below.

---

## `data.json` contract reference

Shared, owned by both people. IDs are **stable integers, never reused**.

### `items[]`
| field | meaning |
|---|---|
| `id` | stable int, unique across items |
| `name` | display name (must match APWorld + client) |
| `classification` | `progression` \| `useful` \| `filler` |
| `effect_type` | how the **client** applies it (enum below) |
| `effect_args` | object with effect parameters |

`effect_type` enum (client-owned semantics):
- `tool_unlock` - `{tool_key}` (e.g. climate/water building tools)
- `facility_unlock` - `{facility_key}` (research centre, vet surgery)
- `species_unlock` - `{species_key}` (permit to acquire a species)
- `program_unlock` - `{program_key}` (conservation program → CC economy)
- `cash` - `{amount}`
- `cc` - `{amount}` (conservation credits)
- `staff_training` - `{levels}`
- `marketing` - `{campaign}`
- `enrichment_pack` - `{}`

### `locations[]`
| field | meaning |
|---|---|
| `id` | stable int, unique across locations |
| `name` | display name (must match APWorld + client) |
| `trigger_type` | how the **client** detects the check (enum below) |
| `trigger_args` | object with trigger parameters |

`trigger_type` enum (client-owned semantics):
- `research_complete` - `{research_key}`
- `first_breed` - `{species_key}`
- `milestone` - `{metric, threshold}` (metric ∈ `zoo_rating`, `guest_count`, `conservation_release`)

### `slot_data`
Sent by the APWorld to the client at connect:
- `goal` - `{type, args}` (slice: `type: "chain"`, complete flagship research + breed)
- `death_link` / `escape_link` - bool (off for the slice)
- `options_echo` - generation options the client may want to display

---

## Suggested logic graph (Track B owns final rules)

Mirrors the gates encoded in `data.json` (Track B owns the final access rules in the APWorld).

```
Start (sphere 0)
├── ungated species: Plains Zebra, Grey Wolf, American Bison, African Elephant
│     → acquired + bred immediately; their First-Breeding locations are reachable now
│     (welfare RESEARCH for ANY species still needs the Research Centre - see below)
│
├── [Research Centre]  → ALL per-species Research:Welfare locations (animal research, category 7)
├── [Workshop]         → both mechanic-research locations: Drink Shops + Advanced Barriers (category 3)
├── [Permit: Bengal Tiger]                                → Bengal Tiger
├── [Water Habitat Tools]                                 → Nile Hippopotamus
├── [Water Habitat Tools] + [Permit: Saltwater Crocodile] → Saltwater Crocodile
├── [Permit: Snow Leopard]                                → Snow Leopard
├── [Permit: Western Lowland Gorilla]                     → Western Lowland Gorilla
├── [Conservation Program] + [Permit: Giant Panda]        → Giant Panda (flagship)
└── [Conservation Program]                                → "First Conservation Release" milestone
```

Notes: **all research is facility-gated** - no `Research:*` location is sphere 0. The Research
Centre gates the per-species welfare research (animal, cat 7); the Workshop gates the *mechanic*
research - **both** Drink Shops **and** Advanced Barriers (cat 3), despite the latter's "Habitat"
display name. A species' **First-Breeding** location inherits that species' acquisition gate (you
can only breed what you can build); the **Zoo Rating** and **Guests** milestones are ungated economy
goals. The flagship **Giant Panda** is intentionally double-gated - its permit **plus** the
**Conservation Program** (the conservation-icon animal, and the hub of the release milestone). Every
other gated species is **permit-only** (the Lowland Gorilla's redundant Research-Centre gate was
dropped, since the Research Centre is already a de-facto early item - all welfare research needs it).
Climate-control gating was dropped - gated species use **permits** (plus water tools / conservation).
Keep rules **conservative**: players will break optimistic assumptions.

---

## Track A - Hooking client (Person 1)

**Stack:** Python + `pymem`, subclass Archipelago `CommonClient` (network layer is free).

**A1 - AP client shell (no game needed)**
- Subclass `CommonClient`; connect to a real AP server running a Track-B seed.
- Add a **manual trigger console**: type a location name → send that check; print received
  items. This stands in for the game until A2 lands and tests the full AP round-trip.

**A2 - Memory access layer (no AP needed)** - *highest-risk, start early*
- **Cheat Engine spike:** locate stable anchors for research-complete flags, species roster,
  cash, CC, and an animal-birth signal. Produce an **offset/signature table** doc.
- Implement **AOB/signature scanning** (not hardcoded addresses) + pointer-chain resolution.
- Read path: snapshot relevant memory each poll tick. Write path: grant cash/CC, set a
  research-complete flag, flip a species permit.

**A3 - Glue / state machine**
- Poll loop: diff snapshot → map events to **location IDs** (via `data.json`) → send checks; debounce.
- Apply received **item IDs** → effects (via `data.json`).
- **Idempotent re-grant:** on (re)connect replay the server's full received set without
  double-applying; track an applied-index high-water mark in a local state file.

**Track A done:** complete a research item in-game → check fires; another player's item
arrives → effect applied in-game; restart save → state re-synced correctly.

---

## Track B - APWorld / item & location graph (Person 2)

**Needs no game, no memory work** - test entirely with the AP generator + standard text client.

**B1 - Skeleton APWorld**
- Scaffold the World subclass; build `item_name_to_id` / `location_name_to_id` from `data.json`;
  declare options + game name; generate a seed without crashing.

**B2 - Regions & access rules**
- Encode the logic graph above; gate locations behind progression items; place the goal.

**B3 - Fill & validation**
- Classify/balance the item pool to exactly the location count.
- Generate many seeds; use AP reachability/`fill` checks to prove every seed is beatable.
- Emit the agreed `slot_data` at connect.

**Track B done:** repeatedly generates beatable seeds; the AP text client connects, sees
items/locations, and `!hint` resolves names.

---

## Integration milestone (both, after A3 + B3)

1. Person 2 generates a slice seed; host an AP server with it.
2. Person 1 connects the hooking client to a live Challenge save.
3. Walk the loop: complete research → check fires → server routes an item → effect applied
   in-game → reach goal → slot marked complete.

Both sides were validated against stand-ins (manual console / text client), so integration
is mostly wiring, not debugging two unknowns at once.

## Dependency summary
- **Phase 0 (`data.json`)** blocks everything - DONE (this commit).
- After Phase 0, **Track A and Track B are independent** until the integration milestone.
- Within Track A, do the **A2 Cheat-Engine spike first** - it's the make-or-break unknown.
- Track B depends on nothing from Track A.

### Track A↔B reconciliation to the FULL pool (2026-06-16)
Track B grew from the slice to the full pool (**229 items / 698 locations / 2 options**); the client
was resynced to it. `data.json` is no longer hand-maintained - **`tools/build_data_json.py`
regenerates it from the APWorld** (authoritative ids) + `research_catalog.json`, recovering each
decoupled-reward's content token by replaying the APWorld's own `convert_readable()`. The species-key
namespace is unified on the APWorld **stringid** (`pzebra`, `twolf`, `hippo`, `gpanda`, …).
- New client effect types: `research_reward {content}` (137 - the decoupled rewards, granted by
  `memory/rewards.py` flipping the content's unlocked byte - the productionised unlock-flip spike) and
  `progressive_research_reward {family}` (4). New trigger types: `first_acquire` (78, non-newborn
  insert via the birth hook) and per-species `conservation_release` (78).
- **Cross-check: 0 id/name mismatches, 0 unknown effect/trigger types; 66 client tests green** (incl. a
  synthetic-memory unit test for the grant primitive and the regenerated sync guard).
- **Detection coverage — registry attribution (DONE 2026-06-16):** the per-species *capture* campaign
  was avoidable and is now removed. The ids are session-dynamic intern indices, but the client resolves
  them live via `RegistryResolver` (the global symbol registry that drives the market), and the
  research-map handle == that symbol id. `data.json` species now carry `engine_token` (generator:
  `norm(label)` + a 5-entry alias for Track B's divergent/typo'd names), and `ResearchReader` +
  `BirthDetector` resolve any live handle → species_key through it (auto-deriving the welfare item-id
  run from the handle when not captured). So **welfare per-level (396), first-breed (78), first-acquire
  (78)** cover all species with no capture (falls back to the captured 11 if the registry is down).
  Remaining: `conservation_release` (78) needs a hook change (capture the released animal's handle);
  mechanic research (57) resolves cat-3 ids via the InternRegistry. All detection degrades safely (no
  false checks). One **live confirm** still wanted: the registry token spellings + `handle == symbol-id`
  hold across all 78 (it's proven for the slice species). Item *application* was never capture-gated.
- **Track B bugs to fix:** `Locations.py` uses hardcoded relative `open()` paths (breaks generation
  unless run from the AP parent dir - `build_data_json.py` reads the data files directly to dodge it);
  `fill_slot_data` emits only `starting_money` (no `goal`, no `num_starting_species`).

---

# v1.0 expansion proposal - locations & items

The slice proved every mechanism class we need. v1.0 is mostly **scaling proven mechanisms**,
not new research. Suggestions below are ordered by (impact × feasibility), tiered by how much
new client work each needs:

- **Tier 1** - detection/enforcement already shipped and live-validated; scaling is data work
  (`data.json` entries + species-handle/id captures).
- **Tier 2** - mechanism located (offsets/writers known) but needs wiring or a focused capture.
- **Tier 3** - new reverse-engineering; only include if v1.0 has room.

## Location categories

### L1. Full research tree (Tier 1) - the primary scaling axis
The research-items map (stride-0x58 records, status byte `+0x49 == 4` = complete) covers the
**entire** tree: ~1600 animal-welfare records (category 7) and ~117 mechanic records (category 3).
The slice only used "welfare fully complete" per species; v1.0 should emit:
- **Per-level welfare locations**: `Research Welfare - <Species> Lv1..Lv5` (record level field is
  readable). 5 locations per pooled species instead of 1 - this alone supports a 200-400 location
  seed without inventing anything.
- **All mechanic research items/tiers** (shops, facilities, barriers, enrichment, power...): each
  is a record with the same status byte. These are great mid-game filler locations and pair
  naturally with the Workshop gate.
- New `trigger_args`: `{research_key, level}` - additive, no schema break.

### L2. First Acquisition per species (Tier 1) - cheap sphere-0 breadth
The habitat-insert hook (0xC82168) already fires on **every** animal entering a habitat, with
species attribution via `entity+0x50` (research-handle namespace, auto-resolves for all species).
A non-newborn insert = acquisition. `First Acquisition - <Species>` gives one early check per
permit item received - the classic AP "item immediately pays out a check" loop. New
`trigger_type: first_acquire`. With the dormant-market stocking (see I1), the whole loop is
native: permit arrives → listing appears in the market → purchase → habitat insert → check.

### L3. First Breeding per species (Tier 1)
Already shipped and species-generic (newborn = `entity+0x3A7 == 0`). Scale from 5 species to the
whole pool; optionally add `Breed N distinct species` cumulative locations (client already
accumulates `_bred_species`).

### L4. Conservation releases (Tier 1)
The release-gate detour already counts releases. Add per-species release locations (resolve the
released animal's species the same way as births) and cumulative tiers
(`Release 1/5/10/25 animals to the wild`). This makes CC + the Conservation Program a full
mid/late-game loop instead of a single milestone.

### L5. Zoo-stat milestone ladders (Tier 1)
Cash, CC, guest count, zoo rating, park age are all restart-validated read anchors. Expand the 3
slice milestones into ladders: rating 1-5 stars, guests 250/500/1k/2.5k/5k, lifetime cash/CC
thresholds, animal count, distinct-species count, park age years (already read for re-award).
Cheap, ungated economy locations that keep every sphere non-empty.

### L6. Guest-stat & education milestones (Tier 2)
Stats-manager roots are mapped; happiness/education averages need field identification but no new
mechanism. Nice-to-have variety, not load-bearing.

### L7. Exhibit (vivarium) species - IN for v1.0
Exhibit species are first-class citizens of the research data layer: they ship the same
per-species `.animalresearchunlockssettings` files as habitat species (verified:
brazilianwanderingspider, gianttigerlandsnail, mexicanredkneetarantula...), with their own
per-level reward tokens (`<Species>EnrichmentL1..L3`). So:
- **Research locations** (Tier 1): should come free via the existing ResearchReader - the
  runtime items map's ~1600 cat-7 records very likely already include exhibit species
  (verify: find a known exhibit species handle in the map).
- **First-acquire / first-breed** (Tier 2): exhibits are a separate placement subsystem - the
  habitat-insert hook (0xC82168) does not cover them. Needs one focused capture session to find
  the exhibit-insert equivalent (same proven hook pattern; exhibit breeding is high-volume, so
  debounce per species).
- **Market delivery** (Tier 2, first RE facts 2026-06-10): the exhibit exchange manager
  (`*(park+0x1C0)`) is NOT layout-parallel to the habitat one - whitelist id sits at +0x2A0,
  mode byte at +0x304, and `SetExhibitAnimalExchangeGuaranteedSpecies` writes a single
  always-offer species slot at +0x330 (an untested, script-callable per-species lever). No
  schedule array found yet. In the AP scenario the exhibit market is natively empty and
  dormant - already the desired gated default - so the analog only matters once exhibit
  species actually enter the item pool.

## Item categories / effects

### I1. Species permits for the whole pool (Tier 1) - the backbone
The permit gate is shipped and **species-generic** (listing handles shared with research records;
no-spend capture tool exists for naming). Scale from 5 permits to the full pooled roster
(~60-100 habitat species depending on owned DLC). Two pool shapes worth offering as an option:
- **Individual permits** (max randomization, large pool), or
- **Progressive regional permits** (`Progressive Permit: Africa` x3 ...) to keep pools tight in
  small multiworlds and create AP-style "progressive item" texture.
DLC handling: the species-handle table clusters by DLC and `OwnRequiredDLCForSpecies` exists in
the binding catalog - the **generator** must take an owned-DLC option and restrict the pool; the
client should sanity-check at connect.

**Permit DELIVERY solved (2026-06-10):** in the AP career scenario the animal market is natively
dormant (its schedule entries are tag-fired and nothing ever fires the tags), so **enforcement
comes free - the market shows only what we spawn**. The client stocks it with exactly the
unlocked species: `market.ScheduleSpawner` retargets a schedule slot's species id and fires the
engine's own tag-spawn byte; the native Advance loop then generates, prices, and lists the
animal (`client._reconcile_market` polls with a 120 s per-species cooldown; live-validated,
slot re-arming proven). A permit item therefore *visibly pays out* as a native market listing;
the PermitGate stays as purchase-level belt-and-braces. Species ids resolve via the research
map (registry symbol id == research handle, verified unified namespace) - never by name
derivation. Remaining: UI-eyeball pass on a hijacked listing (price/purchase/animal validity).

### I2. Research gating, finer-grained (Tier 1)
The research-START gate (status-write hook at 0x140E461C6) currently gates whole categories
(Research Centre -> cat 7, Workshop -> cat 3). The same hook can gate **per-record-id sets**,
enabling:
- **Per-species research permits** (welfare research for a species gated separately from its
  acquisition permit), and/or
- **Progressive Research: <branch>** items unlocking mechanic-research tiers.
This is the item-side mirror of L1 and the main lever for making logic deep instead of wide.

### I3. Facility unlocks: complete the set (Tier 2)
Research Centre + Workshop are shipped (ResearchGate + native greyed-button PresenceGate). The
presence-cache fill writer (0x9E94863) is **shared by all facility component-managers** - vet
surgery / trade centre / staff facilities only need their `zoo+0x...` manager offsets enumerated
(siblings at 0x140/0x148/0x178/0x2A8/0x328, identified by build/demolish toggling). Re-adds the
Trade Centre and Vet Surgery items the slice repointed, with native UX.

### I4. Terrain & tool unlocks (Tier 1/2)
Water tools shipped via the Lua-bytecode TerrainGate. The same patch point covers the other
terrain tools (sculpt/flatten/paint) per-tool. Barrier/path/scenery unlocks are better routed
through I2 (mechanic-research records) than placement hooks - the placement executor remains
unsolved and should stay out of v1.0 scope.

### I5. Whole-feature locks via the scenario-rules byte map (Tier 2)
The rules object holds 17 one-byte feature toggles (+0x694..+0x6A4: aging, death, escapes,
global terrain, hard shelter...). Candidates as **items**: "Animal Trading", "Global Terrain
Editing". Candidates as **options**: starting a seed with QoL features off and items turning
them on. Capture mechanism exists (load-time hook); one byte write per feature.

### I6. Traps (new for v1.0) - reuse the reversible gates
The slice shipped several cleanly *reversible* locks, which is exactly what traps need:
- **Research Strike** - gate all research categories for N minutes (ResearchGate).
- **Terrain Lockout** - revoke terrain tools for N minutes (TerrainGate).
- **Audit / Fine** - cash or CC deduction (write anchors shipped).
- **Facility Inspection** - grey a facility for N minutes (PresenceGate; note unlock requires a
  cache refill, so prefer ResearchGate-based traps until refill-on-unlock is solved).
Time-boxed, no permanent damage, all on proven hooks. Trap density should be an APWorld option.

### I7. Useful/filler beyond cash & CC (Tier 2/3)
- **Marketing Campaign** (resolve the executor via the binding catalog) - useful.
- **Staff-building perk** (the keeper-training repoint target idea) - thematic useful item.
- **Enrichment Pack** - grant/unlock enrichment research records directly (I2 machinery).
Keep cash/CC as the filler floor; they're cheap and always welcome.

## Reward decoupling & native build-menu locks - the data layer (new v1.0 pillar)

The classic AP pattern: checking a location does NOT give the vanilla reward - the reward enters
the multiworld item pool instead. For Planet Zoo this means: completing a research is only a
*check*; the things that research normally unlocks (enrichment items, shop types, barriers,
education/breeding bonuses...) arrive as *items*. And the same unlock machinery, pushed further,
lets us start the save with parts of the build menu locked - the "stronger Workshop/Research
Centre gate".

**Status: the data layer is CRACKED (2026-06-10).** The entire research/unlock economy is
data-driven and lives in `GameMain/Main.ovl` (Oodle .ovl), which cobra-tools parses AND can
inject into (the shipped TerrainGate already patches Lua inside an .ovl - same pipeline, proven):

- `<species>.animalresearchunlockssettings` (76 in Main.ovl, exhibit species included) - the
  per-species research tree: each `ResearchLevel` lists `next_levels` + `children` =
  **the vanilla rewards by name**. Census: 738 enrichment-item grants (`EN_*`), 380 Zoopedia,
  228 Education, 175 Breeding, 106 Supplement, 95 exhibit-enrichment tokens = **~1000 nameable
  vanilla rewards**.
- `default_off_<branch>.mechanicresearchsettings` / `default_on_<branch>` (93 files, 15 branches:
  drink/food/souvenir shops, barriers, habitats, shelters, power, transport, staff facilities,
  5 theme sets; 404 research items total) - per-item flags `is_entry_level`, `is_enabled`,
  `is_completed`. The `_off`/`_on` variants ARE the game's own "starts locked" vs "starts
  unlocked" switch per branch.
- `default.animalresearchstartunlockedsettings` (844 lines) vs `scenario_01...` (76 lines) -
  **the career-zoo restricted start is literally a smaller start-unlock file.** This is the
  native mechanism for "basic content starts locked".

**Groundwork DONE (tools in repo):**
- Extraction: `cobra-tools ovl_tool_cmd.py extract --type .animalresearchunlockssettings ...`
  (validated against the live install at `K:\SteamLibrary\steamapps\common\Planet Zoo`).
- `tools/research_catalog.py` - parses the extracted XML into `tools/research_catalog.json`
  (species trees + rewards, mechanic branches + flags, start-unlock sets). This catalog is the
  generation-side source of truth for the v1.0 item/location pool.

**Architecture (three layers):**
1. **Seed-time data mod (generation/install step):** patch Main.ovl (cobra-tools inject) -
   strip `children` rewards from research levels (research completes but awards nothing) and/or
   swap mechanic branches to their `_off` topology + shrink the start-unlock set (career-style
   initial locks). One patched ovl per seed-options profile; keep a pristine backup + re-patch
   after game updates (selfcheck should detect version drift).
2. **Check detection (shipped):** ResearchReader status bytes - unchanged.
3. **Runtime grant (SOLVED 2026-06-10 - tools/unlock_flip_test.py):** when the AP item
   "EN_Grazing_Ball" arrives, the client writes the unlocked byte (+0x12) on the content's
   record in the unlockables map at research_system+0x148 (content intern id key at +8,
   stride 0x14, occupancy bitmap before records; name->id via read-only enumeration of the
   intern registry - names are interned lowercased). VERIFIED in-game: flipped
   EN_Grazing_Ball appeared in the build menu and was placeable - no event broadcast needed.
   Per-type residue: type 1 enrichment = flag only (done); types 0/2/3 (supplements/
   breeding/education) have small bookkeeping side-effects to mirror (rs+0x210 count map /
   max-level float rs+0x1E8 / rs+0x52C counter); type 4 zoopedia needs the script dispatch
   (filler tier, may stay cosmetic). Decompiles archived in tools/_decomp/; headless-Ghidra
   dump flow in tools/ghidra_scripts/DumpDecomp.java (close the Ghidra GUI first).
4. **Mechanic research decoupling (PROVEN in-game 2026-06-10, tools/_ovl_patch/ spikes):**
   lock = seed-time ovl patch moving AP-gated mechanic items into the `noneresearchable`
   pool (vanilla-legal; CRASH RULE: every VISIBLE branch must keep >=1 is_enabled=1 entry
   item, and remove dangling next_research pointers when moving). Moved items stay in the
   runtime items map; grant = client writes status 4, resolving the record BY NAME via the
   intern-id bridge at items-map record+0x08 (ids proved name-stable across moves, but name
   resolution is the contract). Verified live: moved Pipshot Smoothies = unresearchable +
   shop locked; status write -> shop buildable. cobra-tools inject round-trip is faithful
   (pristine-reinject ran clean); backup flow: Main.ovl.apbak next to the install.
   NOTE: base facilities (workshop/research centre/trade centre) are NOT research-gated in
   data and absent from the unlockables map - they stay on the shipped presence gate.

**Risks / constraints:**
- Game updates overwrite Main.ovl -> repatch flow + AOB-style version checks (selfcheck.py
  already owns this class of problem).
- Patched-data + memory-hook interactions must be re-validated (e.g. ResearchReader semantics
  if a branch starts `_off`).
- Zoopedia/education/breeding/supplement rewards may be lower-value as items - candidates for
  filler tiers rather than progression.

## The AP career scenario - the shipping vehicle (new v1.0 pillar)

**Status: ARCHITECTURE SHIPPED (Design C). v17 boot-validated in-game; v18 built+verified
2026-06-10 (adds the AP-session marker), awaiting boot-validation.** AP mode is a custom
career scenario injected into `Main.ovl` with cobra-tools - the same inject pipeline the
data-layer pillar uses. Authored sources: `tools/_ovl_patch/_apshell_v18/` (careerdata entry,
script-table hijack, scenario script, park settings, objectives). Build = inject over the
vanilla backup (~4 min); deploy = copy over `Content0\Main.ovl` (vanilla kept as
`Main.ovl.apbak`).

What ships in the ovl shell:
- A career-menu entry **"ARCHIPELAGO"** whose careerdata points at the game's own shipped
  `Scenario_01_Empty.bin` (empty-terrain park, permissive rules - zero bin patching). The
  engine **natively merges** our `Scenario_AP_ParkSettings` + `Scenario_AP_Objectives` at
  load. All 12 `Scenario_NN_Empty` bins exist, so **map/biome can be a per-seed option**.
- A script-table hijack in `scenarioscriptutils.lua` that activates **`Scenario_AP_Script`**
  (the in-game brain) for that world only; saved AP parks re-resolve it via the script
  whitelist. Boot-validated: menu entry → empty park boots → script runs → settings and
  objectives merge → all build menus open, staff hireable, terrain tools enabled.

Why this is the v1.0 frame - it moved three pillars from "needs RE" to shipped/closed:
1. **Species-permit delivery** - the scenario market is natively dormant; the client stocks
   it (see I1). No market RE remains on the v1.0 critical path.
2. **AP-session detection (SOLVED 2026-06-10)** - `Scenario_AP_Script.Init` plants the park
   name `"ARCHIPELAGO ZOO"` (native storage at park-info+0x1E8, the same vtable-anchored
   class as the park-age anchor; persists in saves). The client treats *marker ∧
   exchange-mode==0* as "AP session" and the **entire poll tick idles in foreign parks**
   (`memory/session.py`; `PZAP_NO_SESSION_GATE=1` escape hatch; selfcheck 14/14 with the new
   `ap_session` check). The client is now safe to leave running while playing vanilla.
   (The scenario *code* itself proved unanchorable - no registrar/reflection binding for
   `GetScenarioCode`, unknown hash dispatch; the park-name marker is the keeper.)
3. **Native objectives & locked-menu UX (open, Tier 2)** - objectives merge natively today
   (guest-count placeholder); real AP objectives would surface location checks in native UI
   (`objectives.*` catalog: researchitem / facilitybuild / animalbreeding / ...). Script
   triggers can also fire `browserItemsToHide/Show` and the engine's own tutorial-lock
   surface (`SetItemsTutorialLocked`) - the native face for the I2 / data-layer locks.

Client integration inside the scenario is **verified live**: selfcheck green in the AP park
(all 5 hooks, both roots, all anchors, research map, terrain bytecode), fresh-save re-award
working via the park-age anchor on the same park-info class, market reconciler wired, 41
tests green. Open items: v18 boot-validation, real AP objectives, locked-menu UX,
exhibit-market analog (L7), end-to-end client test against a real AP server in-scenario.

### Deployment / releases - one exe, in-client installer (built 2026-06-10)

Decisions (locked): releases are a **single exe** (the client, standard AP Kivy GUI), and the
ovl management lives **in the client as explicit actions**, decoupled from the connect
lifecycle - install converges state once; nothing swaps per-session. The shell is inert
outside AP (the hijack keys on our careerdata's scenario code), so there is nothing to revert
for vanilla play; `/pz_restore` exists as a courtesy.

- **No Frontier bytes in the release.** Measured: cobra-tools inject repacks the whole ovl
  (16 bytes common prefix vs vanilla), so binary deltas are ~full-size and copyright-laden.
  Instead the release ships only the authored Lua (`pz_ap_client/ovl_src/` - now the canonical
  home; `tools/_ovl_patch/` stays a workspace) plus a trimmed **vendored cobra-tools** (like
  the vendored Archipelago tree), and `/pz_install` builds the patched ovl **on the user's
  machine from their own vanilla** (~4 min, one-time per game version).
- **No Frontier content in the SOURCES either (v19 rewrite, 2026-06-11).** The shell's two
  derived files (vanilla career-table copy, Scenario_01 park-settings copy) were eliminated
  via engine extension points found in gamescript decompiles (`Database.Main`,
  `Database.MainCareerData`, `ScenarioManager`):
  1. **Additive career data via a standalone content pack** - the engine discovers content
     packs by Manifest.xml folder scan and tryrequires `Database.<PackName>LuaDatabase` per
     pack (`Main.InitContentToCall` - the exact mechanism every DLC uses, verified against
     Content1's decompile). The shell now ships as `ovldata\PZArchipelago\` (~6 KB Main.ovl,
     built by cobra `new` in seconds, byte-identical extract round-trip verified): hot-plug
     hook + additive careerdata (scenario codes are unique-asserted; sets MERGE by code via
     shallow `table.merge`, so extending the first career set restates only 4 park-code
     identifiers) + scenario script + settings.
  2. **Minimal park settings** - `ScenarioManager.Init` defaults are permissive
     (bDisable*=false, bCanHireNewStaff=true, multipliers=1) and `WorldLoad` nil-guards
     numerics/tables while assigning booleans unguarded; the sub-managers (marketing,
     demographics, park rating) skip absent sub-tables. The module now states only the
     true-booleans we rely on, our economy choices, and the two fields
     `MergeParkSettingData` reads unguarded (`nRefundMultiplier`/`nTrackRefundMultiplier`).
  Only `scenarioscriptutils.lua` (hand-rewritten, committable) still requires the Content0
  inject - it must REPLACE the vanilla module that builds the script-type table. The
  installer builds/deploys both artifacts and `/pz_restore` also removes the pack folder.
  PENDING: v19 boot validation (career entry appears via set merge, scenario boots, marker
  plants, selfcheck green in-park).
- **`pz_ap_client/ovl.py`**: Steam discovery (registry + libraryfolders.vdf), a hash/stamp
  state machine (`vanilla / installed / stale / game-updated / ambiguous` - stale = bundled
  sources newer than the deployed shell; game-updated = Steam patched/verified over us),
  backup hygiene (never overwrite a backup that doesn't match the live vanilla), and the
  inject as a crash-isolated subprocess (frozen exe re-invokes itself with
  `--run-ovl-inject`). 17 offline tests cover the state machine end-to-end.
- **Client UX**: startup logs the mod status; `/pz_mod` `/pz_install` `/pz_restore`
  `/pz_launch` (Steam url with `-skipScenarioIntro` - closes that polish item). Install runs
  in a worker thread; refuses while the game runs.
- **Per-seed stays runtime**: the ovl remains static and seed-independent - all randomization
  is client-side reconcile (gates, market, future slot-data-driven start tweaks). For the
  v1.0 data-layer locks, prefer scenario-scoped settings files and runtime lock-writes over
  per-seed ovl patches so this stays true.

## Goals & options (Track B)
- **Goal options**: flagship chain (current), star-rating target, N conservation releases,
  breed-N-distinct-species, mechanic-research completion %.
- **Pool options**: species count + DLC filter, individual vs progressive permits, per-level vs
  per-species research locations, milestone-ladder density, trap density.
- **World options**: starting map/biome (any of the 12 `Scenario_NN_Empty` bins - see the
  scenario pillar), starting build-menu lock profile (career-style `_off` topology + shrunk
  start-unlock set from the data layer).
- **DeathLink/EscapeLink** (already reserved in `slot_data`): *sending* needs a death/escape
  detection hook (Tier 3 RE); *receiving* should be a trap-style effect (cash penalty / research
  strike), never killing animals. Defer sending to v1.1 if the hook hunt drags.

## Suggested v1.0 shape
~30-50 species (habitat + exhibit, DLC-filtered), permits individual or progressive-regional;
locations = per-level welfare research + mechanic research + first-acquire + first-breed +
release tiers + milestone ladders (roughly 250-450); items = permits + decoupled vanilla rewards
(enrichment items, shop types, barriers/shelters, breeding/education bonuses from the research
catalog) + research/facility/tool unlocks + traps + cash/CC filler to match. The reward-decoupling
data layer turns the item pool from "mostly filler cash" into hundreds of real, named game
content items - that is the single biggest v1.0 quality lever. Everything in Tier 1 first; the
runtime-grant RE (see data-layer pillar) - v1.0's A2-equivalent make-or-break - was spiked and
PROVEN 2026-06-10, so the reward-decoupling pillar has no remaining critical-path unknowns.

v1.0 ships inside the **AP career scenario** (pillar above). As of 2026-06-10 all three
mechanism-grade unknowns are closed: reward decoupling + runtime grant (data layer), species
permit delivery (dormant market + ScheduleSpawner), and AP-session gating of the client. What
remains is UX- and data-grade work (real objectives, locked-menu UX, pool data entry, APWorld
rules), plus the v18 boot-validation pass.

## v1.0 logic graph (proposed)

The slice graph above is literal per-species; v1.0 is **templated** - one chain stamped out per
pooled species, plus the global gates. Track B owns the final rules; keep them conservative.

```
Start (sphere 0) - AP career scenario: empty park, dormant markets,
│                  build menu (partially) locked per seed options
│
├── Milestone ladders (ungated economy locations):
│     zoo rating 1..5★ · guests 250/500/1k/2.5k/5k · lifetime cash/CC
│     thresholds · animal count · distinct-species count · park age years
│
├── PER SPECIES S in pool (~30-50, stamped per species):
│   [Permit: S]   (individual, or folded into [Progressive Permit: <region>])
│   (+ [Water Habitat Tools] if S is aquatic)
│     → native market listing appears (client stocks the dormant schedule)
│     → First Acquisition - S
│     → First Breeding - S
│     → with [Research Centre] (+ optionally [Research Permit: S], see I2):
│          → Research Welfare - S Lv1..Lv5   (5 locations)
│              ↳ vanilla rewards stripped at seed time (ovl patch);
│                S's enrichment/education/breeding content returns as AP items
│     → with [Conservation Program]:
│          → Conservation Release - S        (inherits S's gates)
│
├── [Workshop] (+ [Progressive Research: <branch>] per branch, ~15 branches)
│     → Research Mechanic - <item> locations (~117 records)
│         ↳ shop/barrier/shelter/power content returns as AP items
│           (decoupled via the `_off` topology + runtime status-write grant)
│
├── [Vet Surgery] · [Trade Centre]  - facility items (presence gate);
│     QoL-grade: gate nothing in hard logic (or only soft milestone locations)
│
├── [Terrain Tools: sculpt/flatten/paint] - QoL items, gate nothing (TerrainGate)
│
├── [Conservation Program]
│     → First Conservation Release
│     → Release 1/5/10/25 cumulative ladder
│     → opens the CC economy: CC-priced content stays logically behind a CC source
│
└── Goal (per-seed option): flagship chain | star-rating target |
      N conservation releases | breed-N-distinct-species | mechanic-research %
```

Rules of thumb the graph encodes:
- **Everything downstream of a species inherits its acquisition gate** (permit + water tools
  where applicable): acquire, breed, welfare research, release. Welfare research additionally
  needs the Research Centre; releases additionally need the Conservation Program.
- **Decoupled vanilla rewards are items, never logic gates.** Missing enrichment is a welfare
  penalty, not a hard block - logic must not assume any of the ~1000 reward items. Same for
  traps (non-logical by definition) and cash/CC filler.
- **Research Centre and Workshop remain de-facto early items** - virtually every research
  location sits behind one of them (the v1.0 version of the slice's gorilla-gate note).
- **Exhibit species join the per-species template only after the L7 work lands** (exhibit
  insert hook + market analog); their research locations come free via the existing
  ResearchReader either way.
- Milestone ladders keep every sphere non-empty; goal options pick which subgraph must be
  completable, and the generator's reachability fill proves each seed beatable as usual.
