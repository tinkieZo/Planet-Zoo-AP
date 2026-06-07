# A2 — Birth-hook RE handoff (Ghidra)

> Goal: find a **stable code site** the client can AOB-signature and detour to
> detect **animal births with species attribution**. The injection/detour
> capability is already built and proven (`pz_ap_client/memory/hook.py`,
> `tools/inject_poc.py`, `tools/hook_poc.py`); this is the one missing input.

PlanetZoo.exe preferred image base is **`0x140000000`**, so the runtime addresses
below match Ghidra's addresses directly (RVA = addr - `0x140000000`).

## Why RE (not scanning)

The species roster has **no restart-stable data anchor** (0/85 pointer chains
survive restart; 26 birth-confirmed objects share no vtable) and value-scanned
count fields are **unreliable proxies** (one read `10` while the live warthog
count was `8`; CE "find what writes" on it caught nothing → a derived/cache cell).
So we hook **code**, not data. Code RVAs are stable across restarts (same exe
version); re-derive the AOB after a Frontier patch.

## Confirmed roster layout (per-species counts)

One roster object holds every species' count at fixed offsets, **0x100 apart**,
count at `record+0x30`:
- zebra count at `roster_obj + 0x630`
- warthog count at `roster_obj + 0x530` (Δ 0x100 below zebra)
A **live warthog birth bumped its count in-place** (3→4→5→6), so a birth writes
the species count at its `+offset`. `species_birth[plains_zebra] = 0x630` is the
client-side offset already recorded in `anchors.json`.

## Entry points found (start here in Ghidra)

| What | Address | Notes |
|---|---|---|
| Species name string table | `0x1426C1280` (`PlainsZebra`), `0x1426C133D` (`Panda`) | contiguous content-name table |
| Species **name-dispatch** fn | `~0x140E66500` | `lea rdx,[name]; mov rcx,rbx; call strcmp; test al; jne handler` repeated per species |
| match handler target | `0x140E66862` | per-species branch in the dispatch |
| strcmp helper | `0x140A4E600` | returns al = match |
| per-entity-count **read** template | `0x14640123B` | `mov rdx,[rcx+0x830]` (guest count) — shows how the game reads a per-entity stat |
| per-entity-count **write** template | `0x140B84A62` / `0x140B8500E` | `add/sub [r13+0x830],reg` (guest count up/down) |

## Suggested Ghidra workflow

1. Load `PlanetZoo.exe` (auto-analyze; base stays `0x140000000`).
2. Go to `0x1426C1280`, find xrefs to the species name strings → the species/
   content **definition** code; recover the species **record/index** structure.
3. Decompile the name-dispatch at `0x140E66500` and its caller — identify what
   `rbx` is and what the handler at `0x140E66862` does (likely builds/looks up a
   species entry). This connects a **species_key → roster slot/index**.
4. Find the **AddAnimal / OnBirth / population++** function: xref the roster
   write, or search for the `[reg + 0x30]` increment within a 0x100-strided
   record; cross-check against the guest-count write template
   (`add [r13+0x830],reg`) which is the analogous per-entity-count update.
5. Pick the **hook site**: the instruction that increments a species count on
   birth (ideal — fires once per birth, species in a register/offset), OR an
   instruction that loads the roster base (so the detour can capture the live
   roster pointer for the client to poll).

## What to extract (hand back these)

- **AOB signature** of the hook site (≥12–16 bytes, operands wildcarded), unique
  in the module — verify with `tools/disasm.py` or Ghidra search.
- **Original instruction** bytes + length (for the trampoline + restore).
- **Species attribution**: which register/offset identifies the species at the
  hook (e.g. the `+offset` written, or an index in a register) → map to
  `species_key` via the offset map / dispatch.

## Plugging into the client (`pz_ap_client/memory/hook.py`)

```py
hm = HookManager(scanner)
site = scanner.aob_scan(SIGNATURE)            # resolve the AOB at runtime
orig = scanner.read_bytes(site, INSTR_LEN)    # the bytes we'll replace + restore
hm.install("birth", site, orig, make_birth_trampoline)  # detour -> scratch region
# each tick: read hm.scratch("birth"), decode (species_off -> species_key), fire first_breed
hm.restore_all()                               # on disconnect / finally
```
`make_birth_trampoline(region, scratch, resume)` returns trampoline bytes that
**record the species event** into `scratch` (extend `trampoline_count_hits` —
instead of a blind `inc`, write the species offset/id), run `orig`, and `jmp
resume`. The same pattern applies to **permits** (hook the purchasable/market
check) once that site is found.

