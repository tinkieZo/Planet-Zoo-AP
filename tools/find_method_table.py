"""find_method_table - locate IScenarioManager method-table entries (name/header/hash) + their native fn.

Frontier interned strings have a header at name-0x10: [04 04 00][len][4-byte hash]. The interface method
table may reference a method by char-ptr (name), header-ptr (name-0x10), OR by the 4-byte hash. For each
known method we sweep committed memory once for ALL those needles and, at each hit, flag any pointer into
the exe .text range within +-0x40 (= candidate native method fn). Hits where >1 method clusters in one
region with code ptrs = the IScenarioManager method table.

Pass the canonical string addrs found by find_iface_method:
    python -m tools.find_method_table IsTerrainEditDisabled:0x1AD68DF8:0x880201E1 \
        IsAddLakesDisabled:0x1ACD5BB8:0x55F7F5AC IsRemoveLakesDisabled:0x1AE865A8:0xB6C79334
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools._meminfo import enum_regions, READABLE  # noqa: E402


def _parse_specs(argv):
    """Parse `name:straddr_hex:hash_hex` args into (name, str_addr, hash32) tuples."""
    specs = []
    for a in argv:
        nm, sa, hh = a.split(":")
        specs.append((nm, int(sa, 16), int(hh, 16)))
    return specs


def _build_needles(specs):
    """Map each search needle (bytes) -> (method_name, kind), so one sweep covers all kinds at once."""
    needles = {}
    for nm, sa, hh in specs:
        needles[struct.pack("<Q", sa)] = (nm, "name")
        needles[struct.pack("<Q", sa - 0x10)] = (nm, "hdr-0x10")
        needles[struct.pack("<Q", sa - 0x18)] = (nm, "hdr-0x18")
        needles[struct.pack("<I", hh)] = (nm, "hash32")
    return needles


def _code_ptrs(win, base, hi):
    """Every qword in `win` that points into the exe .text range [base, hi) (= candidate native fn)."""
    cps = []
    for off in range(0, len(win) - 7):
        v = struct.unpack_from("<Q", win, off)[0]
        if base <= v < hi:
            cps.append(v)
    return cps


def _scan_region(data, rb, needles, base, hi, found):
    """Find every needle in one region; record (name, kind, addr, code-ptrs) for hits with a nearby fn."""
    for nd, (nm, kind) in needles.items():
        i = data.find(nd)
        cnt = 0
        while i != -1 and cnt < 200:
            cps = _code_ptrs(data[max(i - 0x40, 0):i + 0x48], base, hi)
            # hash32 matches are noisy, so require corroboration (>=2 nearby code ptrs);
            # pointer-kind needles are specific enough to accept on a single code ptr.
            if len(cps) >= (2 if kind == "hash32" else 1):
                found.append((nm, kind, rb + i, cps[:4]))
            cnt += 1
            i = data.find(nd, i + 1)


def _sweep(s, regs, needles, base, hi):
    """Sweep every region once, collecting candidate method-table entries across all needles."""
    found = []
    for k, (rb, rs) in enumerate(regs):
        if k % 2000 == 0:
            print("   %d/%d..." % (k, len(regs)), flush=True)
        if rs > 0x10000000:
            continue
        try:
            data = s.read_bytes(rb, rs)
        except Exception:
            continue
        _scan_region(data, rb, needles, base, hi, found)
    return found


def main() -> int:
    specs = _parse_specs(sys.argv[1:])
    if not specs:
        print("usage: name:straddr_hex:hash_hex ..."); return 1
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    hi = base + (getattr(s, "module_size", 0) or 0x10000000)
    regs = enum_regions(s.pm.process_handle, READABLE, max_size=0x10000000)
    print("sweeping %d regions for %d methods..." % (len(regs), len(specs)), flush=True)

    found = _sweep(s, regs, _build_needles(specs), base, hi)

    print("\n=== candidate method-table entries (with nearby code ptr) ===", flush=True)
    found.sort(key=lambda t: t[2])  # group by address (region proximity)
    for nm, kind, addr, cps in found[:120]:
        print("  %-22s %-9s @0x%X  code-ptrs: %s" % (nm, kind, addr, ", ".join("0x%X" % c for c in cps)), flush=True)
    print("total candidates: %d" % len(found), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
