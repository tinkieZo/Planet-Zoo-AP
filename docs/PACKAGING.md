# Packaging the client as a distributable exe (PyInstaller)

Produces a self-contained **one-dir** Windows build of the Planet Zoo Archipelago hooking client —
the AP network client + the memory/hook layer + a bundled Archipelago — that a player can run without
installing Python or Archipelago.

## Prerequisites

- **Python 3.11.9–3.13** (Archipelago's `ModuleUpdate` hard-rejects 3.10). 
- The venv + deps + the vendored Archipelago tree, as set up in
  [`pz_ap_client/README.md`](../pz_ap_client/README.md):
  ```powershell
  py -3.11 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements-clientA.txt pyinstaller
  git clone --depth 1 https://github.com/ArchipelagoMW/Archipelago.git vendor/Archipelago
  ```

## Build

```powershell
.\build-exe.ps1
```
or directly:
```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean pz-ap-client.spec
```
Output: **`dist\pz-ap-client\`** (~85 MB) containing `pz-ap-client.exe` + `_internal\`.

## How the bundle is structured (and why)

Archipelago discovers its game "worlds" dynamically at import time by scanning real folders
(`os.listdir` over `worlds/`), which a normal PyInstaller freeze breaks. So the build is a **hybrid**
(see [`pz-ap-client.spec`](../pz-ap-client.spec)):

- **Frozen** into the exe: the `pz_ap_client` package + its Python deps (websockets, pymem, orjson, …).
- **Shipped as real on-disk data** in `_internal\vendor\Archipelago\`: the whole Archipelago tree,
  *not* frozen. At startup `client.py` inserts that path onto `sys.path`, so AP imports from real
  files and world-discovery works exactly as from source. `data.json` and `anchors.json` are bundled
  where the code already looks for them (`Path(__file__).parent.parent / data.json`, etc.), so **no
  source changes were needed** — the existing path logic resolves to the bundle dir when frozen.

AP's runtime deps are declared as `hiddenimports` in the spec because the AP code that imports them
is loaded from data (not statically analyzed). pymem is pulled in via `collect_all`.

## Run the exe

```powershell
# Console mode (A1) — no game needed; drive checks by hand:
dist\pz-ap-client\pz-ap-client.exe <host:port> --name <slot>

# Full mode — attach to a running PlanetZoo.exe, apply items + detect checks via memory:
dist\pz-ap-client\pz-ap-client.exe <host:port> --name <slot> --memory
```
`--memory` requires `anchors.json` to be populated. On a clean exit the client **restores every
installed detour**; don't hard-kill it while attached or the game is left patched.

## Validation status (2026-06-04)

- ✅ **Bundle integrity** — `pz-ap-client.exe --help` loads the bundled Archipelago and prints usage.
- ✅ **Frozen startup + network** — launched against a bogus server, the exe loads/validates
  `data.json`, builds the AP `CommonContext`, and connects via the bundled `websockets`
  (`Connecting to … → Connection refused`, as expected), then retries. The hard part (bundling AP)
  works end-to-end.
- ⏳ **`--memory` live attach** — *not* exercised against the running game on purpose: the first poll
  tick installs detour trampolines, and a test that hard-kills the process would leave the live game
  patched. The code path is byte-identical to the source build that was validated live this session,
  and pymem is bundled. Run it against the game with a real server (and exit cleanly) to confirm.
- ⏳ **Full end-to-end** (generate seed → host → connect → play) — **blocked on the Planet Zoo
  APWorld (Track B)**, which doesn't exist yet. Without it, AP can't generate a Planet Zoo seed and a
  server has no "Planet Zoo" slot to authenticate against. Procedure for when the apworld lands:
  1. Drop `planet_zoo.apworld` into `vendor/Archipelago/worlds/` (and into the bundle).
  2. Generate: `.\.venv\Scripts\python.exe vendor\Archipelago\Generate.py` with a Planet Zoo YAML.
  3. Host: `.\.venv\Scripts\python.exe vendor\Archipelago\MultiServer.py <multidata>`.
  4. Connect the exe: `pz-ap-client.exe localhost:38281 --name <slot> --memory` and play.
