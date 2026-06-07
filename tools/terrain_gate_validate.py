"""terrain_gate_validate — PROVE the semi-live terrain-tool gate: write rules+0x6a1, re-enter mode, observe.

Decompiled TerrainEditUIMode (tools/luaparse) shows the greying is recomputed on every terrain-mode
ENTRY: main.1 calls scenarioMgr:IsTerrainEditDisabled() -> bTerrainEditDisabled, and main.2 sets
deformation.enabled = shapestamp.enabled = (not bTerrainEditDisabled). IsTerrainEditDisabled reads the
same byte SetEnableTerrain writes: rules+0x6a1 (rules = scriptZoo+0x48). So writing rules+0x6a1=0 then
RE-ENTERING terrain edit mode should grey SCULPT + STAMP ("Disabled by scenario").

This tool hooks SetEnableTerrain's write (fires at scenario load OR when you toggle the sandbox
"Enable Terrain" option), captures the live rules pointer, forces rules+0x6a1=0, holds so you can
exit+re-enter terrain mode and observe, then restores.

    python -m tools.terrain_gate_validate [hold_secs=75] [force=0]
Steps: run it; then RELOAD the scenario OR toggle sandbox Enable-Terrain once (to capture rules);
then EXIT terrain edit mode (pick scenery/paths) and RE-ENTER it. Report what greyed.
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
ORIG = bytes.fromhex("8881a1060000")  # mov byte [rcx+0x6a1], al
TERRAIN_BYTE = 0x6A1
LO, HI = 0x10000, 0x7FFFFFFFFFFF


def make_capture(region, scratch, resume, original):
    body = bytearray()
    body += b"\x9C\x50"                               # pushfq; push rax
    body += b"\x48\xB8" + struct.pack("<Q", scratch)   # movabs rax, scratch
    body += b"\x48\x89\x48\x08"                       # mov [rax+8], rcx
    body += b"\xFF\x00"                               # inc dword [rax]
    body += b"\x58\x9D"                               # pop rax; popfq
    body += original
    code = region + 0x40
    body += b"\xE9" + struct.pack("<i", resume - (code + len(body) + 5))
    return bytes(body)


def _capture_rules(s, scratch, timeout=300):
    """Poll the capture scratch until SetEnableTerrain fires with a rules ptr whose +0x6a1 is a clean
    bool; return that rules address, or 0 if nothing valid was captured within `timeout` seconds."""
    end = time.time() + timeout
    while time.time() < end:
        if struct.unpack("<I", s.read_bytes(scratch, 4))[0]:
            r = read_qword(s, scratch + 8) or 0
            if LO < r < HI and s.read_bytes(r + TERRAIN_BYTE, 1)[0] in (0, 1):
                return r
        time.sleep(0.15)
    return 0


def main() -> int:
    hold = int(sys.argv[1]) if len(sys.argv) > 1 else 75
    force = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    site = s.module_base + RVA
    if s.read_bytes(site, len(ORIG)) != ORIG:
        print("byte mismatch @0x%X" % site); return 1
    hm = HookManager(scanner=s)
    if not hm.install("tgv", site, ORIG, lambda r, sc, res: make_capture(r, sc, res, ORIG)):
        print("install failed"); return 1
    scratch = hm.scratch("tgv")
    print("INSTALLED SetEnableTerrain capture @0x%X" % site, flush=True)
    print(">>> RELOAD the scenario OR toggle the sandbox 'Enable Terrain' option once (to capture rules)...", flush=True)

    rules = 0
    orig = None
    try:
        rules = _capture_rules(s, scratch)
        if not rules:
            print("no SetEnableTerrain fire captured in 300s — did you reload/toggle?"); return 1
        time.sleep(2.5)  # let scenario setup finish any further SetEnableTerrain writes
        orig = s.read_bytes(rules + TERRAIN_BYTE, 1)[0]
        print("\nrules=0x%X  rules+0x6a1 (terrain-enable) currently = %d" % (rules, orig), flush=True)
        s.write_bytes(rules + TERRAIN_BYTE, bytes([force]))
        print("FORCED rules+0x6a1 = %d  (terrain %s)" % (force, "DISABLED" if force == 0 else "ENABLED"), flush=True)
        print(">>> NOW: exit terrain edit mode (click scenery/paths), then RE-ENTER terrain edit mode.", flush=True)
        print(">>> Expected: SCULPT + STAMP greyed ('Disabled by scenario'); water + paint unchanged.", flush=True)
        print(">>> Holding %ds, then restoring..." % hold, flush=True)
        time.sleep(hold)
    finally:
        if rules and orig is not None:
            s.write_bytes(rules + TERRAIN_BYTE, bytes([orig]))
            print("\nRESTORED rules+0x6a1 = %d." % orig, flush=True)
        hm.restore_all()
        print("hook restored.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
