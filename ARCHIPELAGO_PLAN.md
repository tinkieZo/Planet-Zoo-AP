# Planet Zoo × Archipelago — Implementation Plan

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
already ships several such graphs — the **research tree**, **conservation credits (CC)**,
**ratings/milestones**, **breeding**. We don't invent the structure (as we'd have to in a
sandbox like Arma 3); we *re-route* the structure the game already has.

The hard part is integration: Planet Zoo has **no scripting / mod API**, so all logic lives
in an external **memory-hooking client** (the Cheat-Engine-style approach the community
already uses). This is fragile across Frontier patches — we mitigate with signature/AOB
scanning instead of hardcoded addresses.

**Key architectural fact:** the client never needs the progression *logic*. Logic lives
entirely in the APWorld (generation-side). The client only needs:
- item ID → "do this in the game" (apply effect)
- game event → location ID (detect & report a check)

That makes the seam between the two work tracks narrow and stable: it's just `data.json`.

---

## The three pieces

1. **APWorld (Python)** — items, locations, regions, access rules, options, goal. Consumed
   by the AP generator. (Track B)
2. **Hooking client (Python + `pymem`)** — subclasses Archipelago's `CommonClient` (network
   layer free), reads/writes game memory to detect checks and apply received items. (Track A)
3. **No in-game mod** — impossible without a script API, so all behavior lives in the client.

---

## Vertical-slice scope (agreed in Phase 0)

- **Mode:** Challenge, fixed starting save.
- **10 species:** 4 ungated starters (sphere 0) + 6 gated behind water tools / permits / conservation.
- **20 locations:** 12 research + 5 first-breed + 3 milestones.
- **20 items:** 9 progression + 5 useful + 6 filler. (Item count == location count, as AP requires.)
- **No traps** in the slice.
- **Goal:** complete the flagship research + first-breed chain (see `data.json` `slot_data.goal`).

The canonical data is in **`data.json`** — both tracks code against it. Field reference below.

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
- `tool_unlock` — `{tool_key}` (e.g. climate/water building tools)
- `facility_unlock` — `{facility_key}` (research centre, vet surgery)
- `species_unlock` — `{species_key}` (permit to acquire a species)
- `program_unlock` — `{program_key}` (conservation program → CC economy)
- `cash` — `{amount}`
- `cc` — `{amount}` (conservation credits)
- `staff_training` — `{levels}`
- `marketing` — `{campaign}`
- `enrichment_pack` — `{}`

### `locations[]`
| field | meaning |
|---|---|
| `id` | stable int, unique across locations |
| `name` | display name (must match APWorld + client) |
| `trigger_type` | how the **client** detects the check (enum below) |
| `trigger_args` | object with trigger parameters |

`trigger_type` enum (client-owned semantics):
- `research_complete` — `{research_key}`
- `first_breed` — `{species_key}`
- `milestone` — `{metric, threshold}` (metric ∈ `zoo_rating`, `guest_count`, `conservation_release`)

### `slot_data`
Sent by the APWorld to the client at connect:
- `goal` — `{type, args}` (slice: `type: "chain"`, complete flagship research + breed)
- `death_link` / `escape_link` — bool (off for the slice)
- `options_echo` — generation options the client may want to display

---

## Suggested logic graph (Track B owns final rules)

Mirrors the gates encoded in `data.json` (Track B owns the final access rules in the APWorld).

```
Start (sphere 0)
├── ungated species: Plains Zebra, Grey Wolf, American Bison, African Elephant
│     → acquired + bred immediately; their First-Breeding locations are reachable now
│     (welfare RESEARCH for ANY species still needs the Research Centre — see below)
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

Notes: **all research is facility-gated** — no `Research:*` location is sphere 0. The Research
Centre gates the per-species welfare research (animal, cat 7); the Workshop gates the *mechanic*
research — **both** Drink Shops **and** Advanced Barriers (cat 3), despite the latter's "Habitat"
display name. A species' **First-Breeding** location inherits that species' acquisition gate (you
can only breed what you can build); the **Zoo Rating** and **Guests** milestones are ungated economy
goals. The flagship **Giant Panda** is intentionally double-gated — its permit **plus** the
**Conservation Program** (the conservation-icon animal, and the hub of the release milestone). Every
other gated species is **permit-only** (the Lowland Gorilla's redundant Research-Centre gate was
dropped, since the Research Centre is already a de-facto early item — all welfare research needs it).
Climate-control gating was dropped — gated species use **permits** (plus water tools / conservation).
Keep rules **conservative**: players will break optimistic assumptions.

---

## Track A — Hooking client (Person 1)

**Stack:** Python + `pymem`, subclass Archipelago `CommonClient` (network layer is free).

**A1 — AP client shell (no game needed)**
- Subclass `CommonClient`; connect to a real AP server running a Track-B seed.
- Add a **manual trigger console**: type a location name → send that check; print received
  items. This stands in for the game until A2 lands and tests the full AP round-trip.

**A2 — Memory access layer (no AP needed)** — *highest-risk, start early*
- **Cheat Engine spike:** locate stable anchors for research-complete flags, species roster,
  cash, CC, and an animal-birth signal. Produce an **offset/signature table** doc.
- Implement **AOB/signature scanning** (not hardcoded addresses) + pointer-chain resolution.
- Read path: snapshot relevant memory each poll tick. Write path: grant cash/CC, set a
  research-complete flag, flip a species permit.

**A3 — Glue / state machine**
- Poll loop: diff snapshot → map events to **location IDs** (via `data.json`) → send checks; debounce.
- Apply received **item IDs** → effects (via `data.json`).
- **Idempotent re-grant:** on (re)connect replay the server's full received set without
  double-applying; track an applied-index high-water mark in a local state file.

**Track A done:** complete a research item in-game → check fires; another player's item
arrives → effect applied in-game; restart save → state re-synced correctly.

---

## Track B — APWorld / item & location graph (Person 2)

**Needs no game, no memory work** — test entirely with the AP generator + standard text client.

**B1 — Skeleton APWorld**
- Scaffold the World subclass; build `item_name_to_id` / `location_name_to_id` from `data.json`;
  declare options + game name; generate a seed without crashing.

**B2 — Regions & access rules**
- Encode the logic graph above; gate locations behind progression items; place the goal.

**B3 — Fill & validation**
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
- **Phase 0 (`data.json`)** blocks everything — DONE (this commit).
- After Phase 0, **Track A and Track B are independent** until the integration milestone.
- Within Track A, do the **A2 Cheat-Engine spike first** — it's the make-or-break unknown.
- Track B depends on nothing from Track A.
