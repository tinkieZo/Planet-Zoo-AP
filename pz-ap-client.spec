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
] + pm_hidden + _stdlib

# Where to READ the Archipelago tree at build time. Defaults to the vendored clone; override with
# the PZ_AP_SOURCE env var to bundle an Archipelago install from elsewhere (e.g.
# set PZ_AP_SOURCE=D:\Archipelago). The bundle DESTINATION stays "vendor/Archipelago" regardless, so
# the frozen client finds it at the same relative path - only the build-time source location changes.
AP_SOURCE = os.environ.get("PZ_AP_SOURCE", "vendor/Archipelago")
if not os.path.isfile(os.path.join(AP_SOURCE, "CommonClient.py")):
    raise SystemExit("pz-ap-client.spec: no Archipelago tree at %r (CommonClient.py not found). "
                     "Set PZ_AP_SOURCE to your Archipelago dir." % AP_SOURCE)

datas = (
    _tree(AP_SOURCE, "vendor/Archipelago")
    + [("data.json", "."), ("pz_ap_client/memory/anchors.json", "pz_ap_client/memory")]
    + pm_datas
)

a = Analysis(
    ['pz_client_main.py'],
    pathex=['.'],
    binaries=pm_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['kivy', 'tkinter'],
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
    strip=False,
    upx=False,
    upx_exclude=[],
    name='pz-ap-client',
)
