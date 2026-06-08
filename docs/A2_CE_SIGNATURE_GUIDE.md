# A2 - Cheat-Engine AOB Signature Guide (robustness pass)

> Companion to [`A2_SPIKE_PLAYBOOK.md`](A2_SPIKE_PLAYBOOK.md). Use this for the
> anchors that `memscan` located but **cannot make restart-robust** with a
> pointer chain.

## Why this exists (the spike's robustness finding)

We empirically restart-tested the `module_offset` pointer chains:

| Container | Anchors | Restart result |
|---|---|---|
| **Finance** | `cash`, `conservation_credits` | **robust** - 2 of 9 candidate chains survived **two** full restarts; committed |
| **Zoo-stats** | `guest_count` (and likely `zoo_rating`) | **fragile** - **0 of 23** candidate chains survived a restart |
| **Roster** | `species_roster_base` | reached via the same unstable stats root → treat as fragile |

So pointer chains are **container-specific**: fine for finance, useless for the
stats/roster container. Those anchors need an **AOB code signature** - the one
resolution method `memscan` can't produce, because it relies on CE's
hardware-breakpoint **"Find out what accesses this address"**.

`cash`/`conservation_credits` already work via validated chains; you only need CE
for the anchors marked `PENDING - LIKELY NEEDS A CE SIGNATURE` in
[`anchors.json`](../pz_ap_client/memory/anchors.json).

---

## How a `signature` anchor resolves (capture the right thing)

`AnchorTable`/`MemoryScanner` resolve a signature anchor like this
(`scanner.py`):

1. `aob_scan(signature)` → address of the **matched instruction**.
2. if `rip` is set → `resolve_rip_relative(hit, disp_offset, instr_len)`:
   `target = instr_addr + instr_len + read_i32(instr_addr + disp_offset)`.
   This turns a `[rip+disp32]` instruction into the **address of the static it
   references** (typically a global pointer to the manager/container).
3. if `offsets` is set → `resolve_pointer_chain(target, offsets)` walks the
   struct (deref every offset except the last, which is added).

So the goal in CE is: **find an instruction that references a STABLE static**
(a global pointer near the manager), capture it as an AOB with the `disp32`
**wildcarded**, and record `rip:{disp_offset, instr_len}` plus the `offsets`
from that static down to the field.

The most robust pick is an instruction that loads the **container/manager
pointer** (a static global), not one that touches the volatile field directly -
then `offsets` are stable struct member offsets (e.g. `0x830`).

---

## Per-anchor recipes

For each, first re-find the **live address** in CE (values drift/move per
session - that's expected), then run "find what accesses", then build the AOB.

### `guest_count` (i32, read-only; milestone threshold 1000)
- **Find it:** CE value type **4 Bytes**, scan your current guest number, let it
  drift, Next Scan the new number, repeat to 1–3 addresses. (Field sits at
  **stats-container + 0x830**; a sibling at +0x58/+... may be total/peak.)
- **Signature:** right-click the address → **Find out what accesses this
  address** → let a few instructions collect → prefer one like
  `mov eax,[rax+830]` / `mov [rcx+830],…` whose base register was loaded from a
  `[rip+disp32]` global just above. Ideally signature the instruction that loads
  the **stats-container pointer** (`mov rax,[rip+disp32]`), then `offsets`
  = `[0x830]` (or the deref path to the container, then `0x830`).
- **Record:** `kind:"signature"`, `type:"i32"`, `scale:1`,
  `signature:"<AOB, disp32 wildcarded>"`, `rip:{disp_offset,instr_len}`,
  `offsets:[…,0x830]`.

### `species_roster_base` + `species_birth[plains_zebra]` (i32 population)
- **Find it:** the per-species population record. Re-isolate the zebra-count
  field (own a known number of zebras, value-scan + change as you buy/sell). In
  that record, **zebra pop is at object+0x630**, the disambiguator warthog count
  was at **+0x530** (delta 0x100).
- **Signature:** "find what accesses" the zebra-count address while a keeper/UI
  reads it; capture the instruction loading the **roster object pointer** via
  `[rip+disp32]`.
- **Record:** signature for the roster base; keep
  `entity_offsets.species_birth.plains_zebra = 1584` (0x630). Map other species
  by owning one of each and noting its offset (stride not yet confirmed).

### `zoo_rating` (float, read-only; milestone threshold 2 stars)
- **Locate first** (not yet found): the **aggregate** Zoo Reputation, not the
  per-entity rating block (that block is at stats-container+0x910.., an array of
  ~0x28-byte records - NOT the 6 displayed ratings). Easiest when reputation is
  a clearly fractional value, or via change-detection. Once you have the
  aggregate address, signature it as above. `scale:1` if stored 0–5.

### `research_state_base` + `research[<key>]` and `conservation_release_count`
- **Locate first** via a 0→1 event (complete a *tracked* research:
  `welfare_*`, `enrichment_generalist`, `habitat_advanced_barriers`; or release
  an animal). Pause the game around the event to cut sim noise, diff to find the
  flag/counter, then signature the instruction that accesses it. Per-research
  keys live at `research_state_base + research[key]`.

---

## Generic CE workflow (per address)

1. `File ▸ Open Process ▸ PlanetZoo.exe`. Confirm `process_name` in anchors.json.
2. Value-scan to the field's address (see recipes).
3. Right-click → **Find out what accesses this address**. Trigger the value to
   be read (hover the HUD/panel) so instructions appear.
4. Pick a stable instruction; double-click → **Show disassembler**. Prefer a
   `mov reg,[rip+disp32]` that loads a global pointer.
5. Select ~12–16 bytes; note the byte index of the `disp32` and the total
   instruction length. **Wildcard the disp32 bytes**, e.g.
   `48 8B 05 ?? ?? ?? ??` (RIP-relative `mov rax,[rip+disp32]`).
6. **Validate uniqueness:** in CE, memory-view → search the AOB; if it matches in
   multiple places, lengthen it. Restart the game, re-scan the AOB - it must
   still resolve and (with `rip`+`offsets`) point at the right value.

`rip` for a `48 8B 05 ?? ?? ?? ??` instruction (7 bytes total, disp32 at byte 3):
`{"disp_offset": 3, "instr_len": 7}`.

---

## Handing results back

For each anchor give me: the **AOB string** (operand wildcarded), the
**`disp_offset`/`instr_len`** if RIP-relative, and the **`offsets`** from the
resolved static to the field. I'll write them into `anchors.json`, then we
re-resolve live and re-run the restart validation (`tools/validate_anchors.py`
pattern) to confirm each survives.

## Definition of done (robustness)

- [ ] `guest_count` resolves via signature after a restart.
- [ ] `species_roster_base` resolves; `species_birth[plains_zebra]` reads the live count.
- [ ] `zoo_rating` aggregate located + signature resolves.
- [ ] `research_state_base` + ≥3 starter `research[key]` read correctly.
- [ ] `conservation_release_count` located + signature resolves.
- [ ] all signatures survive a game restart (re-scan still resolves).
