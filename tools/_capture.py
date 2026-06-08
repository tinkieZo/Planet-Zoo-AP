"""_capture - shared trampoline + polling helpers for the ring-capture RE tools.

scenario_mgr_capture / staff_train_capture install a read-only software detour that ring-captures a
register (the object the hooked code is about to use) into a scratch buffer, then read that ring out
each tick. The trampoline and the poll loop are identical except for the captured register, so they
live here. read_qword is the safe 8-byte reader those tools (and rules_capture_load) share.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.hook import HookManager  # noqa: E402

LO, HI = 0x10000, 0x7FFFFFFFFFFF   # plausible heap-pointer window
RING = 32
RING_OFF = 0x200

# REX prefix of `mov [rax+rcx*1+disp32], <reg64>` (opcode 89 /r, modrm BC, SIB 08), per captured register.
_RING_STORE_REX = {"rdi": 0x48, "r15": 0x4C}


def read_qword(scanner, a):
    """Read an unsigned 8-byte qword, or None on a failed read."""
    try:
        return struct.unpack("<Q", scanner.read_bytes(a, 8))[0]
    except Exception:
        return None


def make_ring_capture(region, scratch, resume, original, reg, ring_off=RING_OFF):
    """Build a read-only ring-capture trampoline: save flags+rax+rcx, store `reg` into a 32-slot ring at
    [scratch+ring_off] indexed by the counter at [scratch], bump the counter, run `original`, jmp `resume`."""
    rex = _RING_STORE_REX[reg]
    b = bytearray()
    b += b"\x9C\x50\x51"                                               # pushfq; push rax; push rcx
    b += b"\x48\xB8" + struct.pack("<Q", scratch)                      # movabs rax, scratch
    b += b"\x8B\x08"                                                   # mov ecx, [rax]    (idx)
    b += b"\x83\xE1\x1F"                                               # and ecx, 0x1F     (idx mod 32)
    b += b"\x48\xC1\xE1\x03"                                           # shl rcx, 3        (* 8)
    b += bytes([rex]) + b"\x89\xBC\x08" + struct.pack("<i", ring_off)  # mov [rax+rcx+ring_off], <reg>
    b += b"\xFF\x00"                                                   # inc dword [rax]
    b += b"\x59\x58\x9D"                                               # pop rcx; pop rax; popfq
    b += original                                                     # the displaced original instruction
    code = region + 0x40
    b += b"\xE9" + struct.pack("<i", resume - (code + len(b) + 5))     # jmp back to resume
    return bytes(b)


def install_ring_capture(name, rva, orig, resume_rva, reg):
    """Attach to PlanetZoo, verify `orig` at base+rva, and install a ring-capture detour there that
    captures `reg`. Returns (scanner, hook_manager, scratch) on success, or None (printing the reason).
    `resume_rva` is the module RVA to resume at after the displaced instruction (= rva + len(orig))."""
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return None
    base = s.module_base
    site = base + rva
    if s.read_bytes(site, len(orig)) != orig:
        print("byte mismatch @0x%X: %s" % (site, s.read_bytes(site, len(orig)).hex())); return None
    hm = HookManager(scanner=s)
    if not hm.install(name, site, orig, lambda r, sc, res: make_ring_capture(r, sc, base + resume_rva, orig, reg)):
        print("install failed"); return None
    return s, hm, hm.scratch(name)


def poll_ring(scanner, scratch, seen, ring_off=RING_OFF, ring=RING, lo=LO, hi=HI):
    """One poll: read the fire count and fold plausible ring pointers into `seen`. Returns the count."""
    cnt = struct.unpack("<I", scanner.read_bytes(scratch, 4))[0]
    if cnt:
        data = scanner.read_bytes(scratch + ring_off, ring * 8)
        for i in range(min(cnt, ring)):
            v = struct.unpack_from("<Q", data, i * 8)[0]
            if lo < v < hi:
                seen[v] = seen.get(v, 0) + 1
    return cnt
