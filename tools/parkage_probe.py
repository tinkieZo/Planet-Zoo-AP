"""parkage_probe - re-derive / verify the PARK-AGE fresh-save anchor (the completed-years-open counter;
see pz_ap_client/memory/zoodate.py + signatures.PARKINFO_VTABLE_RVA). Use it after a game patch shifts
the build-specific vtable RVA, or to confirm the signal on a save.

  --find-world       find the world/park object by its structural signature - a calendar at +0x88
                     (month 0..11), and a park-info object at +0xa8 that is a real in-module class with an
                     open-for-guests flag at +0x184 and the completed-years-open counter at +0x1c8 - and
                     print the PARK-INFO VTABLE RVA + yearsOpen. Run on an OLD and a FRESH save: same RVA =
                     a stable class; yearsOpen far larger on the old zoo = the age signal.
  --parkinfo-vt RVA  vtable-scan that class and read open(+0x184)/yearsOpen(+0x1c8) from each instance.
                     The open instance with the real count is the live park (the other is a static
                     template that always reads 0). This is exactly what ParkAgeReader does at runtime.

Then set signatures.PARKINFO_VTABLE_RVA to the confirmed RVA. Live read check: python -m tools.zoodate_probe.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory import signatures as sig  # noqa: E402

PROCESS_NAME = "PlanetZoo.exe"
_NOT_ATTACHED = "not attached (is Planet Zoo running, in a loaded zoo?)"
HEAP_LO, HEAP_HI = 0x10000, (1 << 47)
_OPEN_OFF = 0x184   # park-info open-for-guests flag (IsParkOpenForGuests)


def _looks_pointer(v) -> bool:
    return bool(v) and HEAP_LO < v < HEAP_HI


def _expand_frontier(s, frontier, out, cap):
    """One BFS level: every new pointer-looking qword in the frontier objects' first 0x600 bytes."""
    nxt = []
    for obj in frontier:
        try:
            blob = s.read_bytes(obj, 0x600)
        except Exception:
            continue
        for off in range(0, len(blob) - 8, 8):
            p = struct.unpack_from("<Q", blob, off)[0]
            if _looks_pointer(p) and p not in out and len(out) < cap:
                out.add(p)
                nxt.append(p)
    return nxt


def _reachable_objects(s, max_depth: int = 3, cap: int = 6000):
    """Heap objects reachable from the stable anchor roots (BFS, depth-limited)."""
    out = set()
    for rva in (0x2944690, 0x29446A0):
        slot = sig.resolve_root(s, rva)
        root_obj = s.read_qword(slot) if slot else None
        if not _looks_pointer(root_obj):
            continue
        out.add(root_obj)
        frontier, depth = [root_obj], 0
        while frontier and depth < max_depth and len(out) < cap:
            frontier, depth = _expand_frontier(s, frontier, out, cap), depth + 1
    return out


def _parkinfo_match(s, w, base, size):
    """If w looks like the world/park, return (pinfo, pvt_rva, month, openflag, yearsOpen); else None."""
    try:
        cal = s.read_qword(w + 0x88)
        if not _looks_pointer(cal):
            return None
        month = struct.unpack("<i", s.read_bytes(cal + 0x2E8, 4))[0]
        pinfo = s.read_qword(w + 0xA8)
        if not (0 <= month <= 11) or not _looks_pointer(pinfo):
            return None
        pvt = s.read_qword(pinfo)
        if not (base <= pvt < base + size):               # park-info must be a real in-module class
            return None
        openflag = s.read_bytes(pinfo + _OPEN_OFF, 1)[0]
        years = struct.unpack("<q", s.read_bytes(pinfo + sig.PARKINFO_PERIODS_OFF, 8))[0]
    except Exception:
        return None
    if openflag > 1 or not (0 <= years < 100000):
        return None
    return (pinfo, pvt - base, month, openflag, years)


def _do_find_world(s) -> None:
    base = s.module_base or 0
    size = getattr(s, "module_size", 0) or 0x10000000
    found = 0
    for w in _reachable_objects(s):
        m = _parkinfo_match(s, w, base, size)
        if m is None:
            continue
        pinfo, pvt_rva, month, openflag, years = m
        print("  WORLD=0x%X  parkinfo=0x%X pvt=rva:0x%X  month=%d open=%d  yearsOpen=%d"
              % (w, pinfo, pvt_rva, month, openflag, years), flush=True)
        found += 1
    print("found %d world/park candidate(s)" % found, flush=True)


def _do_parkinfo_vt(s, rva: int) -> None:
    target = s.module_base + rva
    hits = s.scan_heap_for_qword(target, max_hits=256)
    print("park-info vtable rva 0x%X (=0x%X) -> %d instance(s); open(+0x184)/yearsOpen(+0x1c8):"
          % (rva, target, len(hits)))
    for h in hits:
        try:
            openf = s.read_bytes(h + _OPEN_OFF, 1)[0]
            years = struct.unpack("<q", s.read_bytes(h + sig.PARKINFO_PERIODS_OFF, 8))[0]
        except Exception:
            continue
        if openf == 1 or years != 0:                       # skip inert/zero template instances
            print("  obj=0x%X  open=%d  yearsOpen=%d%s"
                  % (h, openf, years, "   <-- OPEN" if openf == 1 else ""), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="re-derive / verify the park-age fresh-save anchor")
    ap.add_argument("--find-world", action="store_true",
                    help="find the world/park via calendar(+0x88)+park-info(+0xa8) signature -> pvt RVA")
    ap.add_argument("--parkinfo-vt", type=lambda x: int(x, 0), metavar="RVA",
                    help="vtable-scan the park-info class and read open(+0x184)/yearsOpen(+0x1c8)")
    args = ap.parse_args()
    s = MemoryScanner(PROCESS_NAME)
    if not s.attach():
        print(_NOT_ATTACHED); return 1
    if args.parkinfo_vt is not None:
        _do_parkinfo_vt(s, args.parkinfo_vt)
    elif args.find_world:
        _do_find_world(s)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
