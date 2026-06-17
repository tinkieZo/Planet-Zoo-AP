"""movement_patch - temporarily force IsPlayerAnimalMovementEnabled (animals) to return true.

Discovery aid for conservation_release. The AP scenario keeps player animal movement DISABLED
(the engine re-disables the flag at load even though our script enables it), so "Release to Wild"
is greyed ("Animal must stay in zoo for this scenario"). The trade-tab gate is exactly:

    editors.animal.animaltradecentretab line 180:  if not animals:IsPlayerAnimalMovementEnabled() then <hide release>

The animals getter (executor 0x145DF00C0) returns `*(*(mgr+0x80)+0x638) != 0`; the flag-read is
`movzx edx, byte ptr [rcx+0x638]` (0F B6 91 38 06 00 00) at 0x145DF016B. We overwrite that read with
`mov dl,1` + NOPs so the getter always reports enabled -> gate A passes -> Release to Wild appears.
ABI-safe (only the computed bool changes; the VM-push tail still runs) and fully reversible.

    python -m tools.movement_patch on     # force-enable (then open Animals panel, release works)
    python -m tools.movement_patch off     # restore the original flag-read

This is a LIVE probe aid only; the shipping fix lives in the scenario script. Restore (or restart the
game) when done. RVA may drift across game updates - the tool verifies the expected bytes first.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402

# Both IsPlayerAnimalMovementEnabled executors (animals + exhibits) - we don't know which one
# tWorldAPIs.animals resolves to, so force BOTH. Each is `movzx edx, byte ptr [rcx+OFF]` (7 bytes);
# overwrite with `mov dl,1 ; nop*5` so the getter always reports enabled. ABI-safe + reversible.
SITES = (
    (0x145DF016B, bytes.fromhex("0FB69138060000")),   # getter @0x145DF00C0  (flag [rcx+0x638])
    (0x1460AE738, bytes.fromhex("0FB6913A030000")),   # getter @0x1460AE690  (flag [rcx+0x33a])
)
PATCH = bytes.fromhex("B201") + b"\x90" * 5            # mov dl,1 ; nop*5  -> getter always true


def _apply(s, site, orig, mode) -> bool:
    cur = s.read_bytes(site, len(orig))
    want_from, want_to = (orig, PATCH) if mode == "on" else (PATCH, orig)
    if cur == want_to:
        print(f"  0x{site:X}: already {'patched' if mode == 'on' else 'restored'}.")
        return True
    if cur != want_from:
        print(f"  0x{site:X}: FAIL unexpected bytes {cur.hex()} (expected {want_from.hex()}) - RVA drift?")
        return False
    s.write_bytes(site, want_to)
    if s.read_bytes(site, len(orig)) != want_to:
        print(f"  0x{site:X}: FAIL write didn't take.")
        return False
    print(f"  0x{site:X}: {'PATCHED (forced TRUE)' if mode == 'on' else 'RESTORED'}.")
    return True


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mode not in ("on", "off"):
        print("usage: python -m tools.movement_patch on|off")
        return 2
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1
    ok = all(_apply(s, site, orig, mode) for site, orig in SITES)
    if mode == "on" and ok:
        print("BOTH movement getters forced TRUE.")
        print("  -> CLOSE then REOPEN the Animals panel (forces a list refresh) and check 'Release to Wild'.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
