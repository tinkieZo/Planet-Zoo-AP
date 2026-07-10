"""offline_xrefs - RIP-relative code-xref finder for the ON-DISK PlanetZoo.exe (no live game).

PlanetZoo.exe is Denuvo-packed but its code section (.shared) is PLAINTEXT on disk, so xref
analysis works offline (see memory/exhibit-release-RE.md). Given a string (or a hex VA), this
finds every `<insn> [rip+disp32]` reference to it across all executable sections: a reference
at VA v satisfies v + 4 + disp32(v) == target, i.e. disp32 at file position i equals
(target_rva - rva(i) - 4) - a constant minus the index - which numpy checks in one vectorized
pass per section. Each hit is confirmed by disassembling a small window around it.

    python -m tools.offline_xrefs GetUnlockedSpeciesEnrichmentLevels
    python -m tools.offline_xrefs 0x142663260
    python -m tools.offline_xrefs <name> --exe <path-to-PlanetZoo.exe>

Purely file-based and read-only; safe with the game running.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

DEFAULT_EXE = r"K:\SteamLibrary\steamapps\common\Planet Zoo\PlanetZoo.exe"
IMAGE_BASE = 0x140000000
WINDOW_BACK = 0x30      # disasm context before each hit
WINDOW_FWD = 0x50


class StaticImage:
    """Minimal PE section map for VA<->file-offset over the on-disk exe."""

    def __init__(self, path: str):
        self.data = Path(path).read_bytes()
        d = self.data
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        nsec = struct.unpack_from("<H", d, pe + 6)[0]
        opt_size = struct.unpack_from("<H", d, pe + 20)[0]
        sec0 = pe + 24 + opt_size
        self.sections = []
        for k in range(nsec):
            off = sec0 + k * 40
            name = d[off:off + 8].rstrip(b"\x00").decode("latin-1")
            vsize, va, rawsz, rawptr = struct.unpack_from("<IIII", d, off + 8)
            chars = struct.unpack_from("<I", d, off + 36)[0]
            self.sections.append({"name": name, "va": va, "vsize": vsize,
                                  "raw": rawptr, "rawsz": rawsz,
                                  "exec": bool(chars & 0x20000000)})

    def va_of_off(self, off: int) -> "int | None":
        for s in self.sections:
            if s["raw"] <= off < s["raw"] + s["rawsz"]:
                return IMAGE_BASE + s["va"] + (off - s["raw"])
        return None

    def off_of_va(self, va: int) -> "int | None":
        rva = va - IMAGE_BASE
        for s in self.sections:
            if s["va"] <= rva < s["va"] + s["rawsz"]:
                return s["raw"] + (rva - s["va"])
        return None

    def find_string(self, name: str):
        """Yield the VA of every NUL-terminated occurrence of ``name`` in the file."""
        needle = name.encode("ascii") + b"\x00"
        i = self.data.find(needle)
        while i != -1:
            # require a NUL (or start) before, so 'FooBar' doesn't match inside 'GetFooBar'
            if i == 0 or self.data[i - 1] == 0:
                va = self.va_of_off(i)
                if va is not None:
                    yield va
            i = self.data.find(needle, i + 1)

    def rip_xrefs(self, target_va: int, chunk: int = 0x1000000):
        """Yield the VA of every position whose i32 equals the RIP displacement to target.
        Chunked (16 MB + 3-byte overlap) so the 300 MB .shared doesn't allocate GiB temporaries."""
        target_rva = target_va - IMAGE_BASE
        for s in self.sections:
            if not s["exec"]:
                continue
            for base in range(0, s["rawsz"], chunk):
                n = min(chunk + 3, s["rawsz"] - base)
                if n < 4:
                    continue
                raw = np.frombuffer(self.data, dtype=np.uint8,
                                    count=n, offset=s["raw"] + base).astype(np.int64)
                disp = raw[:-3] + (raw[1:-2] << 8) + (raw[2:-1] << 16) + (raw[3:] << 24)
                disp = (disp + 2**31) % 2**32 - 2**31      # sign-extend i32
                want = ((target_rva - 4 - s["va"] - base)
                        - np.arange(len(disp), dtype=np.int64))
                for i in np.nonzero(disp == want)[0]:
                    yield IMAGE_BASE + s["va"] + base + int(i)


def _disasm_window(img: StaticImage, hit_va: int) -> None:
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    start = hit_va - WINDOW_BACK
    off = img.off_of_va(start)
    if off is None:
        return
    code = img.data[off:off + WINDOW_BACK + WINDOW_FWD]
    # decode from several alignments; keep the one that decodes THROUGH the hit cleanly
    best = None
    for lead in range(16):
        insns = list(md.disasm(code[lead:], start + lead))
        cover = [x for x in insns if x.address <= hit_va < x.address + x.size]
        if cover:
            best = insns
            break
    if not best:
        print("     (no clean decode)")
        return
    for x in best:
        mark = "  <-- ref" if x.address <= hit_va < x.address + x.size else ""
        if x.address >= hit_va + 8 and not mark and x.address > hit_va + 0x28:
            break
        print("     0x%X: %-10s %s%s" % (x.address, x.mnemonic, x.op_str, mark))


def main() -> int:
    args = sys.argv[1:]
    exe = DEFAULT_EXE
    if "--exe" in args:
        i = args.index("--exe")
        exe = args[i + 1]
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return 1
    print("loading %s ..." % exe)
    img = StaticImage(exe)
    print("sections: %s" % ", ".join("%s(va 0x%X%s)" % (s["name"], s["va"], " X" if s["exec"] else "")
                                     for s in img.sections))
    for arg in args:
        if arg.lower().startswith("0x"):
            targets = [int(arg, 16)]
            print("\n=== xrefs to VA 0x%X ===" % targets[0])
        else:
            targets = list(img.find_string(arg))
            print("\n=== %r : %d on-disk string occurrence(s) ===" % (arg, len(targets)))
        for tva in targets:
            hits = list(img.rip_xrefs(tva))
            print("  target 0x%X -> %d rip-relative code ref(s)" % (tva, len(hits)))
            for h in hits:
                print("   ref @0x%X:" % h)
                _disasm_window(img, h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
