# -*- mode: python ; coding: utf-8 -*-
# PyInstaller one-dir build of the Planet Zoo Archipelago hooking client.
#
# Hybrid strategy: FREEZE the client (pz_ap_client) + its Python deps, but SHIP the vendored
# Archipelago tree as real on-disk data (NOT frozen). At runtime client.py inserts
# <bundle>/vendor/Archipelago onto sys.path, so AP imports from those real files and its dynamic
# world-discovery (os.listdir over real dirs) works exactly as from source. Because the AP code
# isn't statically analyzed, AP's runtime deps are declared as hidden imports below.
import os
import sys
from PyInstaller.utils.hooks import collect_all


def _tree(src_root, dest_root, skip=(".git", "__pycache__", ".pytest_cache")):
    """(src_file, dest_dir) tuples for every file under src_root, skipping junk dirs."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(os.path.dirname(full), src_root)
            out.append((full, os.path.join(dest_root, rel)))
    return out


# pymem dynamically loads submodules - collect everything it ships.
pm_datas, pm_binaries, pm_hidden = collect_all("pymem")

# GUI (kivy + kivymd): the vendored kvui imports both at runtime, invisible to static
# analysis. kivy's lazy-loading breaks PyInstaller's generic collect_all, so use kivy's
# OWN PyInstaller hooks (kivy.tools.packaging) for it; kivymd is a normal package and
# collect_all works. Without them the exe still builds - console UI fallback
# (client.py catches the ImportError).
gui_datas, gui_binaries, gui_hidden, gui_excludes = [], [], [], []
kivy_hookspath, kivy_runtime_hooks = [], []
GUI_BUNDLED = False
try:
    from kivy.tools.packaging.pyinstaller_hooks import (
        get_deps_minimal, hookspath as kv_hookspath, runtime_hooks as kv_runtime_hooks,
    )
    _kv = get_deps_minimal(camera=None, spelling=None, video=None)
    gui_hidden += _kv["hiddenimports"]
    gui_binaries += _kv.get("binaries", [])
    gui_excludes += _kv.get("excludes", [])
    kivy_hookspath = kv_hookspath()
    kivy_runtime_hooks = kv_runtime_hooks()
    # CLEAN-MACHINE FIX (2026-06-17): kivy's runtime hook points KIVY_DATA_DIR at <bundle>/data,
    # but the kivy PyInstaller hook does not reliably land kivy/data there - a clean-machine run
    # crashed with FileNotFoundError on _internal/data/glsl/header.vs (the default shader header).
    # Collect kivy's data dir (glsl/fonts/images/...) explicitly to "data" so the runtime path is
    # satisfied regardless of the hook's behavior.
    import kivy as _kv_mod
    _kv_data = getattr(_kv_mod, "kivy_data_dir", "")
    if _kv_data and os.path.isdir(_kv_data):
        gui_datas += _tree(_kv_data, "data")
    else:
        print("pz-ap-client.spec: WARNING kivy_data_dir not found (%r) - GUI shaders may be missing." % _kv_data)
    _d, _b, _h = collect_all("kivymd")
    gui_datas += _d
    gui_binaries += _b
    gui_hidden += _h
    GUI_BUNDLED = True
except Exception as e:  # noqa: BLE001 - any miss means "build without GUI"
    print("pz-ap-client.spec: kivy/kivymd not collectable (%s) - building console-only." % e)

# kivy's Windows runtime DLLs (SDL2 / glew) ship as separate dep wheels.
kivy_dep_trees = []
if GUI_BUNDLED:
    try:
        from kivy_deps import sdl2, glew
        kivy_dep_trees = [Tree(p) for p in (list(sdl2.dep_bins) + list(glew.dep_bins))]
    except Exception as e:  # noqa: BLE001
        print("pz-ap-client.spec: kivy_deps missing (%s) - GUI may lack SDL2/glew DLLs." % e)

# numpy: imported at runtime by the vendored cobra-tools (the ovl-inject child the
# installer spawns), invisible to static analysis. Without it the exe still builds,
# but /pz_install will fail - so warn loudly.
np_datas, np_binaries, np_hidden = [], [], []
try:
    np_datas, np_binaries, np_hidden = collect_all("numpy")
except Exception as e:  # noqa: BLE001
    print("pz-ap-client.spec: numpy not collectable (%s) - /pz_install will NOT work." % e)

# AP is loaded from data files at runtime, so PyInstaller can't see its imports via static analysis.
#  (1) Third-party deps (requirements-clientA.txt) - named explicitly.
#  (2) STDLIB imports (e.g. shlex via MultiServer): whether these get bundled otherwise is INCIDENTAL
#      - pulled in transitively by some dep on one machine but not another (unpinned-version drift),
#      which is exactly the intermittent "No module named shlex" on a fresh build elsewhere. So bundle
#      the whole stdlib (minus heavy GUI/dev modules we never use) to make AP's imports resolve
#      deterministically on every machine.
_STDLIB_SKIP = {"tkinter", "turtle", "turtledemo", "idlelib", "test", "lib2to3", "antigravity", "this"}
_stdlib = sorted(m for m in sys.stdlib_module_names if not m.startswith("_") and m not in _STDLIB_SKIP)

hidden = [
    "websockets", "yaml", "colorama", "pathspec", "jellyfish", "jinja2", "markupsafe",
    "schema", "platformdirs", "certifi", "orjson", "typing_extensions", "bsdiff4",
] + pm_hidden + gui_hidden + np_hidden + _stdlib

# Where to READ the Archipelago tree at build time. Defaults to the vendored clone; override with
# the PZ_AP_SOURCE env var to bundle an Archipelago install from elsewhere (e.g.
# set PZ_AP_SOURCE=D:\Archipelago). The bundle DESTINATION stays "vendor/Archipelago" regardless, so
# the frozen client finds it at the same relative path - only the build-time source location changes.
AP_SOURCE = os.environ.get("PZ_AP_SOURCE", "vendor/Archipelago")
if not os.path.isfile(os.path.join(AP_SOURCE, "CommonClient.py")):
    raise SystemExit("pz-ap-client.spec: no Archipelago tree at %r (CommonClient.py not found). "
                     "Set PZ_AP_SOURCE to your Archipelago dir." % AP_SOURCE)

# cobra-tools rides as on-disk data exactly like the Archipelago tree: the ovl-inject
# child puts <bundle>/vendor/cobra-tools on sys.path. Staged (trimmed) by build-exe.ps1;
# building without it just disables /pz_install in the shipped exe.
COBRA_SOURCE = os.environ.get("PZ_COBRA_SOURCE", "vendor/cobra-tools")
cobra_datas = []
if os.path.isfile(os.path.join(COBRA_SOURCE, "ovl_tool_cmd.py")):
    cobra_datas = _tree(COBRA_SOURCE, "vendor/cobra-tools")
else:
    print("pz-ap-client.spec: no cobra-tools at %r - /pz_install will NOT work in this build "
          "(run build-exe.ps1 to stage it, or set PZ_COBRA_SOURCE)." % COBRA_SOURCE)

datas = (
    _tree(AP_SOURCE, "vendor/Archipelago")
    + cobra_datas
    + [("data.json", "."), ("pz_ap_client/memory/anchors.json", "pz_ap_client/memory"),
       ("pz_ap_client/ovl_src", "pz_ap_client/ovl_src")]
    + pm_datas + gui_datas + np_datas
)

a = Analysis(
    ['pz_client_main.py'],
    pathex=['.'],
    binaries=pm_binaries + gui_binaries + np_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=kivy_hookspath,
    hooksconfig={},
    runtime_hooks=kivy_runtime_hooks,
    excludes=['tkinter'] + gui_excludes + ([] if GUI_BUNDLED else ['kivy', 'kivymd']),
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pz-ap-client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    *kivy_dep_trees,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='pz-ap-client',
)