## ★★ CURRENT STATUS (2026-06-01, supersedes the spec below) ★★

The `0x1407F5404` give-birth hook below was **DISPROVEN live** (installed cleanly,
bred a real calf, counter stayed 0 — that call is not on the birth path). What we
have now, **validated live**:

- **Robust birth SIGNAL — SOLVED.** Software-detour at **`0x140C82168`** (RVA
  `0xC82168`), the rejoin of the per-species container insert (`mov rdi,[rbp+0xd8]`,
  7B). Fires on every animal entering a habitat (births immediately, buys after
  quarantine), idle rate 0, crash-proof, address-independent. `pz_ap_client/memory/hook.py`
  → `make_insert_instrument` / `read_insert_events`; harness `tools/insert_hook.py`.
- **SPECIES attribution — SOLVED.** At the hook `rbx` = per-species container;
  **`[rbx+0x08]` = stable species id** (zebra `0x1B8E`, warthog `0x1BF1`), confirmed
  identical across a zebra buy and birth. `[rbx+0x10]` = population count, `[rbx+0]`
  = ptr into a species-registry array, `[rbx+0x18]` = handle data array, `[rbx+0x20]`
  = capacity. The inserted element (`rsi`) is an **entity HANDLE** (not a pointer).
- **SOLVED: birth-vs-buy** (no Ghidra needed in the end — replicated the game's own
  resolver from the two pasted functions `FUN_146EC8630` + hash-map `FUN_1444F29B0`).
  At the hook **`r13` = the ZOO** (`param_1`; prologue `mov r13,rcx` @`0x140C820B1`),
  NOT the manager. Resolution: `manager = *(zoo+0x2F8)`; hash-map at `manager+0xB90`
  (`+0x10` cap, `+0x18` buckets; open-addressing probe — see `animals.py`) maps the
  handle → index; `entity = *(manager+0xC20) + index*0x3F0`. The entity's **life-stage
  byte `[entity+0x3A7]` == 0 ⇒ NEWBORN (a BIRTH)**; buys are stage 1+. Species id at
  `[entity+0x54]` (or the container `[rbx+8]`). VALIDATED live 16/16 (the only stage-0
  animals were exactly the observed births; all buys stage 1) and end-to-end (warthog
  triplets → 3 `first_breed: common_warthog`). Implemented in
  `pz_ap_client/memory/animals.py` (`AnimalResolver`) + `births.py` (`BirthDetector`),
  wired into `triggers.py::_poll_first_breed`. **first_breed is DONE.**

### ★ GHIDRA ASKS (this is the static-RE piece — your tool) — any ONE unblocks it:
1. **(preferred) Birth-specific spawn site.** In `FUN_1407F4EA0` (gestation) or its
   callees, find the EXACT instruction that *creates the offspring entity* on a
   completed birth (NOT `0x1407F5404` — that didn't fire). Hooking it = birth-only,
   sidesteps buy-vs-birth entirely. Give: address, the instruction, and which
   register/field identifies the species (or the new entity handle).
2. **Animal-entity age/newborn field.** In `FUN_1407F4EA0`, `param_1` (r15) is the
   animal entity (gestation float `[r15+0x31c]`, birth flags `[r15+0x3bd]`/`[r15+0x3c1]`).
   Identify the **age** field and/or a **"born in zoo"/juvenile** boolean in that
   struct (offset). A newborn = age≈0.
3. **Handle→entity resolver.** The function mapping an entity handle (the value in
   the species container's `+0x18` data array) → the animal-entity pointer (the
   `param_1`-type object), likely via `DAT_14298ae00`. Give its address + the
   index/lookup algorithm so the client can resolve a handle at the insert hook.

With (1) we just hook that site. With (2)+(3) the client resolves the inserted
handle → entity → age and treats age≈0 as a birth. Either finishes `first_breed`.

**Concrete Ghidra navigation targets (the captured BIRTH call path to the insert):**
the insert hook recorded the birth caller chain as return addresses
`0x1401B566A <- 0x147249DE3 <- 0x1444F2ACE` (innermost→outermost). The functions
*containing* these addresses are the birth-handling code. Decompile each (go to the
address in Ghidra, F: function start) and look for: the offspring/entity creation
call, and where this path differs from a market purchase (buys captured longer
chains with extra frames `0x72BCBDB`/`0x79E407`/`0x93D55B4` or `0x1041A6F1`). The
function at `0x1444F2ACE` (outermost, common to all adds) likely dispatches; the
birth-specific spawn is on the branch that reaches `0x147249DE3` directly.
→ **Paste those decompiles and I'll pinpoint the birth-only hook site.**

UPDATE: those 3 caller-chain addrs turned out to be GENERIC UTILITIES (hash-map
find `FUN_1444F29B0`, hash-map resize `FUN_147249C30`, atomic refcount-release
`FUN_1401B55D0`) — the stack-scan caught incidental container/refcount frames, NOT
birth-vs-buy business logic. **Caller-chain classification (Path B) is dead.**

Static call-tree from the insert (no game, from the dump):
- insert/rejoin site `0x140C82168` is inside fn **`0x140C82080`** (RVA `0xC82080`)
  = "add animal to species container + register" (calls `FUN_1410BA010` with
  r8b=6 then 7 after the insert).
- `0x140C82080` has exactly **ONE** direct caller: `0x14942305D`, inside fn
  **`0x149422DFD`** (RVA `0x9422DFD`).
- `0x149422DFD` has **ZERO** direct E8 callers and is **not in any dump vtable slot**
  → it's invoked via **virtual dispatch** (my dump tools can't follow it).

