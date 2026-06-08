"""disasm_at - disassemble a window around given VAs (context for an xref hit).

    python -m tools.disasm_at 0x140B45D9A 0x1400B25E1 [before=0x40] [after=0xA0]
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capstone  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402


def main() -> int:
    args = list(sys.argv[1:])
    addrs = [int(a, 16) for a in args if a.lower().startswith("0x")]
    nums = [int(a) for a in args if not a.lower().startswith("0x")]
    before = nums[0] if len(nums) > 0 else 0x40
    after = nums[1] if len(nums) > 1 else 0xC0
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    for a in addrs:
        print("=" * 78, flush=True)
        start = a - before
        code = s.read_bytes(start, before + after)
        print("window around 0x%X:" % a, flush=True)
        for insn in md.disasm(code, start):
            mark = "  <==" if insn.address == a else ""
            print("  0x%X: %-9s %s%s" % (insn.address, insn.mnemonic, insn.op_str, mark), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
