"""disasm - static RE toolkit for PlanetZoo.exe (capstone over a memory dump).

Dynamic value-scanning can't reveal *code structure* (which function mutates the
species roster on birth). This dumps the module image once, then works offline:

    python -m tools.disasm dump                 # snapshot module image -> .pz_module.bin
    python -m tools.disasm str Warthog          # find ASCII/UTF-16 occurrences
    python -m tools.disasm xref 0x14XXXXXXX      # RIP-relative refs to an address
    python -m tools.disasm dis 0x14XXXXXXX 30    # disassemble 30 instructions
    python -m tools.disasm func 0x14XXXXXXX      # find function start + disassemble

The xref scan uses the standard identity: a RIP-relative operand with disp32 at
file offset O targets ``base + O + 4 + disp32``; so refs to target T are offsets
where ``int32[O] == T - (base + O + 4)``, found vectorised with numpy (chunked).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402

BIN = Path(__file__).resolve().parent / ".pz_module.bin"
META = Path(__file__).resolve().parent / ".pz_module.meta"


def _dump() -> None:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return
    base, size = s.module_base, s.module_size or 0
    buf = bytearray()
    off = 0
    while off < size:
        chunk = min(0x1000000, size - off)
        try:
            buf += s.read_bytes(base + off, chunk)
        except Exception:
            buf += b"\x00" * chunk  # unreadable page -> zero-fill, keep offsets aligned
        off += chunk
    BIN.write_bytes(bytes(buf))
    META.write_text("%d %d" % (base, size))
    print("dumped module: base 0x%X, size %d -> %s" % (base, size, BIN.name))


def _load():
    base, size = (int(x) for x in META.read_text().split())
    return base, size, BIN.read_bytes()


def _cap():
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = False
    return md


def _str(needle: str) -> None:
    base, _, img = _load()
    for label, pat in (("ascii", needle.encode()), ("utf16", needle.encode("utf-16-le"))):
        start = 0
        n = 0
        while n < 20:
            i = img.find(pat, start)
            if i == -1:
                break
            print("  [%s] 0x%X" % (label, base + i))
            start = i + 1
            n += 1


def _xref(target: int) -> None:
    base, _, img = _load()
    b = np.frombuffer(img, dtype=np.uint8).astype(np.int64)
    n = b.size - 4
    K = target - base - 4
    hits = []
    step = 8_000_000
    for s0 in range(0, n, step):
        e0 = min(s0 + step, n)
        idx = np.arange(s0, e0)
        v = (b[s0:e0] | (b[s0 + 1:e0 + 1] << 8) | (b[s0 + 2:e0 + 2] << 16) | (b[s0 + 3:e0 + 3] << 24))
        v = v.astype(np.int32).astype(np.int64)        # sign-extend disp32
        match = np.nonzero(v == (K - idx))[0]
        for m in match.tolist():
            hits.append(base + s0 + m)
        if len(hits) > 60:
            break
    print("%d RIP-relative disp matches to 0x%X (disp at these addrs; instr starts ~1-3 before):" % (len(hits), target))
    md = _cap()
    for disp_addr in hits[:40]:
        off = disp_addr - base
        for back in (3, 2, 4, 1, 5, 6):       # try common instr prefixes before the disp
            ins = list(md.disasm(img[off - back:off - back + 12], disp_addr - back, count=1))
            if ins and (ins[0].address + ins[0].size) >= disp_addr:
                print("  0x%X: %s %s" % (ins[0].address, ins[0].mnemonic, ins[0].op_str))
                break


def _dis(addr: int, count: int) -> None:
    base, _, img = _load()
    off = addr - base
    md = _cap()
    for ins in md.disasm(img[off:off + count * 15], addr, count=count):
        print("  0x%X: %-10s %s" % (ins.address, ins.mnemonic, ins.op_str))


def _func(addr: int) -> None:
    base, _, img = _load()
    off = addr - base
    # scan back for a likely prologue boundary: int3 padding (CC CC) or a ret (C3) before.
    start = off
    for i in range(off, max(off - 0x800, 0), -1):
        if img[i - 1] == 0xCC and img[i] != 0xCC:
            start = i
            break
        if img[i - 2:i] == b"\xC3\xCC" or img[i - 1] == 0xC3 and img[i] == 0x55:
            start = i
            break
    print("function start ~0x%X (target 0x%X):" % (base + start, addr))
    _dis(base + start, 40)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: dump | str <s> | xref <hexaddr> | dis <hexaddr> [n] | func <hexaddr>")
        return 2
    cmd = argv[0]
    if cmd == "dump":
        _dump()
    elif cmd == "str":
        _str(argv[1])
    elif cmd == "xref":
        _xref(int(argv[1], 16))
    elif cmd == "dis":
        _dis(int(argv[1], 16), int(argv[2]) if len(argv) > 2 else 20)
    elif cmd == "func":
        _func(int(argv[1], 16))
    else:
        print("unknown:", cmd); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