★ GHIDRA ASK (precise, uses Ghidra's xref/vtable resolution my tools lack):
  Go to **`0x149422DFD`** (and/or `0x140C82080`), use **References ▸ Find References
  To** (Ghidra resolves vtable/virtual refs). Identify its callers and which is the
  **birth** path vs the **market-buy** path. The birth-path caller (or the offspring-
  creation it does just before the add) is our birth-only hook site. Paste those
  callers' decompiles. (Equivalently, ask #2/#3 above: the animal-entity age/newborn
  field + the handle→entity resolver — either finishes it.)

## ★ BIRTH HOOK — OLD SPEC (DISPROVEN — kept for history)

Fully reverse-engineered in `FUN_1407F4EA0` (the gestation/birth tick):
- **HOOK SITE: `0x1407F5404`** (RVA `0x7F5404`), instruction `call 0x140C91A70`
  (`E8` rel32, **5 bytes** — fits a 5-byte jmp/call detour with NO padding). It is
  the give-birth/create-offspring call (`thunk → FUN_1494635E0`), reached only via
  the gestation-complete branch (`[r15+0x3bd]=0` at `0x1407F537E`), so it fires
  **exactly once per birth**.
- **SPECIES INDEX = `r14w`** (ushort), set at `0x1407F4F94`
  (`movzx r14d, word ptr [rdx+rax+0x54]`) and held in callee-saved `r14` through
  the function — live at the hook. Maps to a species via the per-species table
  (base `*(*(zoo+0x10)+0x98)`, **stride `0xb88`**).
- `r15` = the breeding animal object; mate at `[r15+0x2d8]`.
- **AOB SIGNATURE (locked, UNIQUE in module — 1 occurrence):**
  `4C 8B C3 48 8D 54 24 40 49 8B CB E8 ?? ?? ?? ??`
  = `mov r8,rbx` / `lea rdx,[rsp+0x40]` / `mov rcx,r11` / `call <rel32>`.
  **Hook site = match + 11** (the `E8` byte). Don't hardcode the give-birth
  address — compute it from the live rel32: `target = site + 5 + int32(orig[1:5])`
  (was `0x140C91A70`; recomputing survives a callee relocation).

