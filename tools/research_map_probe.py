"""research_map_probe — dump the live research items-map: which species (welfare item-ids) are present.

Diagnoses why a PermitGate handle didn't resolve: is the map readable + populated, and is the species
actually in THIS zoo's research map? Prints the record count and, for every species in
SPECIES_WELFARE_ITEM, whether its item-id is present (+ its current handle).

    python -m tools.research_map_probe
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.research import ResearchReader, SPECIES_WELFARE_ITEM  # noqa: E402


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    r = ResearchReader(s)
    rs = r._research_system()
    print("research_system = %s" % (hex(rs) if rs else "NONE (chain didn't resolve — in a zoo?)"), flush=True)
    snap = r._snapshot()
    if not snap:
        print("snapshot FAILED — items map unreadable (not in a loaded zoo, or chain/offsets off)."); return 0
    by_item, by_handle = snap
    print("map records: %d occupied; %d distinct handles" % (len(by_item), len(by_handle)), flush=True)
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
    # show a sample of cat-7 (animal) item-ids actually present, to sanity-check the id space
    animal_items = sorted(i for i, v in by_item.items() if v[3] == 7)
    print("animal-research (cat 7) item-ids present (first 40): %s" % [hex(i) for i in animal_items[:40]], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
