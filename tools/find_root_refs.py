"""find_root_refs — find code instructions that reference the anchor-chain root globals.

The anchor pointer chains start at module globals base+0x2944690 / base+0x29446A0 — raw RVAs that shift on
a patch. To resolve them by signature instead, we need a code site that loads a root (or a near-root
cluster global) via `lea/mov reg,[rip+disp]`; signaturing that instruction (wildcarding the disp) +
RIP-resolving recovers the address wherever a patch moves it. This is how signatures.ROOT_CLUSTER was
built: a unique ref to the near-root cluster global base+0x2944450, plus a fixed delta to each root.

Two scan modes (re-run either if a future patch breaks the ROOT_CLUSTER signature and it needs re-deriving):

  (default)  byte-scan — fast hand-rolled match of 7-byte REX.W lea/mov/cmp `[rip+disp32]`; reports any ref
             landing within +-0x800 of a root, which reveals *near-root cluster globals* (a table base +
             offset). This proximity match is the mode that surfaced base+0x2944450.
  --disasm   capstone — disassembles .text and computes the true target of ANY rip-relative operand
             (non-REX movs, trailing-immediate instrs, cmp/add/...), but reports only EXACT root refs.
             More thorough per-instruction, but blind to near-root globals (the roots aren't directly
             referenced), so it complements rather than supersedes the byte-scan.

    python -m tools.find_root_refs [--disasm]
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capstone  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402

ROOTS = {0x2944690: "root_2944690", 0x29446A0: "root_29446A0"}
ROOTS_BY_NAME = {name: rva for rva, name in ROOTS.items()}
# REX.W prefixes whose 7-byte `[rip+disp32]` forms the byte-scan recognises.
PREFIXES = {b"\x48\x8d": "lea", b"\x4c\x8d": "lea", b"\x48\x8b": "mov", b"\x4c\x8b": "mov",
            b"\x48\x89": "mov[st]", b"\x4c\x89": "mov[st]", b"\x48\x3b": "cmp", b"\x48\x39": "cmp[st]"}


def text_section(s, base):
    """Parse the PE to return (rva, size) of .text."""
    hdr = s.read_bytes(base, 0x400)
    e_lfanew = struct.unpack_from("<I", hdr, 0x3C)[0]
    coff = s.read_bytes(base + e_lfanew, 0x18)
    nsec = struct.unpack_from("<H", coff, 6)[0]
    opt_size = struct.unpack_from("<H", coff, 0x14)[0]
    sect_off = e_lfanew + 0x18 + opt_size
    sects = s.read_bytes(base + sect_off, nsec * 0x28)
    for i in range(nsec):
        ent = sects[i * 0x28:(i + 1) * 0x28]
        if ent[:8].rstrip(b"\x00") == b".text":
            return struct.unpack_from("<I", ent, 12)[0], struct.unpack_from("<I", ent, 8)[0]
    return 0x1000, 0x8000000


# -- byte-scan mode (proximity; finds near-root cluster globals) --------------------------------------

def _record_byte_ref(data, i, addr, base, mn, targets, found) -> None:
    """If data[i:] is a 7-byte REX.W `[rip+disp32]` landing within +-0x800 of a root, record it."""
    if i + 7 > len(data) or (data[i + 2] & 0xC7) != 0x05:  # rip-relative => modrm & 0xC7 == 0x05
        return
    instr_addr = addr + i
    disp = int.from_bytes(data[i + 3:i + 7], "little", signed=True)
    tgt = instr_addr + 7 + disp
    for ta, name in targets.items():
        if abs(tgt - ta) <= 0x800 and len(found[name]) < 12:
            found[name].append((instr_addr - base, mn,
                                "tgt=base+0x%X(root%+d)" % (tgt - base, tgt - ta),
                                data[i:i + 16].hex()))


def _scan_bytes_chunk(data, addr, base, targets, found) -> None:
    for pref, mn in PREFIXES.items():
        i = data.find(pref)
        while i != -1:
            _record_byte_ref(data, i, addr, base, mn, targets, found)
            i = data.find(pref, i + 1)


def _scan_bytes(s, base, size, targets):
    """Byte-scan the whole module for REX.W rip-relative refs near a root."""
    found = {name: [] for name in targets.values()}
    chunk = 0x800000
    addr = base
    while addr < base + size:
        n = min(chunk + 16, base + size - addr)
        try:
            data = s.read_bytes(addr, n)
        except Exception:
            addr += chunk
            continue
        _scan_bytes_chunk(data, addr, base, targets, found)
        addr += chunk
    return found


# -- disasm mode (capstone; exact root refs of any opcode/length) -------------------------------------

def _record_disasm_op(insn, op, base, targets, found) -> None:
    """Record `insn` if `op` is a rip-relative memory operand whose target is exactly a root."""
    if op.type != capstone.x86.X86_OP_MEM or op.mem.base != capstone.x86.X86_REG_RIP:
        return
    name = targets.get(insn.address + insn.size + op.mem.disp)
    if name and len(found[name]) < 12:
        found[name].append((insn.address - base, "%s %s" % (insn.mnemonic, insn.op_str),
                            "", insn.bytes.hex()))


def _scan_disasm_chunk(md, data, addr, chunk, base, targets, found) -> None:
    for insn in md.disasm(data, addr):
        if insn.address >= addr + chunk:
            break
        for op in insn.operands:
            _record_disasm_op(insn, op, base, targets, found)


def _scan_disasm(s, base, targets):
    """Disassemble .text and record exact rip-relative refs to a root (any opcode/length)."""
    va, size = text_section(s, base)
    print("# .text rva=0x%X size=0x%X — disassembling for RIP refs to roots..." % (va, size), flush=True)
    found = {name: [] for name in targets.values()}
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    chunk = 0x400000
    addr = base + va
    end = base + va + size
    while addr < end and sum(len(v) for v in found.values()) < 24:
        n = min(chunk + 0x40, end - addr)
        try:
            data = s.read_bytes(addr, n)
        except Exception:
            addr += chunk
            continue
        _scan_disasm_chunk(md, data, addr, chunk, base, targets, found)
        addr += chunk
    return found


def _print_found(found) -> None:
    for name, refs in found.items():
        print("\n=== %s (base+0x%X) : %d ref(s) ===" % (name, ROOTS_BY_NAME[name], len(refs)))
        for rva, label, detail, bs in refs:
            print("  rva 0x%-9X %-8s %-30s bytes=%s" % (rva, label, detail, bs))


def main() -> int:
    use_disasm = "--disasm" in sys.argv[1:]
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    targets = {base + r: name for r, name in ROOTS.items()}
    if use_disasm:
        found = _scan_disasm(s, base, targets)
    else:
        size = getattr(s, "module_size", None) or 0x10000000
        found = _scan_bytes(s, base, size, targets)
    _print_found(found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
