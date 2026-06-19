# Packaging the client as a distributable exe (PyInstaller)

Produces a self-contained **one-dir** Windows build of the Planet Zoo Archipelago hooking client -
the AP network client + the memory/hook layer + a bundled Archipelago - that a player can run without
installing Python or Archipelago.

## Prerequisites

- **Python 3.11.x–3.13.x from python.org** (`py -3.11 -m venv .venv`; `ModuleUpdate` hard-rejects 3.10).
  python.org is recommended (build-exe.ps1 refuses `uv`/python-build-standalone interpreters), though the
  STALE-RUNTIME bug below was the real "GUI closes on startup" cause, not the interpreter.
- **CRITICAL (the "frozen GUI closes on startup, runs fine from source" bug):** PyInstaller bundles the
  *build machine's* Universal CRT (`ucrtbase.dll` + `api-ms-win-*.dll`). If that copy is older than the
  bundled Python 3.13 / kivy / SDL2 expect, those DLLs fail to load at frozen runtime and the GUI closes
  with no traceback (confirmed 2026-06-19 by diffing a working vs broken `_internal`: only the UCRT DLLs
  differed). The spec now **excludes the OS UCRT from the bundle** so the exe uses the target's current
  System32 UCRT (always present on Win10+, which the game requires). No action needed beyond using the
  current spec; VCRUNTIME140/MSVCP140 stay bundled.
  GPU note: the GUI defaults to the ANGLE (Direct3D) GL backend on Windows (`client.py`
  `KIVY_GL_BACKEND=angle_sdl2`) as a precaution; override with `KIVY_GL_BACKEND=glew`.
- The venv + deps + the vendored Archipelago tree, as set up in
  [`pz_ap_client/README.md`](../pz_ap_client/README.md):
  ```powershell
  py -3.11 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements-clientA.txt pyinstaller
  git clone --depth 1 https://github.com/ArchipelagoMW/Archipelago.git vendor/Archipelago
  ```
- **GUI + ovl-installer extras** (optional but expected for releases; `build-exe.ps1` warns
  about whichever is missing and the exe degrades gracefully without them):
  ```powershell
  # Kivy GUI (the standard AP client window):
  .\.venv\Scripts\pip install kivy==2.3.1 kivy_deps.sdl2 kivy_deps.glew
  .\.venv\Scripts\pip install "kivymd @ git+https://github.com/kivymd/KivyMD@5ff9d0d"
  # numpy - runtime dep of the vendored cobra-tools (the /pz_install inject engine):
  .\.venv\Scripts\pip install numpy
  ```
