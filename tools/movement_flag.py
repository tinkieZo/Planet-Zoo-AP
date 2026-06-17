"""movement_flag - capture the ACTIVE animals manager and force its player-animal-movement flag on.

Decisive test for conservation_release. Every script-side enable runs in the loading-world env and
never reaches the active-world animals API the release UI checks (animaldatabasetab.dec ln248:
`if not animals:IsPlayerAnimalMovementEnabled() then <must-stay-in-zoo block>`). This sets the flag
on the EXACT instance that gate reads, from the live (active) process:

  * the animals getter IsPlayerAnimalMovementEnabled (executor 0x145DF00C0) returns
    `*(*(rdi+0x80)+0x638) != 0`, where rdi is the animals manager resolved from the VM arg;
  * we detour `mov rcx,[rdi+0x80]` at 0x145DF0155 to capture rdi to scratch (push rax / store / pop
    rax / run original / jmp back - register-preserving, can't fault), then read the flag byte at
    [[mgr+0x80]+0x638] and write 1.

    python -m tools.movement_flag [seconds=60]

Run it, then OPEN the Animals panel in-game (that calls the getter -> captures the manager). The tool
sets the flag and restores its detour; reopen the panel and check whether 'Release to Wild' ungreys.
If it does, gate A is the lever and was only unreachable from the script env -> a client-side set is viable.
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.hook import HookManager        # noqa: E402

SITE = 0x145DF0155                          # mov rcx, [rdi+0x80]  (inside the animals getter)
ORIG = bytes.fromhex("488B8F80000000")      # the 7 original bytes
SUB_OFF = 0x80                              # rdi+0x80 -> sub-object
FLAG_OFF = 0x638                            # sub+0x638 -> the movement flag byte
HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def make_capture(region: int, scratch: int, resume: int, original: bytes) -> bytes:
    """Trampoline: stash rdi -> [scratch], run the original, jmp back. rax saved/restored."""
    code = region + 0x40
    body = b"\x50"                                       # push rax
    body += b"\x48\xB8" + struct.pack("<Q", scratch)     # mov rax, imm64(scratch)
    body += b"\x48\x89\x38"                              # mov [rax], rdi
    body += b"\x58"                                      # pop rax
    body += original                                     # mov rcx, [rdi+0x80]  (relocated)
    jmp_at = code + len(body)
    body += b"\xE9" + struct.pack("<i", resume - (jmp_at + 5))
    return body


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached")
        return 1
    cur = s.read_bytes(SITE, len(ORIG))
    if cur != ORIG:
        print(f"FAIL: unexpected bytes at 0x{SITE:X}: {cur.hex()} (expected {ORIG.hex()}) - RVA drift?")
        return 1
    hm = HookManager(s)
    if not hm.install("mvflag", SITE, ORIG,
                      lambda r, sc, res: make_capture(r, sc, res, ORIG)):
        print("FAIL: could not install capture detour")
        return 1
    scratch = hm.scratch("mvflag")
    print(f"capture detour @0x{SITE:X}; OPEN the Animals panel now ({secs}s)...")
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            mgr = int.from_bytes(s.read_bytes(scratch, 8), "little")
            if HEAP_LO < mgr < HEAP_HI:
                sub = int.from_bytes(s.read_bytes(mgr + SUB_OFF, 8), "little")
                if HEAP_LO < sub < HEAP_HI:
                    before = s.read_bytes(sub + FLAG_OFF, 1)[0]
                    s.write_bytes(sub + FLAG_OFF, b"\x01")
                    after = s.read_bytes(sub + FLAG_OFF, 1)[0]
                    print(f"\n*** captured animals mgr=0x{mgr:X} sub=0x{sub:X}")
                    print(f"    movement flag [sub+0x{FLAG_OFF:X}]: {before} -> {after}")
                    if after == 1:
                        print("    FLAG FORCED ON. Reopen the Animals panel and check Release to Wild.")
                    return 0
            time.sleep(0.05)
        print("\nno getter call captured (was the Animals panel opened?)")
        return 0
    finally:
        hm.restore("mvflag")
        print("capture detour restored.")


if __name__ == "__main__":
    raise SystemExit(main())
