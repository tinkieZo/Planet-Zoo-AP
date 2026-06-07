"""terrain_gate_live — drive the real TerrainGate against the running game (final integration check).

Instantiates the client's TerrainGate with a live MemoryScanner and exercises the full path it uses in
the poll loop: locate main.2's bytecode, gate water (item NOT received), then enable it (received), then
restore. Re-enter terrain-edit mode at each prompt to confirm.

    python -m tools.terrain_gate_live [phase_secs=70]
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.terrain import TerrainGate  # noqa: E402


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 70
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    g = TerrainGate(s)
    g.set_gated({"water_tools"})
    print("gated tools:", g.gated_tools, flush=True)
    try:
        # Phase 1: water_tools NOT received -> water gated (greyed)
        ok = g.reconcile(set())
        print("reconcile(no items) -> located=%s addrs=%s" % (ok, [hex(a) for a in g._addrs]), flush=True)
        if not g._addrs:
            print("FAILED to locate main.2 bytecode (is a scenario loaded?)."); return 1
        print("\n=== PHASE 1 (~%ds): water GATED ===\n>>> Re-enter terrain edit mode — WATER should be greyed (Disabled by scenario)." % secs, flush=True)
        time.sleep(secs)
        # Phase 2: water_tools received -> water force-enabled
        g.reconcile({"water_tools"})
        print("\n=== PHASE 2 (~%ds): water ENABLED ===\n>>> Re-enter terrain edit mode — WATER should be usable again." % secs, flush=True)
        time.sleep(secs)
    finally:
        g.shutdown()
        print("\nshutdown -> bytecode restored.", flush=True)
    print("RESULT: if water greyed in phase 1 and re-enabled in phase 2, the integrated TerrainGate works.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
