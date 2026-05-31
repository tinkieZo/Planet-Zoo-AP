# A2 — Cheat-Engine Memory Spike Playbook

> **Goal of A2:** find *stable* memory anchors for the values the client must
> read (research-complete flags, animal births, zoo rating, guest count,
> conservation releases) and write (cash, CC, permits, research flags), and
> record how to resolve each one in [`pz_ap_client/memory/anchors.json`](../pz_ap_client/memory/anchors.json).
>
> This is the **make-or-break unknown** of Track A (per `ARCHIPELAGO_PLAN.md`).
> Everything else is built and tested; the only thing standing between us and a
> live round-trip is filling that table.

The scanner/applier/trigger code is already written and unit-tested against an
unfilled table (it degrades to no-ops). Your job here is purely **reconnaissance
+ data entry into `anchors.json`** — no client code changes should be needed for
the happy path.

---

## Preferred path: `tools/memscan.py` (Claude drives, you play)

You don't have to run Cheat Engine by hand. [`tools/memscan.py`](../tools/memscan.py)
reproduces CE's scan loop over `pymem`, so the division of labour is:

- **You:** play the game, perform the action (spend cash, breed an animal), and
  read the on-screen number back ("cash is 60,000 now").
- **Claude / the tool:** attach, scan, narrow, pointer-scan for a stable chain,
  test-write, and save the anchor into `anchors.json`.

```
python -m tools.memscan            # attaches to PlanetZoo.exe
```

Typical session for a scalar (cash):
```
type double
new 75000            # you tell me current cash; tool scans for 75000.0
                     # >> you spend money in-game, cash -> 60000
next 60000           # narrows to addresses that moved 75000 -> 60000
                     # repeat next <value> until 1–2 candidates
list
write 0x<addr> 65000 # you watch the HUD jump to 65,000 to confirm
save cash 0x<addr>   # pointer-scans + writes a module_offset anchor to anchors.json
```
For an **unknown** value (e.g. the birth counter): `new` won't help; use
`next inc` / `next dec` / `next changed` across in-game events to converge.

`save` runs a pointer-scan and stores the shortest static chain
(`base + offsets`). If it finds none (`no pointer chain found`), that anchor is
the rare case that needs CE's code-signature route below.

> The one thing memscan can't do is CE's hardware-breakpoint **"find what
> accesses this address"**. Everything else — value scans, pointer scans, write
> tests, saving anchors — it does. Use the CE steps below only as a fallback.

---

## 0. Setup (once) — Cheat Engine fallback

1. Launch Planet Zoo, start/load a **Challenge** save (the fixed slice save).
2. Open **Cheat Engine**, `File ▸ Open Process ▸ PlanetZoo.exe`.
3. Confirm the process name matches `anchors.json` → `process_name`
   (`PlanetZoo.exe`). If Frontier renamed it, update that field.
4. Settings ▸ Scan: enable **"MEM_PRIVATE"** and **"MEM_IMAGE"**; pause the game
   while scanning to reduce churn.

> ⚠️ **Anti-cheat / safety.** This is local single-player Challenge memory
> editing for our own randomizer. Don't attach to anything online. Expect Frontier
> patches to move things — that's why we record **signatures**, not raw addresses.

---

## 1. The "known value" scan loop (use for every scalar)

This is the standard CE workflow we'll repeat for cash, CC, rating, guests,
releases:

1. Note the current in-game value (e.g. cash = 75,000).
2. CE → set **Value Type**, **First Scan** for that value.
3. Change it in-game (spend/earn), **Next Scan** for the new value.
4. Repeat until a handful of addresses remain; green ones (static) are gold.
5. Right-click → **"Find out what accesses/writes this address"** to get an
   instruction that references it → basis for a **signature** (see §3).

**Value types to try, in order:**
| value | likely type | why |
|---|---|---|
| cash | `double` | Frontier money is usually 64-bit float |
| conservation credits | `double` or `i32` | try double first |
| zoo rating | `float` | shown as 0–5 stars, fractional |
| guest count | `i32` | integer |
| conservation releases | `i32` | small integer counter |

---

## 2. Animal-birth signal (HIGHEST RISK — do this first)

The `first_breed` locations need a reliable "a baby of species X was born" edge.
Two viable shapes; determine which exists:

**Option A — per-species birth count** (preferred). Each owned species has a
"born in zoo" / population stat. Find the species record, locate a monotonic
birth counter within it. The client diffs it (`baseline → +1`) to fire the check.
→ Record base in `species_roster_base` and per-species offset in
`entity_offsets.species_birth[<species_key>]`.

