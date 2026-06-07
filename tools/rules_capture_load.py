"""rules_capture_load — capture the scenario-RULES object at scenario LOAD via SetEnableTerrain, save a dump.

The scenario setup configures rules at load; SetEnableTerrain (0x145DDC510) writes the rules object at
0x145DDC649 `mov byte [rcx+0x6a1],al` (rcx = rules = scriptZoo+0x48). We hook that write, capture rcx,
verify rules+0x6a1 is a clean bool, and SAVE rules[0..0x2000] to tools/rules_<label>.bin. Run once per
scenario (reload with the hook armed) then diff two dumps to isolate a tool's per-tool data:
    snapshot A = this scenario (paint ON, water OFF); snapshot B = tutorial (paint ON, water ON)
    -> the byte(s) OFF in A and ON in B = the WATER tool's availability data.

    python -m tools.rules_capture_load <label> [seconds]
Arm it, then RELOAD / restart the scenario so SetEnableTerrain fires during setup.
"""
from __future__ import annotations
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.hook import HookManager  # noqa: E402
from tools._capture import read_qword  # noqa: E402

RVA = 0x5DDC649
ORIG = bytes.fromhex("8881a1060000")  # mov byte [rcx+0x6a1], al  (6 bytes)
TERRAIN_BYTE = 0x6A1
DUMP_LEN = 0x2000
LO, HI = 0x10000, 0x7FFFFFFFFFFF


def make_rules_capture(region, scratch, resume, original):
    body = bytearray()
    body += b"\x9C\x50"                              # pushfq; push rax (al = value, preserved)
    body += b"\x48\xB8" + struct.pack("<Q", scratch)  # movabs rax, scratch
    body += b"\x48\x89\x48\x08"                      # mov [rax+8], rcx   (rules)
    body += b"\xFF\x00"                              # inc dword [rax]
    body += b"\x58\x9D"                              # pop rax; popfq
    body += original                                 # mov byte [rcx+0x6a1], al
    code = region + 0x40
    body += b"\xE9" + struct.pack("<i", resume - (code + len(body) + 5))
    return bytes(body)


def _read_byte(s, a):
    """Read one byte, or -1 on failure."""
    try:
        return s.read_bytes(a, 1)[0]
    except Exception:
        return -1


def _try_capture(s, scratch, label):
    """One poll: if SetEnableTerrain has fired with a plausible rules ptr whose +0x6a1 is a clean bool,
    dump rules[0..DUMP_LEN] to tools/rules_<label>.bin. Returns the captured rules address, else 0."""
    fires = struct.unpack("<I", s.read_bytes(scratch, 4))[0]
    rules = read_qword(s, scratch + 8)
    if not (fires and rules and LO < rules < HI):
        return 0
    tb = _read_byte(s, rules + TERRAIN_BYTE)
    print("\n[fire %d] rules=0x%X  rules+0x6a1=%d  actions-count@+0xb04=%d" % (
        fires, rules, tb, (read_qword(s, rules + 0xB04) or 0) & 0xFFFFFFFF), flush=True)
    if tb not in (0, 1):
        return 0
    blob = s.read_bytes(rules, DUMP_LEN)
    out = Path(__file__).resolve().parent / ("rules_%s.bin" % label)
    out.write_bytes(blob)
    print("  SAVED %d bytes -> %s  (rules object confirmed)" % (len(blob), out), flush=True)
    return rules


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m tools.rules_capture_load <label> [seconds]"); return 1
    label = sys.argv[1]
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    site = s.module_base + RVA
    cur = s.read_bytes(site, len(ORIG))
    if cur != ORIG:
        print("byte mismatch @0x%X: %s (expected %s)" % (site, cur.hex(), ORIG.hex())); return 1
    hm = HookManager(scanner=s)
    if not hm.install("rload", site, ORIG, lambda r, sc, res: make_rules_capture(r, sc, res, ORIG)):
        print("install failed"); return 1
    scratch = hm.scratch("rload")
    print("INSTALLED SetEnableTerrain capture @0x%X scratch=0x%X" % (site, scratch), flush=True)
    print(">>> RELOAD / restart the scenario now so terrain rules are set during load. Watching %ds..." % secs, flush=True)

    captured = 0
    try:
        end = time.time() + secs
        while time.time() < end and not captured:
            captured = _try_capture(s, scratch, label)
            time.sleep(0.2)
    finally:
        fires = struct.unpack("<I", s.read_bytes(scratch, 4))[0]
        hm.restore_all()
        print("\nRESTORED. total fires=%d  captured=0x%X" % (fires, captured), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
