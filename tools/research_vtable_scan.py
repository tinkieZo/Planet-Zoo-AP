"""research_vtable_scan - locate the research-system object by its VTABLE, independent of the fragile
master-root pointer chains (which miss in some saves/sessions - see research_map_probe).

A C++ object stores its vtable pointer at +0x000. In a save where the chains resolved correctly, the
research system's +0x000 was 0x1426c3490 (module rva 0x26C3490). This scans the writable heap for that
vtable pointer and validates the items map at +0xF8 - a chain-independent, layout-robust locator.

    python -m tools.research_vtable_scan [--rva 0x26C3490]

Run it in a loaded zoo - ideally one where research_map_probe FAILS. If it finds the research map here,
the vtable scan is the robust fix and gets wired into ResearchReader._research_system. If it finds 0
objects, re-run in a save where research_map_probe SUCCEEDS to confirm/correct the vtable rva.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.research import ITEMS_MAP_OFF  # noqa: E402
from tools._meminfo import enum_regions, WRITABLE  # noqa: E402

HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def _map_at(s: MemoryScanner, obj: int):
    """If obj+ITEMS_MAP_OFF is a readable items map, return (cap, buckets, occupied_records); else None."""
    try:
        cap = struct.unpack("<q", s.read_bytes(obj + ITEMS_MAP_OFF + 0x10, 8))[0]
        bk = struct.unpack("<Q", s.read_bytes(obj + ITEMS_MAP_OFF + 0x18, 8))[0]
    except Exception:
        return None
    if not (0 < cap <= (1 << 20) and HEAP_LO < bk < HEAP_HI):
        return None
    bm = ((cap >> 3) + 7) & ~7
    try:
        bitmap = s.read_bytes(bk, bm)
    except Exception:
        return None
    return cap, bk, sum(bin(b).count("1") for b in bitmap)


def _scan_vtable(s: MemoryScanner, vtable: int):
    """Every 8-aligned address in writable memory whose qword == vtable (i.e. an object's +0x000)."""
    needle = struct.pack("<Q", vtable)
    hits = []
    for base, size in enum_regions(s.pm.process_handle, WRITABLE, max_size=0x4000000):
        try:
            blob = s.read_bytes(base, size)
        except Exception:
            continue
        i = blob.find(needle)
        while i != -1:
            if i % 8 == 0:
                hits.append(base + i)
            i = blob.find(needle, i + 1)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="locate the research system by its vtable")
    ap.add_argument("--rva", default="0x26C3490", help="vtable rva (research_system+0x000 - module_base)")
    args = ap.parse_args()
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached (is Planet Zoo running, in a loaded zoo?)"); return 1
    rva = int(args.rva, 0)
    vtable = s.module_base + rva
    print("module_base=0x%X  vtable=0x%X (rva 0x%X)" % (s.module_base, vtable, rva), flush=True)
    hits = _scan_vtable(s, vtable)
    print("vtable pointer found on %d object(s)" % len(hits), flush=True)
    valid = []
    for h in hits:
        m = _map_at(s, h)
        if m:
            valid.append(h)
            print("  OBJECT 0x%X -> items map cap=%d records=%d buckets=0x%X  <-- research system"
                  % (h, m[0], m[2], m[1]), flush=True)
        else:
            print("  object 0x%X -> no valid items map at +0x%X" % (h, ITEMS_MAP_OFF), flush=True)
    print("", flush=True)
    if valid:
        print("SUCCESS: vtable-scan located the research system independent of the chains -> wiring it in.",
              flush=True)
    elif hits:
        print("Found the vtable but no valid map at +0x%X - the vtable rva may be wrong (capture "
              "research_system+0x000 in a save where research_map_probe SUCCEEDS) or the map moved." % ITEMS_MAP_OFF,
              flush=True)
    else:
        print("No objects with that vtable here. Re-run in a save where research_map_probe SUCCEEDS to "
              "confirm the rva (read research_system+0x000 there), or research isn't allocated in this state.",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
