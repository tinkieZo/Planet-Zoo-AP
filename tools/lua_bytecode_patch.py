"""lua_bytecode_patch — find TerrainEditUIMode main.2's loaded Lua bytecode + patch a tool's enabled flag.

The greying is decided in Lua main.2 (BuildCategories): each tool's `enabled = NOT b<X>Disabled`, one
`NOT` instruction per tool. This bypasses the (deeply reflection-dispatched) scenario-manager methods by
patching the bytecode directly: find main.2's 312-byte code array in the VM heap (it's loaded verbatim
from the .ovl), and overwrite one instruction with a constant LOADBOOL to force a tool enabled/disabled.
Deterministic, reversible, semi-live (effect on next terrain-mode entry).

main.2 SOURCE-flag instructions (byte offsets into the code array) — patching these forces the disabled
flag, so the tool greys WITH the "Disabled by scenario" tooltip (and un-greys cleanly when restored):
  0x08  GETTABLE R4 = bTerrainEditDisabled  (gates sculpt + stamp)   07014000
  0x0C  GETTABLE R5 = bLakeEditDisabled      (gates water)            47414000
Patch -> LOADBOOL R<A>, <gate>, 0 : gate=1 forces disabled=TRUE (tool GREYS), gate=0 forces FALSE (ENABLES).

    python -m tools.lua_bytecode_patch [byteoff_hex=0x0C] [gate=1|0] [hold_secs=120]
gate 1 = grey the tool (gated), 0 = force-enable it. Re-enter terrain mode during the hold to see it.
"""
from __future__ import annotations
import importlib.util
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools._meminfo import enum_regions, WRITABLE  # noqa: E402

LUA = "../ovl_extract/C0Main_terrain/editors.terrain.terrainedituimode.lua.bin"


def get_main2_code():
    spec = importlib.util.spec_from_file_location("lp", str(Path(__file__).resolve().parent / "luaparse.py"))
    lp = importlib.util.module_from_spec(spec); spec.loader.exec_module(lp)
    data = open(Path(__file__).resolve().parent.parent / LUA, "rb").read()
    r = lp.R(data); r.p = lp.HEADER_LEN; r.u8()
    protos = []; lp.read_function(r, protos)
    return [p for p in protos if p["path"] == "main.2"][0]["code"]


def main() -> int:
    byteoff = int(sys.argv[1], 16) if len(sys.argv) > 1 else 0x0C
    gate = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    hold = int(sys.argv[3]) if len(sys.argv) > 3 else 120
    code = get_main2_code()
    orig_ins = struct.unpack_from("<I", code, byteoff)[0]
    reg_a = (orig_ins >> 6) & 0xFF                                   # target register of the orig instr
    patch = struct.pack("<I", 3 | (reg_a << 6) | ((gate & 1) << 23))  # LOADBOOL R<A>, gate, 0
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    handle = s.pm.process_handle
    regs = enum_regions(handle, WRITABLE, max_size=0x10000000)
    print("searching %d writable regions for main.2 code (%d bytes)..." % (len(regs), len(code)), flush=True)
    hits = []
    for k, (rb, rs) in enumerate(regs):
        if k % 2000 == 0:
            print("   %d/%d..." % (k, len(regs)), flush=True)
        try:
            data = s.read_bytes(rb, rs)
        except Exception:
            continue
        i = data.find(code)
        while i != -1:
            hits.append(rb + i)
            i = data.find(code, i + 1)
    print("found %d copy(ies) of main.2 code: %s" % (len(hits), ", ".join("0x%X" % h for h in hits)), flush=True)
    if not hits:
        print("not found (bytecode may differ when loaded?). Aborting — no patch."); return 1
    orig = struct.pack("<I", orig_ins)
    print("patch target: code+0x%X  orig=%s -> %s (LOADBOOL R%d,%d)" % (byteoff, orig.hex(), patch.hex(), reg_a, gate), flush=True)
    try:
        for h in hits:
            s.write_bytes(h + byteoff, patch)
        print("PATCHED %d copy(ies). >>> RE-ENTER terrain edit mode (switch tool away + back) and observe." % len(hits), flush=True)
        print(">>> Expected (byteoff 0x%X, gate=%d): that tool %s." % (byteoff, gate, "GREYS (Disabled by scenario)" if gate == 1 else "ENABLES"), flush=True)
        print(">>> Holding %ds, then restoring..." % hold, flush=True)
        time.sleep(hold)
    finally:
        for h in hits:
            s.write_bytes(h + byteoff, orig)
        print("RESTORED original bytecode at %d copy(ies)." % len(hits), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