**Detour (it's a CALL, so tail-call style):** because the hooked instruction is a
*relative* `call rel32`, its bytes can't be relocated by copying — the trampoline
instead re-issues the call to the same **absolute** target. Sequence: record
`r14w` into a scratch ring-buffer + bump a birth counter (preserve ALL give-birth
arg regs rcx/rdx/r8/r9 + stack; use only rax/r11 as scratch with push/pop) →
`mov rax,give_birth; call rax` (real give-birth runs with original args + stack
alignment, returns into the trampoline) → `mov rax,0x1407F5409; jmp rax` (resume).
Client polls the counter each tick, reads `r14w` → `species_key` and fires
`first_breed`. Build the `species_index → species_key` map once (read names from
the 0xb88-stride table, or correlate by breeding a known species).

**★ IMPLEMENTED + offline-verified (game closed):**
- `pz_ap_client/memory/hook.py` → `make_birth_trampoline(region, scratch, resume,
  give_birth_target)` (67-byte trampoline, capstone-validated) + `read_birth_events`
  (ring drain w/ overflow handling, unit-tested) + `BIRTH_RING`/`BIRTH_RING_OFF`.
  Also fixed `restore_all` dict-mutation-while-iterating bug.
- `tools/birth_hook.py` → resolves the signature, computes give-birth target from
  the live rel32, `HookManager.install("birth", …)`, polls + prints each birth,
  `restore_all()` in `finally`.
- **NEXT (game running):** `python -m tools.birth_hook`, breed a Plains Zebra,
  confirm it prints one `BIRTH: species_index=N` per calf; record `N` →
  `plains_zebra` in `SPECIES_INDEX`. Then correlate warthog etc.

## RE PROGRESS (live leads)

- **Birth-progression function: `0x1407F4EA0`** (RVA `0x7F4EA0`). Ticks pregnancy
  toward birth: compares a gestation float at `[r15+0x31c]` to a threshold, fires
  `"BirthImminent"` (string `0x14268E0F0`) via `call 0x1407E4660` (name-keyed
  notification), writes birth-state at `[r15+0x3b4]`/`[r15+0x3bd]`. **`r15` = the
  pregnancy/animal object.** THE HOOK SITE is the *gestation-complete* branch in
  here (where the baby is created / population++). NEXT: decompile `0x1407F4EA0`
  in Ghidra, find that branch + the call that spawns the offspring, and get the
  **species** from the animal object (`r15` + a species field/hash).
- **Birth/pregnancy strings** (entry points): `Birth` `0x14268AA54`,
  `BirthImminent` `0x14268E0F0`, `Birth_Task` `0x1426A6574`, `GestationPeriod`
  `0x142687EB2`, `OffspringPerMating` `0x142687E7A`, `NewbornJuvenileProp`
  `0x14268C5BA`, `AddAnimalToExhibit` `0x142662DA0`.
- **Gotcha — hashed tokens:** standalone `Birth`/`Birth_Task` have NO pointer
  xrefs — Frontier's Cobra engine looks many event/property tokens up by
  STRING HASH, so string-pointer xrefs miss those handlers. `BirthImminent` *is*
  pointer-referenced (that's how we reached `0x1407F4EA0`). For hashed tokens,
  trace via the function we already have, not via string xref.
- **Entity pool `DAT_14298ae00`** (generational handle table): `+0x30` array top,
  `+0x10` element stride, `+0x48` count, validity/gen bit `& 0xFFFFFFFFFFFFFFFE`;
  indexed by a handle. Likely the animal/entity store — the species "roster" is
  probably derived from iterating this, which is why no static count anchor exists.
- The `PlainsZebra` xref led to the **appearance/colour-morph** string builder
  (`FUN_140E65DE0`) — a detour, not births. Don't chase species-name xrefs for births.

## ★ PERMITS (species_unlock) — FULLY RE'd (2026-06-01)

Market = "Animal Exchange" / "Trade Centre". The whole buy path + a memory-enforced
gate are reverse-engineered (no game mod API; this is the only mechanism).

**Script-binding registration `FUN_1405D5910`** maps script names → native function
pointers (the technique: a binding fn calls `register(ctx,"Name",&NativeFn,-1)` per
API). Key natives: `PurchaseLocalListing`=`0x1405F3BF0`, `GetSpeciesFromListingID`=
`0x1405F4CE0`, `SetLocalAnimalExchangeActiveWhitelist`=`0x1405F4570`,
`PushLocalAnimalExchangeToDatastore`=`0x1405F3AE0`, rating getters
`GetConservationRating`=`0x1405F9780` / `GetEducationRating`=`0x1405F9870`.

**Buy path:** `PurchaseLocalListing`(`0x1405F3BF0`) → glue `FUN_1466B6DD0` (parses the
listing id, calls) → **native `FUN_14A089410(exchange_mgr, int listing_id)`**:
- `exchange_mgr = *(park_mgr + 0x168)`.
- loops listing records at `[exchange_mgr+0x248]`, **stride `0x240`**, finds `rec`
  where `[rec+0x228] == listing_id`.
- listing record fields: **`[rec+0x10]` = species/animal entity handle (u32)**,
  `[rec+0x208]` = cost (negative), `[rec+0x228]` = listing id.
- validates funds, then **spawns** (`thunk_FUN_14A08B6F0(exchange,rec)`) and returns 1;
  returns 0 at `LAB_14A0894B7` if not found / insufficient funds.

**Gate = conditional-abort detour in `FUN_14A089410`** at the listing-found / pre-spawn
point: read `[rec+0x10]` (species handle) → resolve to species id (reuse the births
`AnimalResolver` path: `*(zoo+0x2f8)` manager → hashmap → record → species id, OR call
the game resolver `FUN_146EC8630`) → if that species is AP-gated AND its permit isn't
owned (client writes a locked-gated species set into a scratch region) → **`jmp
LAB_14A0894B7`** (return 0, no spawn). Buy "fails" → fully memory-enforced, no honor
system, biome-independent. This is more advanced than the births record-only hook
(must resolve species + branch to the function's fail-return synchronously).

Alt gates (worse): the game's native **whitelist** `[exchange_mgr+0x3B8]` = a handle to
a whitelist-collection entity (set via `FUN_14A099DB0`; 0 = unrestricted) — cleanest
UX but requires constructing/populating a collection entity (hard via raw writes); or
hook the display builder `PushLocalAnimalExchangeToDatastore` to skip gated species.

**Exact hook site (disasm of `FUN_14A089410`):**
- listing match: `0x14A0894A0 cmp [rbx+0x228],esi ; 0x14A0894A6 je 0x14A0894E5`.
- **HOOK at `0x14A0894E5`** (`movzx eax, byte ptr [rbx+0x210]` = `0F B6 83 10 02 00 00`,
  7 bytes; rbx=listing record, r15=exchange_mgr). It's a `je` target → a 5-byte jmp +
  2 NOP at the start is safe (branch lands on the patch start).
- **FAIL-RETURN `0x14A0894B7`** (`xor bl,bl; lea rcx,[r15+0x608]; call 0x1401B55D0`
  (release the lock taken at entry `0x14A089437`); restore regs; `ret`) — returns 0,
  no spawn, lock balanced. Jump here to block.
- listing record: `[rbx+0x10]`=species/animal handle, `[rbx+0x208]`=cost(neg),
  `[rbx+0x228]`=listing id, `[rbx+0x210]`=a funds-path flag (the byte the orig reads).

**Trampoline (conditional-abort):** preserve regs (use rax + push/pop); decide BLOCK:
  - Approach A (no in-hook resolution, preferred): client maintains a scratch array of
    BLOCKED LISTING IDs (it polls the exchange listings via the captured r15, resolves
    each `[rec+0x10]`→species, marks gated+locked ones). Trampoline reads `[rbx+0x228]`
    (listing id), loops the small blocked-id array; if present → `jmp 0x14A0894B7`. Also
    record r15 (exchange_mgr) to scratch so the client can poll. (Small race: a buy in
    the instant before the client marks a new listing; acceptable, or default-block
    unknown gated species.)
  - Approach B: resolve `[rbx+0x10]`→species in-hook (call `FUN_146EC8630(*(zoo+0x2f8),
    handle)` → species id) and check a scratch species set — heavier asm, no race.
  - If NOT blocked: run the relocated `movzx` (position-independent) + `jmp 0x14A0894EC`.
Client writes/updates the blocked set on permit receipt. Live-test: a gated species'
buy must fail (no animal, no charge) until its permit arrives. CAUTION: this detours a
live purchase fn — verify bytes, suspend during patch, test carefully (crash risk).

## Offline aid

`tools/disasm.py dump` already snapshotted the module to `tools/.pz_module.bin`;
`dis/xref/func/str` work against it without the game running.

---

# RESEARCH (research_complete) — Ghidra investigation plan (2026-06-01)

> Goal: two deliverables the client needs for the 12 `research_complete` AP locations
> (`welfare_<10 species>` + `enrichment_generalist` + `habitat_advanced_barriers`):
>  (1) **research_id ↔ species map** — which research item belongs to which species;
>  (2) **progress/completion read** — given a research item, is it complete (and where
>      is that stored, re-derivable from a stable root each session).

## Why Ghidra (live-RE is exhausted)
`[zoo]+0x328` (0x13FFCFA0) is the **unlocked-items catalog** (grows ~32 records per
research level), NOT the per-research level state. `mgr_diff` after warthog L4→L5 gave
only fluctuating / pointer-laden 4→5 candidates, no stable per-species level array. The
research **query** script functions (below) are referenced only in a name→list
reflection table (`call 0x1419e4a60`), i.e. **Cobra hash-bound** — no string→native-fn
pointer for the linear disassembler to follow. Ghidra resolves hashed/virtual bindings.

## Primary targets (script-binding name strings; image base 0x140000000)
- **`GetResearchItemAssociatedSpecies`** name @ `0x14267A690`  ← THE id↔species map
- **`GetResearchItemLevel`** name @ `0x14267A838`              ← per-item current level
- **`GetResearchItemPercentageComplete`** name @ `0x14267AB10` ← completion %
- `IsResearchItemUnlocked` @ `0x14267A8F8`, `CompleteResearch` @ `0x14267A888`,
  `GetResearchCategoryLevel` @ `0x14267A670`, `GetResearchType`/`GetResearchBaseType`
  @ `0x14267A7F0`/`0x14267A800`, enum `ResearchStatus_ResearchedAndCompleted` @ `0x14267ABC8`.
- Content-def type: **`AnimalResearchUnlocksSettings`** @ `0x1426360F0` (the content that
  defines research→unlocks per species — likely holds the id↔species relation statically).

## Steps (you drive Ghidra; paste decompilation back, I analyze — like births/permits/rating)
1. In Ghidra: Search → For Strings → `GetResearchItemAssociatedSpecies`; then References
   To that string. One ref is the reflection table (the `0x14063F4EB` site, ignore).
   Look for the **Cobra binding** ref: a registration that passes the string + a native
   function pointer (often via a hash helper). If the string only has the reflection ref,
   instead open the **native fn by behavior**: it takes a research-item handle/id and
   returns a species id — find it near the other `GetResearchItem*` natives.
2. Decompile `GetResearchItemAssociatedSpecies`'s native fn → paste it. I'll extract how
   a research id maps to a species (the field/table that links them) → fills the id↔species
   map for ALL species (owned or not).
3. Decompile `GetResearchItemLevel` + `GetResearchItemPercentageComplete` natives → paste.
   I'll extract WHERE per-research level/completion is stored (the structure + offset) and
   how "complete" is computed (likely level≥max or `ResearchStatus_ResearchedAndCompleted`),
   and a stable root to reach it (the zoo is already reachable: zoo = `*(*(base+0x29446A0)+0x20)+0x390`).
4. With both, the client reads each AP key's completion directly. Then wire `zoo_deref`
   resolution in `anchors.py` (currently `resolve()` doesn't handle `kind="zoo_deref"`).

## Notes / leads
- Live ids seen in the catalog: `0x11xx` and `0x39xx` ranges; the content-def array (from
  the earlier session) used `0x41xx` at `0x7211C00` grouped per-species by vtable `0x14272DA68`.
  Determining which id-space `GetResearchItemLevel` indexes is part of step 3.
- zoo (EntityManager) = `0x14D446C0` this session, **stable chain** `*(*(base+0x29446A0)+0x20)+0x390`.
- Reflection registration of all `GetResearchItem*` names lives around `0x14063F3xx–0x14063FDxx`
  (FUN containing those `lea r8,[name]; call 0x1419e4a60` lines) — a good anchor to find the
  parallel native-binding table if Cobra registers them together.

## RESEARCH — progress (2026-06-01): binding table + handlers + completion enum DECODED

Binding registration fn = **`FUN_14063e570`** (registers each research script name with its
native handler; pattern: store `*puVar7 = HANDLER` then `thunk_FUN_14ee19ef0(ctx,uVar10,"Name")`).
Native handlers extracted (handler precedes its name in the fn):
- `GetResearchItemAssociatedSpecies` → `FUN_140649310` → thunk → **`FUN_1468BCDE0`**
- `GetResearchItemLevel` → **`FUN_140648CB0`**  (full fn; the completion read)
- `GetResearchItemPercentageComplete` → `FUN_140649990`
- `CompleteResearch` → `FUN_140646130`; `IsResearchItemUnlocked` → `FUN_1406468F0`;
  `GetResearchType` → `FUN_140648720`; `GetIsResearchItemAvailable` → `FUN_1406497D0`

**Completion enum (decoded from FUN_14063e570 tail):** `ResearchStatus`: NotStarted=0,
Researchable=1, Researching=2, ResearchedButNotCompleted=3, **ResearchedAndCompleted=4 (COMPLETE)**,
Removed=5. `ResearchBaseTypes`: Mechanic=0, Disease=1, Animal=2.

**Associated-species path (FUN_1468BCDE0):** after the script-arg boilerplate (research-item
arg → r14), it calls the worker **`FUN_140E456A0`** with `rcx=[r14+0x28]` → returns a species
index in `edi`, then validates it against the species pool global (`[rip-0x3f32116]`, strided
mov r9=[rcx+0x10]/r8=[rcx+0x30]/imul by index). `FUN_140E456A0` resolves research→species via a
content map at `[rcx+0xf8]` (hashmap find `FUN_14064F0B0`, record stride **0x58**, sub-calls
`FUN_14064E200`). So research-item → species is a content-def hashmap lookup (stride 0x58).

### Next steps (best with Ghidra decompiler output)
1. Paste **decompilation** of `FUN_140648CB0` (GetResearchItemLevel) → find the research-state
   array/structure it indexes by research-item id and where `level` lives → the completion read
   (level≥max or status==4). Reachable from a stable root (zoo = `*(*(base+0x29446A0)+0x20)+0x390`).
2. Paste **decompilation** of `FUN_140E456A0` + `FUN_14064F0B0` → the research-item→species map
   (so each AP `welfare_<species>` key resolves to its research-item id).
3. Build client read: research_item_id(welfare_<species>) → level/status → complete. Wire
   `kind="zoo_deref"` in anchors.py `resolve()` (currently unhandled).

### KEY FINDING (FUN_140648CB0 decompiled): the research read is NOT a flat field
`GetResearchItemLevel` resolves the research item via `FUN_140E456A0` (content map), then the
**entity pool `DAT_14298AE00`** (generational handle table: `[+0x30]` top, `[+0x10]` stride,
`[+0x48]` count), pulls a value that may be the literal string `"Infinite"`, and **parses a
string→number** (`FUN_1400B7960`). So per-research level/status is a computed value behind
content-map + entity-pool + string indirection — there is **no cheap stable offset** to read.

=> RECOMMENDED STRATEGY for research_complete (focused follow-up, pick one):
  (A) **Call the native query in-process** (injection): build/borrow a script-VM stack and call
      `GetResearchItemLevel` / a `GetResearchStatus`-style fn, read back status==4. Heaviest
      plumbing but exact. (We already inject/detour; calling a script-bound fn needs a valid
      `param_1` script context — capture one from a live script call via a detour, then reuse.)
  (B) **Find the lower-level research-state store** these fns ultimately read/write (follow
      `CompleteResearch` `FUN_140646130` and `FUN_140E456A0`'s map `[rcx+0xf8]` writes) — likely
      a per-research record keyed by research-item id; read status/level from it via a stable
      root. Cleaner if the store is a flat keyed table.
  (C) Detour `CompleteResearch` (`FUN_140646130`) like births/release: record (research-item id)
      on each completion → client maps id→`welfare_<species>`. Simplest + matches our proven
      pattern; only needs the id→species map (worker `FUN_140E456A0`, stride-0x58 content map)
      to label completions. **This is likely the best ROI** (a completion-event hook, not a read).

### Option C assessed (CompleteResearch FUN_140646130 → FUN_1468B0EA0): also Cobra-script-mediated
The CompleteResearch native is the script binding: arg boilerplate (research arg → rbp; `mov
rbp,[rbp+0x28]` = research-system handle), then **string handling** (`FUN_1400B9260`, `[rbx+0x14]`
string). Two issues: (1) the research id is a string inside script-arg objects, no clean register;
(2) it's unconfirmed that NATURAL (vet) completion routes through this *script* binding vs an
internal path. So a clean entry-detour that records the completed research id is NOT available here.

CONCLUSION: research is mediated entirely by the Cobra script/content/entity-pool/string layer —
no clean native hook site like births(insert)/release(executor)/permits(listing loop). It needs a
DIFFERENT strategy + interactive Ghidra (linear offline disasm can't cross the hash/virtual/string
indirection efficiently). Strongest next entry points for a focused session:
  - **`ResearchUnlockChangeMessage`** (msg type @0x14266D490, `MsgType_` @0x14266E168): find the
    message BROADCAST site on completion and the handler — a message carries the changed research,
    likely with the id in a register at the dispatch. Hook that.
  - Capture a live **script-VM context** (`param_1`) via a transient detour on any research script
    call, then call `GetResearchItemLevel`/status from injected code reusing that context (Option A).
  - Trace `FUN_140E456A0`'s content map `[rcx+0xf8]` (stride 0x58) + the entity pool to a flat
    per-research record store (Option B).
Research is fully SCOPED (handlers, completion enum status==4, read-path, id→species worker all
located) but remains a dedicated follow-up sub-project; NOT implemented.

---

# FACILITY GATE (facility_unlock placement-block) — Ghidra target (2026-06-02)

> Goal: enforce the 4 `facility_unlock` AP items (Research Centre, Workshop, Trade Centre,
> Veterinary Surgery) by BLOCKING PLACEMENT until the item arrives (user decision). The
> client side is DONE (`memory/facilities.py` FacilityGate + applier + reconcile + tests);
> it stays a no-op until the constants below are filled. We just need the placement
> executor + the building def-id field, then capture each facility's def-id.

## What to find (fill these in `memory/facilities.py` / `memory/hook.py`)
1. **FACILITY_RVA** + **FACILITY_ORIG** — a stable code site on the building-placement
   COMMIT path (or the `CanPlace` predicate) with >=5 relocatable bytes for the jmp.
2. **The register + offset** holding the building/blueprint DEFINITION id at that site
   (set `FACILITY_DEFID_OFF`, and adjust the base register in `make_facility_gate` if it
   isn't rbx). The def-id is the value we gate on (must distinguish Research Centre vs
   Workshop vs any other building the player legitimately places).
3. **The abort target** — either a clean `xor eax,eax; ret` (if rsp is clean at entry, like
   the release executor) or a fail-return address (set `FACILITY_FAIL_DELTA = site - fail`).

## Technique (same as release-to-wild: name string -> binding -> native handler)
Each name below is a Cobra script-binding NAME string in the same .rdata table as the
release name (`0x14265C788`) and the research names. In Ghidra: References-To the name
string -> the registration FUN stores the native HANDLER pointer just before the
`lea r8/rdx,[name]; call <register_thunk>` (handler-precedes-name pattern, cf. research
`FUN_14063e570`). Follow handler -> thunk -> native executor; decompile it.

## Candidate binding-name string VAs (image base 0x140000000)
- `CanPlace`                       @ 0x142683D20   <- BEST: the can-place predicate. If it
   takes the blueprint/def-id and returns bool, hook it to force-false for gated def-ids
   (blocks at the cursor; cleanest UX). Confirm its arg = blueprint and where the def-id is.
- `AddBuilding`                    @ 0x142661450   <- the placement/commit; gate by def-id.
- `CreateBuildingPartSet`          @ 0x14267D248
- `CreateBuildingHighlightRequest` @ 0x14267D2A0
- `AddBlueprintToCompositeObject`  @ 0x142662240
- `CalculateBlueprintInfo`         @ 0x1426610E8
- Facility identity helpers (to find the def-id / type discriminator for the 4 targets):
  `IsResearchCentre` @0x142665218, `ZooHasResearchCentre` @0x1426652E0,
  `IsSurgery` @0x142664CC8, `GetSurgeryCapacity` @0x142664C88.

## Then: capture each facility's def-id
Once FACILITY_RVA is set, install in CAPTURE mode (a no-spend/always-allow variant logging
`[reg+off]` to scratch+0x100, like tools/capture_species.py) and have the user open the
build menu / attempt placing each of the 4 facilities once -> read the logged def-id ->
fill `FACILITY_DEFID`. VERIFY the def-id is stable across a restart (expected: it's a
content-def id, not a per-session handle). Then the gate goes live with zero further RE.

## Note on `tool_unlock` water_tools (separate, not a facility)
Water Habitat Tools is a TERRAIN edit, not a placed building -> different executor
(`CreateTerrainEditOperation`; flatten tool has a tri-state lock enum
`FlattenTerrain_Unlocked/_LockedAlwaysFlatten/_LockedNeverFlatten` + `GetFlattenTerrainToggleState`
suggesting a settable per-tool lock-state model). Handle after the facility gate is proven.
