"""menu_entry_trace - capture the script-natives main.1 calls BEFORE GetTerrainMenuConfig.

The terrain greying is computed in main.1 (decompiled): scenarioMgr:IsTerrainEditDisabled() (instr 25),
:IsRemoveLakesDisabled()/:IsAddLakesDisabled() (30-35), then GetTerrainMenuConfig() (instr 94). menu_build_
trace captures the calls AFTER GetTerrainMenuConfig; this one captures the calls BEFORE it: we ring-record
every getarg CALLER return-addr and FREEZE the ring the instant GetTerrainMenuConfig's getarg fires
(caller == 0x52A105). The ring then holds the ~64 native callsites leading up to it - including the Is*
methods' native impls (IF they're script-natives that call getarg). Disasm the unknown RVAs to find the
small bool-getter reading [obj+OFF] = the per-tool source field.

    python -m tools.menu_entry_trace [seconds=60]
PAUSE the sim first (less getarg noise), then exit + RE-ENTER terrain edit mode within the window.
"""
from __future__ import annotations
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.hook import HookManager  # noqa: E402

GETARG_RVA = 0xEE15E00
ORIG = bytes.fromhex("4c8b49204989c8")  # mov r9,[rcx+0x20]; mov r8,rcx (7 bytes)
TRIGGER_RVA = 0x52A105                   # return addr after GetTerrainMenuConfig's `call getarg`
RING = 256
RING_OFF = 0x200
IDX_OFF = 0x0
FROZEN_OFF = 0x4


def make_trace(region, scratch, resume, original, trigger_va):
    b = bytearray()
    b += b"\x9C\x50\x52\x51"                          # pushfq; push rax; push rdx; push rcx (0x20)
    b += b"\x48\xB8" + struct.pack("<Q", scratch)      # movabs rax, scratch
    b += b"\x83\x78\x04\x00"                          # cmp dword [rax+4], 0   (frozen?)
    jne_done = len(b); b += b"\x0F\x85\x00\x00\x00\x00"  # jne DONE (rel32, patch)
    b += b"\x48\x8B\x4C\x24\x20"                      # mov rcx, [rsp+0x20]    (caller ret addr)
    b += b"\x8B\x10"                                  # mov edx, [rax]         (idx)
    b += b"\x81\xE2\xFF\x00\x00\x00"                  # and edx, 0xFF          (RING-1=255)
    b += b"\x48\xC1\xE2\x03"                          # shl rdx, 3
    b += b"\x48\x89\x8C\x10" + struct.pack("<i", RING_OFF)  # mov [rax+rdx+RING_OFF], rcx
    b += b"\xFF\x00"                                  # inc dword [rax]        (idx++)
    b += b"\x48\xBA" + struct.pack("<Q", trigger_va)   # movabs rdx, trigger_va
    b += b"\x48\x39\xD1"                              # cmp rcx, rdx
    jne_skip = len(b); b += b"\x75\x00"               # jne DONE2 (skip freeze, rel8)
    b += b"\xC7\x40\x04\x01\x00\x00\x00"              # mov dword [rax+4], 1   (frozen=1)
    done_at = len(b)
    b += b"\x59\x5A\x58\x9D"                          # DONE: pop rcx; pop rdx; pop rax; popfq
    b += original
    code = region + 0x40
    b += b"\xE9" + struct.pack("<i", resume - (code + len(b) + 5))
    # patch branches to DONE
    struct.pack_into("<i", b, jne_done + 2, done_at - (jne_done + 6))
    b[jne_skip + 1] = (done_at - (jne_skip + 2)) & 0xFF
    return bytes(b)


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    site = base + GETARG_RVA
    trigger_va = base + TRIGGER_RVA
    if s.read_bytes(site, len(ORIG)) != ORIG:
        print("byte mismatch @0x%X: %s" % (site, s.read_bytes(site, len(ORIG)).hex())); return 1
    hm = HookManager(scanner=s)
    resume = site + len(ORIG)
    if not hm.install("met", site, ORIG, lambda r, sc, res: make_trace(r, sc, resume, ORIG, trigger_va)):
        print("install failed"); return 1
    scratch = hm.scratch("met")
    print("INSTALLED getarg freeze-trace @0x%X (trigger=0x%X)" % (site, trigger_va), flush=True)
    print(">>> PAUSE the sim, then EXIT + RE-ENTER terrain edit mode. Watching %ds (freezes at GetTerrainMenuConfig)..." % secs, flush=True)
    try:
        end = time.time() + secs
        frozen = 0
        while time.time() < end and not frozen:
            frozen = struct.unpack("<I", s.read_bytes(scratch + FROZEN_OFF, 4))[0]
            time.sleep(0.2)
        idx = struct.unpack("<I", s.read_bytes(scratch + IDX_OFF, 4))[0]
        ring = s.read_bytes(scratch + RING_OFF, RING * 8)
    finally:
        hm.restore_all()
        print("RESTORED. frozen=%d idx=%d" % (frozen, idx), flush=True)
    if not frozen:
        print("GetTerrainMenuConfig getarg never fired - terrain menu not entered? (try again, re-enter mode)"); return 0
    # temporal order: entries were written at positions (n & 63) for n=0..idx-1; last = idx-1 (the trigger)
    seq = []
    start = max(0, idx - RING)
    for n in range(start, idx):
        v = struct.unpack_from("<Q", ring, (n & (RING - 1)) * 8)[0]
        seq.append(v)
    print("=== getarg callers leading up to GetTerrainMenuConfig (oldest..newest) ===", flush=True)
    for i, v in enumerate(seq):
        rva = v - base if base <= v < base + 0x10000000 else None
        mark = "  <== TRIGGER GetTerrainMenuConfig" if rva == TRIGGER_RVA else ""
        print("  [%2d] 0x%X  %s%s" % (i, v, ("RVA 0x%X" % rva) if rva is not None else "(off-module)", mark), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
