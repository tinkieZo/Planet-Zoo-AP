# Track A — Planet Zoo × Archipelago hooking client

External memory-hooking client that bridges a running **Planet Zoo (Challenge)**
save to an Archipelago multiworld. It subclasses AP's `CommonContext` (network
for free), detects in-game events → **location checks**, and applies received
**items** → game effects. All progression *logic* lives in the APWorld (Track B);
this client only consumes the shared [`data.json`](../data.json) contract.

## Layout

| module | role | needs game? |
|---|---|---|
| `data.py` | load + validate `data.json`; lookup tables | no |
| `state.py` | persisted high-water mark → **idempotent** item application (A3) | no |
| `effects.py` | `EffectApplier` + `ConsoleEffectApplier` (dry-run) | no |
| `client.py` | `CommonContext` subclass, manual-trigger console, goal detection (A1) | no |
| `memory/scanner.py` | pymem wrapper: AOB scan, pointer chains, typed r/w (A2) | yes |
| `memory/anchors.py` + `anchors.json` | the offset/signature table | — |
| `memory/applier.py` | `MemoryEffectApplier` — items → memory writes (A3) | yes |
| `memory/triggers.py` | `MemoryTriggerSource` — poll memory → checks (A3) | yes |

## Run

Setup (from project root). **Requires Python 3.11.9–3.13** — Archipelago's `ModuleUpdate`
hard-rejects 3.10, so build the venv with a 3.11+ interpreter:
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-clientA.txt
git clone --depth 1 https://github.com/ArchipelagoMW/Archipelago.git vendor/Archipelago
```
To build a distributable exe instead, see [`docs/PACKAGING.md`](../docs/PACKAGING.md).

**A1 — console mode (no game).** Connect to an AP server and drive checks by hand:
```powershell
.\.venv\Scripts\python.exe -m pz_ap_client.client <host:port> --name <slot>
```
Console commands: `/pz_check <name|id>` (fire a check), `/pz_locations [filter]`,
`/pz_items`, `/pz_goal`, plus all standard AP commands (`/received`, `!hint`, …).

**Full mode (game attached).** Apply items + detect checks via memory:
```powershell
.\.venv\Scripts\python.exe -m pz_ap_client.client <host:port> --name <slot> --memory
```
Requires `anchors.json` to be filled in — see
[`docs/A2_SPIKE_PLAYBOOK.md`](../docs/A2_SPIKE_PLAYBOOK.md).

**Filling `anchors.json` — `tools/memscan.py`.** Interactive scanner that replaces
Cheat Engine for the spike (you play the game and report values; it finds
addresses, pointer-scans for a stable chain, test-writes, and saves anchors):
```powershell
.\.venv\Scripts\python.exe -m tools.memscan
```

## Tests (no game, no server)
Whole suite under one runner (needs the 3.11 venv + `pip install pytest`):
```powershell
.\.venv\Scripts\python.exe -m pytest
```
`pytest.ini` scopes collection to `tests/` (never the vendored Archipelago tree). Each file also
still runs standalone as a script, e.g. `.\.venv\Scripts\python.exe tests\test_client_offline.py`.

## Status
- **A1 (client shell + console):** done, tested offline.
- **A3 (idempotent apply + goal + poll loop):** done, tested offline.
- **A2 (memory anchors):** scaffold done & tested; `anchors.json` awaiting the
  Cheat-Engine spike against the live game.
- **Integration with Track B:** pending both tracks.
