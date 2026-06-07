"""validate_water_proxy — LIVE end-to-end check of the water_tools species-block proxy.

Installs the real PermitGate, gates the water_tools-gated aquatic species (nile_hippo + saltwater_croc),
resolves their CURRENT-session purchase handles (via the research map), and holds the block so you can
verify in-game. Phase 1 (~50s): both BLOCKED — try to buy Hippopotamus + Saltwater Crocodile (the buy
should do NOTHING, no spend) and a control species like Plains Zebra (should buy normally). Phase 2
(~35s): proxy reconciles as if water_tools (+ the croc permit) arrived -> both UNBLOCKED — buying works.
Restores the hook on exit (no permanent change).

    python -m tools.validate_water_proxy
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.permits import PermitGate  # noqa: E402

GATED = ["nile_hippo", "saltwater_croc"]


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached — is the game running with a zoo loaded?"); return 1
    gate = PermitGate(s)
    gate.set_gated(GATED)
    if not gate.ensure_installed():
        print("PermitGate did not install (buy-site bytes mismatch / game not ready)."); return 1
    print("PermitGate installed. Resolving current-session handles for the gated species...", flush=True)
    # reconcile(no unlocks) -> both gated species blocked; this also syncs the hook scratch
    gate.reconcile(set())
    handles = gate._blocked_handles()  # the handles actually written to the hook
    print("  blocked handles = %s" % [hex(h) for h in handles], flush=True)
    if not handles:
        print("  WARNING: no handles resolved (species not in this zoo's research map?). The gate can't\n"
              "  block species it can't resolve — load/visit a zoo where these species exist, or check\n"
              "  research.SPECIES_WELFARE_ITEM. Restoring.", flush=True)
        gate.shutdown(); return 0
    try:
        print("\n=== PHASE 1 (BLOCKED, ~50s) ===", flush=True)
        print(">>> Try to BUY: Hippopotamus + Saltwater Crocodile -> should DO NOTHING (no spend).", flush=True)
        print(">>> Control: buy a Plains Zebra (not gated) -> should buy NORMALLY.", flush=True)
        time.sleep(50)
        print("\n=== PHASE 2 (UNBLOCKED, ~35s) — proxy now sees water_tools (+ permit) as received ===", flush=True)
        gate.reconcile(set(GATED))  # mark both satisfied -> blocked set empty
        print("  blocked handles now = %s (expect empty)" % [hex(h) for h in gate._blocked_handles()], flush=True)
        print(">>> Try to BUY Hippopotamus + Saltwater Crocodile again -> should buy NORMALLY now.", flush=True)
        time.sleep(35)
    finally:
        gate.shutdown()
        print("\nRESTORED PermitGate. (No permanent change.)", flush=True)
    print("RESULT: if the gated buys were blocked in phase 1 and worked in phase 2, the water_tools\n"
          "proxy is LIVE-VALIDATED end-to-end.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
