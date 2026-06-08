"""research_map_probe - dump the live research items-map: which species (welfare item-ids) are present.

Diagnoses why a PermitGate handle didn't resolve OR why a birth wasn't attributed: is the research
chain resolving, is the items map readable + populated, and is the species actually in THIS zoo's
research map? Prints the record count and, for every species in SPECIES_WELFARE_ITEM, whether its
item-id is present (+ its current handle).

When the snapshot FAILS it also (a) prints the raw map header it read at the expected offset and says
which sanity check failed, and (b) scans a window of offsets from research_system for a plausible
hashmap header - so we can tell "not in a loaded zoo" (no candidate) from "the items-map offset moved
on this game build" (a candidate at a different offset).

    python -m tools.research_map_probe
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.research import (  # noqa: E402
    ResearchReader, SPECIES_WELFARE_ITEM, ITEMS_MAP_OFF,
    RESEARCH_CHAIN, RESEARCH_CHAIN_ALT,
)

HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def _walk(s: MemoryScanner, base: int, chain) -> Optional[int]:
    """Walk a static pointer chain, printing each hop; return the final address or None."""
    addr = base
    for off in chain:
        nxt = s.read_qword(addr + off)
        if not nxt:
            print("    +0x%X -> NULL (chain broke)" % off, flush=True)
            return None
        print("    +0x%X -> 0x%X" % (off, nxt), flush=True)
        addr = nxt
    return addr


def _read_header(s: MemoryScanner, rs: int, off: int):
    """Read a candidate hashmap header at rs+off; return (count, cap, buckets) or None on read error."""
    try:
        count = struct.unpack("<q", s.read_bytes(rs + off + 0x08, 8))[0]
        cap = struct.unpack("<q", s.read_bytes(rs + off + 0x10, 8))[0]
        bk = struct.unpack("<Q", s.read_bytes(rs + off + 0x18, 8))[0]
    except Exception:
        return None
    return count, cap, bk


def _is_plausible(count: int, cap: int, bk: int) -> bool:
    pow2 = cap > 0 and (cap & (cap - 1)) == 0
    return pow2 and 8 <= cap <= (1 << 16) and 0 < count <= cap and HEAP_LO < bk < HEAP_HI


def _explain_header(count: int, cap: int, bk: int) -> str:
    """One-line reason the items-map header at the expected offset failed validation."""
    why = []
    if not (cap > 0 and (cap & (cap - 1)) == 0):
        why.append("cap is not a positive power-of-two")
    if cap <= 0 or cap > (1 << 20):
        why.append("cap out of range")
    if not (HEAP_LO < bk < HEAP_HI):
        why.append("buckets pointer not in heap range")
    msg = "; ".join(why) or "values pass basic checks but read_bytes threw"
    if count == 0 or cap == 0:
        msg += "  (count/cap 0 usually means the zoo isn't fully loaded - run while IN the zoo)"
    return msg


def _scan_offsets(s: MemoryScanner, rs: int) -> int:
    """Print every rs+off in [0x40,0x400) that looks like a hashmap header; return how many."""
    found = 0
    for off in range(0x40, 0x400, 8):
        h = _read_header(s, rs, off)
        if h and _is_plausible(*h):
            found += 1
            tag = "  <-- EXPECTED" if off == ITEMS_MAP_OFF else "  <-- DIFFERENT OFFSET (build moved it?)"
            print("    candidate @ +0x%-4X count=%-4d cap=%-5d buckets=0x%X%s"
                  % (off, h[0], h[1], h[2], tag), flush=True)
    return found


def _diagnose(s: MemoryScanner, rs: int) -> None:
    """Explain a failed snapshot: dump the expected-offset header, then scan nearby offsets."""
    print("\n=== snapshot failed - diagnosing the items map @ research_system+0x%X ===" % ITEMS_MAP_OFF,
          flush=True)
    hdr = _read_header(s, rs, ITEMS_MAP_OFF)
    if hdr is None:
        print("  could not even READ rs+0x%X (research_system pointer is bad / unmapped)." % ITEMS_MAP_OFF,
              flush=True)
    else:
        print("  raw header: count=%d  cap=%d (0x%X)  buckets=0x%X" % (hdr[0], hdr[1], hdr[1], hdr[2]),
              flush=True)
        print("  -> failed because: %s" % _explain_header(*hdr), flush=True)
    print("\n  scanning rs+0x40 .. rs+0x400 for a plausible hashmap header (cap=pow2, sane count, heap"
          " buckets):", flush=True)
    if not _scan_offsets(s, rs):
        print("    none found - research_system is likely the WRONG object (chain stale for this build),"
              " or you're not in a loaded zoo.", flush=True)


def _report_chains(s: MemoryScanner, base: Optional[int]) -> None:
    """Walk both research chains, printing every hop, so a stale offset is visible at the break."""
    print("\n=== resolving research chains ===", flush=True)
    print("  primary chain %s:" % (tuple(hex(c) for c in RESEARCH_CHAIN),), flush=True)
    a = _walk(s, base, RESEARCH_CHAIN) if base else None
    print("  alt chain     %s:" % (tuple(hex(c) for c in RESEARCH_CHAIN_ALT),), flush=True)
    b = _walk(s, base, RESEARCH_CHAIN_ALT) if base else None
    print("  primary -> %s ; alt -> %s ; agree=%s"
          % (hex(a) if a else None, hex(b) if b else None, a == b and a is not None), flush=True)


def _dump_species(by_item: dict) -> None:
    print("\n=== SPECIES_WELFARE_ITEM presence in THIS map ===", flush=True)
    present = absent = 0
    for key, iid in sorted(SPECIES_WELFARE_ITEM.items()):
        rec = by_item.get(iid)
        if rec:
            present += 1
            print("  PRESENT  %-18s item=0x%-5X handle=0x%X level=%d status=%d cat=%d"
                  % (key, iid, rec[0], rec[1], rec[2], rec[3]), flush=True)
        else:
            absent += 1
            print("  ABSENT   %-18s item=0x%-5X  (no record in this zoo's map)" % (key, iid), flush=True)
    print("\n%d present, %d absent." % (present, absent), flush=True)
    animal_items = sorted(i for i, v in by_item.items() if v[3] == 7)
    print("animal-research (cat 7) item-ids present (first 40): %s" % [hex(i) for i in animal_items[:40]],
          flush=True)


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    print("module_base = 0x%X" % (s.module_base or 0), flush=True)
    _report_chains(s, s.module_base)

    r = ResearchReader(s)
    rs = r._research_system()
    print("\nresearch_system (used by reader) = %s"
          % (hex(rs) if rs else "NONE (chain didn't resolve - in a zoo?)"), flush=True)
    snap = r._snapshot()
    if not snap:
        if rs:
            _diagnose(s, rs)
        else:
            print("chain didn't resolve at all - likely not in a loaded zoo, or both chains are stale.",
                  flush=True)
        return 0
    by_item, by_handle = snap
    print("map records: %d occupied; %d distinct handles" % (len(by_item), len(by_handle)), flush=True)
    _dump_species(by_item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