**Option B — global birth/notification counter.** A single counter that
increments on every birth. Easier to find, but can't attribute the species
without extra work (read the notification payload, or cross-reference roster
population deltas). → Record as `birth_event_counter`; then extend
`triggers.py::_poll_first_breed` to attribute species (leave a note here on how).

**How to find it:** breed a Plains Zebra (ungated starter). Right before the
birth, First Scan "unknown initial value"; after birth, "increased value" scans.
Cross-check by breeding a second species and watching which counters move.

> If neither shape is findable in a reasonable timebox, **escalate** — this is
> the gate the whole plan flagged. Fallback options: OCR the birth notification,
> or treat `first_breed` as a manual `/pz_check` for the slice.

---

## 3. Turn an address into a signature (patch-robustness)

Raw addresses move every patch; we store an **AOB signature** instead.

1. On a found address, CE → **"Find out what writes/accesses this address"**.
2. Pick a stable-looking instruction, e.g. `mov [rax+1C],rbx` or
   `movsd [rip+0x00ABCDEF],xmm0`.
3. CE → **"Disassemble this memory region"**, select the instruction, copy
   ~12–16 bytes, and **wildcard the operand bytes** (the displacement/address),
   e.g. `F2 0F 11 05 ?? ?? ?? ??`.
4. For **RIP-relative** instructions (the `[rip+disp32]` form), record:
   - `signature`: the AOB with the disp32 wildcarded,
   - `rip`: `{ "disp_offset": <byte index of disp32 in the match>, "instr_len": <total instruction length> }`.
   The scanner does `target = match + instr_len + disp32` for you
   (`MemoryScanner.resolve_rip_relative`).
5. If the value is reached via a pointer chain from that static, add the chain
   to `offsets` (CE's pointer-scan gives these; remember our convention:
   dereference every offset **except the last**, which is added — see
   `scanner.resolve_pointer_chain`).

**Validate the signature:** restart the game / reload the save and re-scan — the
signature must still resolve and point at the right value. If it matches in
multiple places, lengthen it until unique.

---

## 4. Write path (apply received items)

For each writable anchor, confirm a write **sticks and updates the HUD**:

- **cash / CC** — `MemoryEffectApplier` does read-modify-write (`current + amount`).
  Test by setting `cash` to `current + 50000`; the HUD should jump. Watch for a
  shadow/auth copy that overwrites your write next tick (if so, find and write
  that one too, or the source-of-truth).
- **permits (`species_unlock`)** — flip the owned/unlocked flag at
  `species_roster_base + entity_offsets.species[<key>]`. Confirm the species
  becomes purchasable. Record the sentinel value in a note (1? bitfield bit?).
- **research flags (`research_complete` write, if used as a grant)** — usually we
  only *read* these (they're locations), but if a received item needs to mark
  research done, mirror the read offset.
- **tool/facility/program unlocks** — these are the least-understood. If they're
  simple booleans, treat like permits. If they're gated by tech-tree state,
  document findings and we'll decide whether to model them as memory writes or
  as soft (player-actioned) unlocks. Until implemented, `MemoryEffectApplier`
  intentionally **stalls** these items (returns False, logs a warning) rather
  than silently skipping progression.

---

## 5. Fill in `anchors.json`

For every anchor, set `kind`, `type`, `signature` (+ `rip` if RIP-relative),
`offsets`, and a `notes` line with the CE instruction you based it on. Populate
`entity_offsets.research` / `.species` / `.species_birth` with one entry per key
**using the exact keys from `data.json`** (e.g. `welfare_giant_panda`,
`giant_panda`).

Then verify with the game running:

```
python -m pz_ap_client.client <server:port> --name <slot> --memory
```

On connect it logs which anchors are still unfilled. With the table complete:
- received cash/CC items change the balance once each (idempotent across reconnect),
- completing a research / breeding a species fires the matching check,
- crossing a milestone threshold fires its check.

---

## 6. Definition of done for A2

- [ ] `anchors.json` resolves **all** scalars (cash, CC, rating, guests, releases)
      after a game restart (signatures stable).
- [ ] research-complete reads correctly for at least the 3 starter species.
- [ ] birth detection fires for a Plains Zebra breed (Option A or B documented).
- [ ] cash write sticks and updates the HUD.
- [ ] a permit write makes a gated species purchasable.
- [ ] the offset/signature table in this repo matches what's documented here.

When these hold, Track A is ready for the integration milestone with Track B.