- A **cobra-tools** checkout for the in-client ovl installer. `build-exe.ps1` auto-finds a
  `cobra-tools-master` folder next to the repo (or pass `-CobraSource <dir>` /
  `$env:PZ_COBRA_SOURCE`) and stages a trimmed copy into `vendor\cobra-tools` (core code +
  Planet Zoo hash tables only - no docs/GUI/tests/other games' constants).

## Build

```powershell
.\build-exe.ps1
```
or directly:
```powershell
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean pz-ap-client.spec
```
Output: **`dist\pz-ap-client\`** (~85 MB) containing `pz-ap-client.exe` + `_internal\`.

### Bundling Archipelago from a different location

By default the build reads AP from `.\vendor\Archipelago`. To bundle an Archipelago install from
elsewhere (without cloning into `vendor/`), point the build at it - the bundle layout and the frozen
client's runtime path are unchanged; only the build-time *source* moves:
```powershell
.\build-exe.ps1 -ApSource D:\Archipelago
# or, equivalently, set the env var the spec reads:
$env:PZ_AP_SOURCE = 'D:\Archipelago'; .\.venv\Scripts\pyinstaller.exe --noconfirm --clean pz-ap-client.spec
```
The source must be a real Archipelago tree (contain `CommonClient.py`) and version-compatible with the
client. Note this only relocates the *build* source - inside the finished exe, AP always lives at
`_internal\vendor\Archipelago\` (the distributable is self-contained; you don't relocate it after building).

## How the bundle is structured (and why)

Archipelago discovers its game "worlds" dynamically at import time by scanning real folders
(`os.listdir` over `worlds/`), which a normal PyInstaller freeze breaks. So the build is a **hybrid**
(see [`pz-ap-client.spec`](../pz-ap-client.spec)):

- **Frozen** into the exe: the `pz_ap_client` package + its Python deps (websockets, pymem, orjson, …).
- **Shipped as real on-disk data** in `_internal\vendor\Archipelago\`: the whole Archipelago tree,
  *not* frozen. At startup `client.py` inserts that path onto `sys.path`, so AP imports from real
  files and world-discovery works exactly as from source. `data.json` and `anchors.json` are bundled
  where the code already looks for them (`Path(__file__).parent.parent / data.json`, etc.), so **no
  source changes were needed** - the existing path logic resolves to the bundle dir when frozen.

AP's runtime deps are declared as `hiddenimports` in the spec because the AP code that imports them
is loaded from data (not statically analyzed). pymem is pulled in via `collect_all`.

For the same reason, the spec force-includes the **entire standard library**
(`sys.stdlib_module_names`, minus heavy GUI/dev modules like `tkinter`): AP's stdlib imports - e.g.
`shlex` from `MultiServer` - are invisible to PyInstaller, so whether they get bundled is otherwise
*incidental* (pulled transitively by some dependency). That works on the machine that happened to
have the right transitive versions but fails as `No module named '<stdlib>'` on a fresh build
elsewhere (unpinned-dependency drift). Bundling all of stdlib makes the build deterministic across
machines.

## Run the exe

The intended end-user flow is **double-click - no command line** (the order doesn't matter;
the client idles until the AP scenario park is loaded):

1. Double-click **`pz-ap-client.exe`**. With the GUI bundled it opens the standard AP client
   window; enter server + slot there. (Console builds prompt instead.)
2. First time only: **`/pz_install`** - backs up your vanilla `Main.ovl` and builds + deploys
   the AP scenario shell *from your own game files* (a few minutes; game must be closed).
   The startup log always shows the current mod status (`/pz_mod` re-checks; `/pz_restore`
   puts vanilla back; a game update just means running `/pz_install` again).
3. **`/pz_launch`** starts Planet Zoo via Steam (with the scenario-intro skip flag). Pick the
   **ARCHIPELAGO** career entry - the client detects the AP park automatically and goes live.

Flags are optional and mainly for testing - anything passed skips the matching prompt:
```powershell
# Pre-fill the connection (skips both prompts), still attaches to the game:
dist\pz-ap-client\pz-ap-client.exe archipelago.gg:38281 --name Player1

# Console-only (A1): connect to AP but DON'T touch the game - manual-trigger console for testing:
dist\pz-ap-client\pz-ap-client.exe --name Player1 --no-memory
```
Memory attach (the default) requires `anchors.json` to be populated. On a clean exit the client
**restores every installed detour**; don't hard-kill it while attached or the game is left patched.

**GUI vs console:** release builds bundle Kivy/KivyMD, so the exe opens the standard AP client
window (`--nogui` forces the console). If the build venv lacked kivy, the exe falls back to the
AP **console** UI automatically: startup prompts handle the connection, `/connect <host:port>`
works at the prompt, and a harmless one-line `GUI unavailable … running headless console` notice
appears. First launch is a few seconds while AP discovers its worlds from the bundled tree -
that's normal.

## Validation status (2026-06-04)

- ✅ **Bundle integrity** - `pz-ap-client.exe --help` loads the bundled Archipelago and prints usage.
- ✅ **Frozen startup + network** - launched against a bogus server, the exe loads/validates
  `data.json`, builds the AP `CommonContext`, and connects via the bundled `websockets`
  (`Connecting to … → Connection refused`, as expected), then retries. The hard part (bundling AP)
  works end-to-end.
- ⏳ **`--memory` live attach** - *not* exercised against the running game on purpose: the first poll
  tick installs detour trampolines, and a test that hard-kills the process would leave the live game
  patched. The code path is byte-identical to the source build that was validated live this session,
  and pymem is bundled. Run it against the game with a real server (and exit cleanly) to confirm.
- ⏳ **Full end-to-end** (generate seed → host → connect → play) - **blocked on the Planet Zoo
  APWorld (Track B)**, which doesn't exist yet. Without it, AP can't generate a Planet Zoo seed and a
  server has no "Planet Zoo" slot to authenticate against. Procedure for when the apworld lands:
  1. Drop `planet_zoo.apworld` into `vendor/Archipelago/worlds/` (and into the bundle).
  2. Generate: `.\.venv\Scripts\python.exe vendor\Archipelago\Generate.py` with a Planet Zoo YAML.
  3. Host: `.\.venv\Scripts\python.exe vendor\Archipelago\MultiServer.py <multidata>`.
  4. Connect the exe: `pz-ap-client.exe localhost:38281 --name <slot> --memory` and play.
