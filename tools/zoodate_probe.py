"""zoodate_probe - check ParkAgeReader against the live game: prints the completed years the park has
been open (vtable-scanned park-info +0x1c8) and whether that reads as a FRESH (Year 1) zoo. Use it to
confirm the anchor resolves this session and survives a restart, without rebuilding the exe.

    python -m tools.zoodate_probe
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.zoodate import ParkAgeReader, FRESH_YEARS  # noqa: E402


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached (is Planet Zoo running, in a loaded zoo?)"); return 1
    r = ParkAgeReader(s)
    years = r.read()
    print("park years open: %s   | fresh (< %d): %s" % (years, FRESH_YEARS, r.is_fresh()), flush=True)
    if years is None:
        print("No park-info instance resolved. Re-derive the vtable RVA with "
              "tools/parkage_probe.py --find-world / --parkinfo-vt and update signatures.PARKINFO_VTABLE_RVA.",
              flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
