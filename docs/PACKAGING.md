# Packaging the client as a distributable exe (PyInstaller)

Produces a self-contained **one-dir** Windows build of the Planet Zoo Archipelago hooking client -
the AP network client + the memory/hook layer + a bundled Archipelago - that a player can run without
installing Python or Archipelago.

## Prerequisites

- **Python 3.11.x–3.13.x from python.org** (`py -3.13 -m venv .venv313`; `ModuleUpdate` hard-rejects 3.10).
  python.org is recommended (build-exe.ps1 refuses `uv`/python-build-standalone interpreters), though the
  STALE-RUNTIME bug below was the real "GUI closes on startup" cause, not the interpreter.
  `build-exe.ps1` picks `.venv313` over `.venv` when both are staged (3.12+ clears AP ModuleUpdate's
  "security issues" warning, which no installable 3.11 can satisfy); `-VenvPath <dir>` forces one.
- **The "frozen GUI closes on startup, runs fine from source" bug, now auto-guarded.** The bundle is
  self-contained, so PyInstaller bundles the C/C++ runtime (`MSVCP140.dll`, `VCRUNTIME140.dll`,
  `ucrtbase.dll`, `api-ms-win-*.dll`). The trap: PyInstaller may grab a **stale** copy that a binary wheel
  vendored into the build env (e.g. `numpy.libs\msvcp140-<hash>.dll`, which is `14.40`) or an old SDK on
  PATH *even when the machine's actual System32 runtime is current*. Python 3.13 / kivy / SDL2 then fail
  to load against that stale copy when frozen and the GUI closes silently (no window, no traceback,
  exit 0). **Confirmed 2026-06-19**: a build whose bundled `MSVCP140.dll` was `563,944 B` (an old version)
  closed on startup; transplanting a current copy into `_internal` made the window stay open, proving the
  bundled runtime, not the interpreter/GPU/AP tree, was the cause.
  - **`build-exe.ps1` now auto-fixes this:** after building it refreshes each bundled runtime DLL from
    `System32` whenever System32's copy is newer (strictly version-gated, it never downgrades a build made
    on an up-to-date machine), then runs a GUI self-test to confirm the runtime loads. So the only
    requirement is a **current System32 runtime**, which **Windows Update** keeps current.
  - **If the VC++ Redistributable installer reports `0x80070666` "Another version of this product is
    already installed", that's FINE, not an error to fight.** It means your System32 VC++ runtime is
    *already current*, which is exactly what the build-time refresh needs. No reinstall required.
  - We do **not** strip the runtime from the bundle: a self-contained build (current runtime included) is
    what's verified to run on clean targets. Relying on the *target's* System32 runtime instead was tried
    and **failed** on a target whose own System32 copy was stale.
  - GPU note: the GUI defaults to the ANGLE (Direct3D) GL backend on Windows (`client.py`
    `KIVY_GL_BACKEND=angle_sdl2`) as a precaution; override with `KIVY_GL_BACKEND=glew`.
- The venv + deps + the vendored Archipelago tree, as set up in
  [`pz_ap_client/README.md`](../pz_ap_client/README.md):
  ```powershell
  py -3.11 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements-clientA.txt pyinstaller
  git clone --depth 1 https://github.com/ArchipelagoMW/Archipelago.git vendor/Archipelago
  ```
  **A venv is NOT relocatable**, if you move or rename the workspace, the `Scripts\*.exe` launchers
  (`pip.exe`, `pyinstaller.exe`) keep the *old* absolute python path baked in and fail with
  `Fatal error in launcher: Unable to create process`. After moving, **recreate the venv** in place
  (`py -3.13 -m venv .venv` + reinstall deps), or invoke tools as `.\.venv\Scripts\python.exe -m <tool>`
  (which `build-exe.ps1` now does for PyInstaller, so the build itself survives a move).
- **GUI + ovl-installer extras** (optional but expected for releases; `build-exe.ps1` warns
  about whichever is missing and the exe degrades gracefully without them):
  ```powershell
  # Kivy GUI (the standard AP client window):
  .\.venv\Scripts\pip install kivy==2.3.1 kivy_deps.sdl2 kivy_deps.glew
  .\.venv\Scripts\pip install "kivymd @ git+https://github.com/kivymd/KivyMD@5ff9d0d"
  # numpy - runtime dep of the vendored cobra-tools (the /pz_install inject engine):
  .\.venv\Scripts\pip install numpy
  ```
