"""movement_bytecode - PoC: patch ObjectiveManager.IsMovementForAnimalAllowed's loaded Lua bytecode
so it returns TRUE when bEnabled is false, unblocking Release-to-Wild (the gate-B / conservation_release
blocker). Same proven mechanism as TerrainGate (pz_ap_client/memory/terrain.py): the function's code
array is loaded verbatim into a writable VM heap region; we find it by byte signature and flip one
LOADBOOL.

IsMovementForAnimalAllowed (objectivemanager main.49) starts:
  0 GETTABLE R3=self.bEnabled | 1 TEST | 2 JMP->5 | 3 LOADBOOL R3,0(false) | 4 RETURN R3   (return false)
Instruction 3 is the `if not self.bEnabled then return false` path. Flipping its B field 0->1
(C3 00 00 00 -> C3 00 80 00) makes that path `return true`, so a disabled ObjectiveManager permits all
movement -> animaldatabasetab.dec ln252 `if not IsMovementForAnimalAllowed(...)` is false -> no
"must stay in zoo" -> release allowed. Instance/env-agnostic (the proto is shared).

    python -m tools.movement_bytecode [on|off]

Run a loaded zoo. Reopen the Animals panel after patching. Reversible (off restores).
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools.luaparse as lp                              # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner    # noqa: E402

OBJMGR_BIN = Path(__file__).resolve().parent / "_decomp" / "content0" / "components.objectivemanager.lua.bin"
FN_PATH = "main.49"
# Patch the FUNCTION ENTRY to return TRUE unconditionally (covers bEnabled-true paths + any
# GetMoveableAnimals restriction). instr0 := LOADBOOL R3,1,0 ; instr1 := RETURN R3,2.
# R0=self,R1=nAnimalID,R2=sDest are params; R3 is the first free register.
PATCH_INSTR = 0
ORIG_INSTR = bytes.fromhex("C7004000")   # instr0: GETTABLE A=3 B=R0 C=K0('bEnabled')
ENTRY_PATCH = bytes.fromhex("C3008000") + bytes.fromhex("E6000001")  # LOADBOOL R3,1,0 ; RETURN R3,2
ENTRY_ORIG_LEN = 8                        # we save/restore instr0+instr1

_MEM_COMMIT = 0x1000
_PAGE_GUARD = 0x100
# All readable protections, NO size cap - the function can live in >1 Lua state (the working career
# scenario showed 2 copies); we must patch EVERY copy or the release UI may use an unpatched one.
_WRITABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}


def _fn_code() -> bytes:
    data = OBJMGR_BIN.read_bytes()
    r = lp.R(data)
    r.p = lp.HEADER_LEN
    r.u8()
    protos = []
    lp.read_function(r, protos, "main")
    for fn in protos:
        if fn["path"] == FN_PATH:
            return fn["code"]
    raise SystemExit(f"FAIL: {FN_PATH} not found in {OBJMGR_BIN.name}")


def _writable_regions(handle):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.VirtualQueryEx.restype = ctypes.c_size_t

    class _MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]
    mbi = _MBI(); addr = 0
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        if (mbi.State == _MEM_COMMIT and not (mbi.Protect & _PAGE_GUARD)
                and (mbi.Protect & 0xFF) in _WRITABLE and mbi.RegionSize < 0x40000000):
            yield base, mbi.RegionSize
        addr = base + mbi.RegionSize


def _patch_region(s, rb: int, rs: int, sig: bytes, sig_off: int, orig8: bytes, want: bytes):
    """Patch every copy of the function in one region. Returns (located, patched)."""
    try:
        data = s.read_bytes(rb, rs)
    except Exception:
        return 0, 0
    found = patched = 0
    i = data.find(sig)
    while i != -1:
        fnstart = i - sig_off                     # the sig sits at offset sig_off within the function
        cur = data[fnstart:fnstart + ENTRY_ORIG_LEN]
        if cur in (orig8, ENTRY_PATCH):
            found += 1
            if cur != want:
                s.write_bytes(rb + fnstart, want)
                patched += 1
        i = data.find(sig, i + 1)
    return found, patched


def _patch_all(s, sig: bytes, sig_off: int, orig8: bytes, want: bytes):
    """Scan every writable region; patch all copies. Returns (located, patched)."""
    found = patched = 0
    for rb, rs in _writable_regions(s.pm.process_handle):
        f, p = _patch_region(s, rb, rs, sig, sig_off, orig8, want)
        found += f
        patched += p
    return found, patched


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "on").lower()
    code = _fn_code()
    orig8 = code[:ENTRY_ORIG_LEN]                 # instr0+instr1 originals (from the file)
    assert code[:4] == ORIG_INSTR, f"instr0 is {code[:4].hex()}, expected {ORIG_INSTR.hex()}"
    sig_off = 16                                  # locate via instr4+ (skips the earlier instr3 PoC patch)
    sig = code[sig_off:sig_off + 48]
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached")
        return 1
    want = ENTRY_PATCH if mode == "on" else orig8
    found, patched = _patch_all(s, sig, sig_off, orig8, want)
    verb = "forced return-true" if mode == "on" else "restored"
    print(f"IsMovementForAnimalAllowed: {found} copy(ies) located, {patched} {verb}.")
    if mode == "on" and patched:
        print("  -> reopen the Animals panel; 'Release to Wild' should now be available.")
    elif found == 0:
        print("  -> not found (zoo loaded? bytecode differs?).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
