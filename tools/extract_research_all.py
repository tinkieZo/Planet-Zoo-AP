"""extract_research_all - re-extract the research settings from EVERY source ovl into
tools/_research_extract, ready for tools.research_catalog:

    py -3.11 -m tools.extract_research_all
    py -3.11 -m tools.research_catalog

Sources: vanilla GameMain (the .apbak - the INSTALLED Main.ovl carries the AP patch, which
renames the animalresearch config) + every ContentN / ContentAnniversaryN pack (DLC species'
research settings live in their own pack ovl - the old GameMain-only extraction covered just
the 76 base species; with all packs the catalog holds the full roster, 210 as of 2026-07-13).
Content0 is skipped: base species research is all in GameMain, and loading the ~350MB base
content ovl costs minutes for nothing.

Wipes and repopulates tools/_research_extract (one flat dir - the catalog builder is
non-recursive); collisions across packs are reported loudly.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COBRA = REPO / "vendor" / "cobra-tools"
sys.path.insert(0, str(REPO))
from pz_ap_client.ovl import _ensure_oodle, find_game_dir  # noqa: E402
_ensure_oodle(COBRA)   # cobra loads the Oodle DLL at import time - stage it from the game first

sys.path.insert(0, str(COBRA))
import os
os.chdir(COBRA)   # cobra resolves its hash tables etc. relative to its root

import logging
if not hasattr(logging, "success"):   # cobra's GUI logging wrapper provides this; shim like ovl_tool_cmd
    logging.success = logging.getLogger("cobra-tools").info
logging.disable(logging.WARNING)      # silence cobra's plugin/round-trip noise

from generated.formats.ovl import OvlFile  # noqa: E402

OUT = REPO / "tools" / "_research_extract"
TYPES = [".animalresearchunlockssettings", ".animalresearchstartunlockedsettings",
         ".mechanicresearchsettings"]


def sources(ovldata: Path):
    gm = ovldata / "GameMain"
    vanilla = gm / "Main.ovl.apbak"
    yield ("GameMain(vanilla)" if vanilla.is_file() else "GameMain",
           vanilla if vanilla.is_file() else gm / "Main.ovl")
    for d in sorted(ovldata.iterdir()):
        if d.name.startswith("Content") and d.name != "Content0":
            p = d / "Main.ovl"
            if p.is_file():
                yield d.name, p


def main() -> int:
    game = find_game_dir()
    if game is None:
        print("FAIL: Planet Zoo install not found (set PZAP_GAME_DIR)")
        return 1
    ovldata = game / "win64" / "ovldata"
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    seen: dict = {}
    total = 0
    for label, path in sources(ovldata):
        ovl = OvlFile()
        try:
            ovl.load(str(path), {})
            tmp = OUT / "_tmp"
            tmp.mkdir(exist_ok=True)
            ovl.extract(str(tmp), only_types=TYPES)
        except Exception as e:
            print("FAIL %s (%s): %r" % (label, path.name, e))
            continue
        moved = 0
        # materialize before moving - mutating the tree under a lazy rglob skips entries
        for f in [p for p in tmp.rglob("*") if p.is_file() and p.suffix in TYPES]:
            dest = OUT / f.name
            if dest.exists():
                print("   COLLISION: %s from %s overwrites %s's copy" % (f.name, label, seen.get(f.name)))
            seen[f.name] = label
            shutil.move(str(f), str(dest))
            moved += 1
        shutil.rmtree(tmp, ignore_errors=True)
        total += moved
        print("%-22s -> %d research file(s)" % (label, moved))
    print("TOTAL: %d files in %s" % (total, OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
