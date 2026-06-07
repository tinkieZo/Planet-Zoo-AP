"""gen_aob — generate patch-robust AOB signatures for the client's hook sites (run against the live game).

For each hook site we read a window of bytes, disassemble it, and wildcard the VOLATILE operand bytes
(call/jmp rel32 targets and RIP-relative displacements — these move when the game is patched) while
keeping opcodes, modrm, struct-offset displacements and immediates (the code's identity). The result is a
signature unique within the module that re-finds the site wherever a patch relocates it. We verify each
pattern matches EXACTLY ONCE in the module before printing it for baking into signatures.py.

    python -m tools.gen_aob
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capstone  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.signatures import module_aob_scan  # noqa: E402

# name -> (rva, window length to read). Grow the pattern until it's unique in-module.
SITES = {
    "permit": (0xA0894E5, 80),
    "release": (0x5D84690, 96),       # function entry: generic prologue, needs a long window
    "birth_insert": (0xC82168, 80),
    "presence": (0x9E94863, 80),
    "research_start": (0xE461C6, 80),
}


def _wildcard_volatiles(insn) -> list:
    """Return insn's bytes as a list with the volatile operand bytes set to None: a call/jmp rel32
    target, or a RIP-relative disp32 (both end the instruction — our sites carry no trailing imm)."""
    b: list = list(insn.bytes)
    if insn.mnemonic in ("call", "jmp") and len(b) >= 5 and b[0] in (0xE8, 0xE9):
        for i in range(len(b) - 4, len(b)):
            b[i] = None
        return b
    for op in insn.operands:
        if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
            for i in range(max(len(b) - 4, 0), len(b)):
                b[i] = None
    return b


def gen_pattern(md, code: int, data: bytes, min_bytes: int):
    """Return (aob_string, n_bytes) — opcode/modrm/struct-offsets kept, rel32 + RIP-disp wildcarded.
    Emits whole instructions until at least min_bytes are consumed (for uniqueness)."""
    out: list = []
    consumed = 0
    for insn in md.disasm(data, code):
        out.extend(_wildcard_volatiles(insn))
        consumed += len(insn.bytes)
        if consumed >= min_bytes:
            break
    aob = " ".join("??" if x is None else "%02X" % x for x in out)
    return aob, len(out)


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    base = s.module_base
    print("# AOB signatures (verified unique in-module) — bake into signatures.py\n")
    for name, (rva, n) in SITES.items():
        try:
            data = s.read_bytes(base + rva, n)
        except Exception as e:
            print("  %-16s READ FAILED: %s" % (name, e)); continue
        # grow the pattern (more instructions) until it's unique in-module
        aob = blen = None
        for min_bytes in (24, 36, 48, 64):
            aob, blen = gen_pattern(md, base + rva, data, min_bytes)
            hits = module_aob_scan(s, aob, max_hits=4)
            if len(hits) == 1 and hits[0] == base + rva:
                uniq = "UNIQUE"; break
            uniq = ("%d hits" % len(hits)) if hits else "NOT FOUND"
        print('    "%s": "%s",  # rva 0x%X (%d B) [%s]' % (name, aob, rva, blen, uniq))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
