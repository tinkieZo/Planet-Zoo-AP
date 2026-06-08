"""staff_train_hook - boost keeper training by +N by detouring GetStaffMemberCurrentTrainingLevel.

The getter (0x146B1B6F0) computes the level value into r8d at 0x146B1B85E, then stores it to the VM stack
at 0x146B1B870. We detour at 0x146B1B864 (after r8 is set): r8d += N, clamped to CAP. Because this getter
is called ~157x/sec (per-frame keeper logic, not just UI), boosting its return should raise effective
keeper training globally - no roster/map surgery. scratch[+0]=N (boost), scratch[+4]=CAP (max level).

    python -m tools.staff_train_hook [N=2] [cap=4] [hold_secs=90]
Open keeper panels + watch keeper behaviour during the hold; restores on exit.
"""
from __future__ import annotations
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.hook import HookManager  # noqa: E402

RVA = 0x6B1B864
ORIG = bytes.fromhex("488b4d10897 5f0".replace(" ", ""))  # mov rcx,[rbp+0x10]; mov [rbp-0x10],esi (7 bytes)
RESUME_RVA = 0x6B1B86B


def make_boost(region, scratch, resume, original):
    b = bytearray()
    b += b"\x9C\x50"                                  # pushfq; push rax
    b += b"\x48\xB8" + struct.pack("<Q", scratch)      # movabs rax, scratch
    b += b"\x8B\x08"                                  # mov ecx, [rax]    (N)
    b += b"\x41\x01\xC8"                              # add r8d, ecx
    b += b"\x8B\x48\x04"                              # mov ecx, [rax+4]  (CAP)
    b += b"\x41\x39\xC8"                              # cmp r8d, ecx
    b += b"\x7E\x03"                                  # jle +3 (skip clamp)
    b += b"\x41\x89\xC8"                              # mov r8d, ecx      (clamp to CAP)
    b += b"\x58\x9D"                                  # pop rax; popfq
    b += original                                     # mov rcx,[rbp+0x10]; mov [rbp-0x10],esi
    code = region + 0x40
    b += b"\xE9" + struct.pack("<i", resume - (code + len(b) + 5))
    return bytes(b)


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    hold = int(sys.argv[3]) if len(sys.argv) > 3 else 90
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    site = base + RVA
    cur = s.read_bytes(site, len(ORIG))
    if cur != ORIG:
        print("byte mismatch @0x%X: %s (expected %s)" % (site, cur.hex(), ORIG.hex())); return 1
    hm = HookManager(scanner=s)
    if not hm.install("sth", site, ORIG, lambda r, sc, res: make_boost(r, sc, base + RESUME_RVA, ORIG)):
        print("install failed"); return 1
    scratch = hm.scratch("sth")
    s.write_bytes(scratch, struct.pack("<I", n))
    s.write_bytes(scratch + 4, struct.pack("<I", cap))
    print("INSTALLED keeper-training boost @0x%X  N=+%d cap=%d" % (site, n, cap), flush=True)
    print(">>> Open keeper info/training panels - training level should read +%d (clamped %d)." % (n, cap), flush=True)
    print(">>> Also watch keeper work (welfare/abilities) for a behaviour change. Holding %ds..." % hold, flush=True)
    try:
        time.sleep(hold)
    finally:
        hm.restore_all()
        print("RESTORED.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