- A **cobra-tools** checkout for the in-client ovl installer. `build-exe.ps1` uses `-CobraSource <dir>` /
  `$env:PZ_COBRA_SOURCE` / a `cobra-tools-master` folder next to the repo, and **auto-clones
  `OpenNaja/cobra-tools` if none is found** (same self-vendoring as the Archipelago tree, so a fresh build
  machine isn't silently shipped without `/pz_install`). It stages a trimmed copy into `vendor\cobra-tools`
  (core code + Planet Zoo hash tables only - no docs/GUI/tests/other games' constants).
  - **Oodle is NOT bundled.** cobra-tools needs the proprietary `oo2core_8_win64.dll` (RAD/Epic) to
    read/write Planet Zoo ovls; we don't redistribute it. `build-exe.ps1` strips it from the staged tree,
    and the client copies it from the user's **own** Planet Zoo install (which ships the byte-identical
    DLL) into cobra's fixed path at `/pz_install` (`ovl._ensure_oodle`). Caveat: that copy writes into the
    client's `_internal\vendor\cobra-tools\...`, so if the client is unzipped into a **protected folder**
    (e.g. `Program Files`) the first `/pz_install` will error asking you to move it somewhere writable.

## Build

```powershell
.\build-exe.ps1
```
or directly:
```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean pz-ap-client.spec
```
Output: **`dist\pz-ap-client\`** (~85 MB) containing `pz-ap-client.exe` + `_internal\`.

**Automatic guards against the stale-runtime bug** (run by `build-exe.ps1` after a successful build):
- It **refreshes the bundled runtime from System32** (`MSVCP140*`, `VCRUNTIME140*`, `ucrtbase`,
  `concrt140`, `vccorlib140`) whenever System32's copy is newer (version-gated, never downgrades), so a
  stale copy PyInstaller grabbed from a wheel (`numpy.libs`) or an old SDK is replaced with the current one
  the machine actually runs with. This is the core fix; the two checks below confirm it.
- It logs the **bundled C/C++ runtime versions** (`ucrtbase.dll`, `MSVCP140.dll`, `VCRUNTIME140*.dll`) so
  the build log records exactly what shipped, a future "works on my build, not theirs" question is then
  a one-line diff instead of a multi-day hunt.
- It runs a **GUI self-test**: `pz-ap-client.exe --selftest` loads the SDL2/GL native stack *using the
  bundled runtime* and prints `PZAP_SELFTEST: OK`. Because the frozen exe uses its own bundled runtime,
  this reproduces the target failure **on the build machine**, a stale runtime fails here, loudly, with
  the fix printed, instead of silently closing on a user's PC. Bypass with `.\build-exe.ps1 -SkipSelfTest`
  (e.g. a headless CI agent with no display, where window creation can't succeed).

### Release zip (Linux/mac-safe entry paths)

```powershell
.\build-exe.ps1 -Version 0.1.4    # emits pz-ap-client-v0.1.4-win64.zip after the build
```
**Never zip `dist\` with `Compress-Archive`** - it writes **backslash** entry names (`pz-ap-client\_internal\...`).
Windows unpackers tolerate that, but Linux/mac take the `\` literally and unpack a flat pile of
weirdly-named files instead of folders. The build script zips via Python's `zipfile` with forced `/`
separators and an explicit entry for empty dirs (`custom_worlds`), which `os.walk`-over-files misses.

**Verifying a zip is trickier than it looks:** Python's `zipfile` *normalizes* `\` to `/` when reading,
so `namelist()` reports a broken zip as clean. Check the raw central-directory bytes instead:
```powershell
py -c "d=open('pz-ap-client-v0.1.4-win64.zip','rb').read(); import sys; sys.exit(1 if d.count(b'client\x5c_internal') else print('clean'))"
```
(`\x5c` is a literal backslash; any hit means the zip is broken for non-Windows unpackers.)

### Bundling Archipelago from a different location

By default the build reads AP from `.\vendor\Archipelago`. To bundle an Archipelago install from
elsewhere (without cloning into `vendor/`), point the build at it - the bundle layout and the frozen
client's runtime path are unchanged; only the build-time *source* moves:
```powershell
.\build-exe.ps1 -ApSource D:\Archipelago
# or, equivalently, set the env var the spec reads:
$env:PZ_AP_SOURCE = 'D:\Archipelago'; .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean pz-ap-client.spec
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
