"""rating_probe - DIAGNOSTIC (read-only): what the zoo_rating milestone actually sees.

The 'Zoo Rating - N' milestones fire on `anchors.read('zoo_rating') >= N`. That anchor is the clamp01
reputation float (0..1) scaled x5 to stars, so threshold 5 needs the raw float to hit EXACTLY 1.0 - its
clamped max. A zoo the UI shows as "5 stars" may sit at raw ~0.95-0.99 -> scaled ~4.75-4.95, which clears
1..4 but never 5. This prints the live raw + scaled value and which integer thresholds currently pass, so
we can confirm the miss and pick the right fix (display-rounding vs exact).

    python -m tools.rating_probe [seconds=30]

Run with your zoo at its current (supposedly 5-star) rating.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner   # noqa: E402
from pz_ap_client.memory.anchors import AnchorTable      # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1
    anchors = AnchorTable.load()
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        val = anchors.read(s, "zoo_rating")   # raw clamp01 / 0.2 == raw * 5 (stars)
        if val is None:
            print("  zoo_rating: unresolved (no zoo loaded / chain drift)")
        else:
            raw = val * 0.2
            passes = [n for n in range(1, 6) if val >= n]
            nearest_half = round(val * 2) / 2
            print("  zoo_rating raw(clamp01)=%.4f  stars=%.4f  displayed~%.1f  thresholds passing>= : %s"
                  % (raw, val, nearest_half, passes or "none"))
            if 5 not in passes:
                print("      -> 'Zoo Rating - 5' will NOT fire (needs stars>=5.0, i.e. raw==1.0 exactly)")
        time.sleep(1.0)
    print("probe done (read-only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
