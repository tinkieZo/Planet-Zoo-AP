"""xref_strings — find every `lea reg,[rip->X]` reference to given addresses across executable regions.

The terrain tool-name strings (registered by GetTerrainMenuConfig via reflection) are:
    deformation 0x142669E98 | painting 0x142669F08 | water 0x142636460 | shapestamp 0x142669F18
GetTerrainMenuConfig references them; ANY OTHER function that references them may be the per-tool
availability logic (the thing that decides "Disabled by scenario"). This scans all exec regions for
RIP-relative LEAs whose target is one of the given addresses and prints the referencing instruction
address (so we can disasm that function next).

    python -m tools.xref_strings 0x142669E98 0x142669F08 0x142636460 0x142669F18
    (no args -> uses the 4 terrain tool-name strings)
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capstone  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools._meminfo import enum_regions, EXEC  # noqa: E402

DEFAULTS = [0x142669E98, 0x142669F08, 0x142636460, 0x142669F18]
NAMES = {0x142669E98: "deformation", 0x142669F08: "painting", 0x142636460: "water", 0x142669F18: "shapestamp"}


def _rip_target(insn):
    """Effective address of insn's first RIP-relative memory operand, or None."""
    for op in insn.operands:
        if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
            return insn.address + insn.size + op.mem.disp
    return None


def _lea_hit(md, code, addr, tset):
    """Disassemble the first instruction at `code`; if it's a `lea reg,[rip+disp]` whose target is in
    tset, return (insn, target), else None (non-lea leading bytes are skipped, mirroring the scan)."""
    for insn in md.disasm(code, addr):
        if insn.mnemonic != "lea":
            continue
        tgt = _rip_target(insn)
        return (insn, tgt) if tgt in tset else None
    return None


def _scan_region(md, data, rbase, tset):
    """Scan one region for `lea reg,[rip->target]` (target in tset); print each, return the hit count."""
    hits = 0
    i = data.find(b"\x8D")            # LEA opcode
    while i != -1:
        start = max(i - 1, 0)         # include a possible REX prefix
        try:
            hit = _lea_hit(md, data[start:i + 8], rbase + start, tset)
        except Exception:
            hit = None
        if hit:
            insn, tgt = hit
            print("  0x%X: lea %s, [%s]" % (insn.address,
                  insn.reg_name(insn.operands[0].reg), NAMES.get(tgt, hex(tgt))), flush=True)
            hits += 1
        i = data.find(b"\x8D", i + 1)
    return hits


def main() -> int:
    targets = [int(a, 16) for a in sys.argv[1:]] or DEFAULTS
    tset = set(targets)
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    size = getattr(s, "module_size", None) or 0x30000000
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    regions = enum_regions(s.pm.process_handle, EXEC, lo=base, hi=base + size)
    print("targets:", [hex(t) for t in targets], flush=True)
    print("scanning %d exec regions for LEA refs..." % len(regions), flush=True)
    hits = 0
    for rbase, rsize in regions:
        try:
            data = s.read_bytes(rbase, rsize)
        except Exception:
            continue
        hits += _scan_region(md, data, rbase, tset)
    print("done — %d LEA references found" % hits, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
