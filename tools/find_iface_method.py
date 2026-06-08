"""find_iface_method - locate a Cobra interface-method's native impl via runtime string xref.

IScenarioManager methods (IsTerrainEditDisabled / IsAddLakesDisabled / IsRemoveLakesDisabled) are NOT in
the exe's strings (loaded from the .ovl reflection data at runtime) and NOT registrar bindings. But at
runtime the method table maps the name string -> native fn pointer. We scan committed memory for the name
string, then for 8-byte pointers TO that string (method-table entries), and dump each xref window flagging
any pointer into the exe .text range (0x140000000..module_hi) = candidate native method.

    python -m tools.find_iface_method <name> [more names ...]
"""
from __future__ import annotations
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools._meminfo import enum_regions, READABLE  # noqa: E402


def _collect_hits(data, rb, needle_list, hits) -> None:
    """Append every (capped) occurrence of each needle in one region's bytes to its hit list."""
    for nd in needle_list:
        i = data.find(nd)
        while i != -1:
            hits[nd].append(rb + i)
            if len(hits[nd]) > 64:
                break
            i = data.find(nd, i + 1)


def scan_needles(s, regs, needle_list, label):
    """Stream all regions once; return {needle: [addrs]} (capped at 65 per needle)."""
    hits = {nd: [] for nd in needle_list}
    for k, (rb, rs) in enumerate(regs):
        if k % 1500 == 0:
            print("   [%s] %d/%d regions..." % (label, k, len(regs)), flush=True)
        if rs > 0x10000000:  # skip >256MB buffers (textures/meshes)
            continue
        try:
            data = s.read_bytes(rb, rs)
        except Exception:
            continue
        _collect_hits(data, rb, needle_list, hits)
    return hits


def _build_ptr_needles(names, str_hits):
    """Pack 8-byte pointers to each string occurrence - the method-table-entry needles for pass 2."""
    needles = []
    for n in names:
        for sa in str_hits[n.encode()][:6]:
            needles.append(struct.pack("<Q", sa))
    return needles


def _dump_xref(s, base, hi, xa, sa) -> None:
    """Dump the qword window around one xref, flagging any pointer into the exe .text range."""
    try:
        win = s.read_bytes(xa - 0x20, 0x60)
    except Exception:
        return
    codeptrs = []
    for off in range(0, len(win) - 7, 8):
        v = struct.unpack_from("<Q", win, off)[0]
        if base <= v < hi:
            codeptrs.append((xa - 0x20 + off, v))
    if codeptrs:
        print("  xref@0x%X (str@0x%X) nearby code-ptrs: %s" % (
            xa, sa, ", ".join("[+0x%X]=0x%X" % (a - xa, v) for a, v in codeptrs)), flush=True)
    else:
        print("  xref@0x%X (str@0x%X) - no code ptr in +-0x20" % (xa, sa), flush=True)


def _report_name(s, base, hi, name, str_hits, xref_hits) -> None:
    """Print every string occurrence of `name` and the code-ptr windows around its xrefs."""
    straddrs = str_hits[name.encode()]
    print("\n=== %r : %d string occurrence(s) ===" % (name, len(straddrs)), flush=True)
    for sa in straddrs[:6]:
        print("  string @0x%X" % sa, flush=True)
    for sa in straddrs[:6]:
        ptr = struct.pack("<Q", sa)
        for xa in xref_hits.get(ptr, [])[:12]:
            _dump_xref(s, base, hi, xa, sa)


def main() -> int:
    names = sys.argv[1:] or ["IsTerrainEditDisabled"]
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    hi = base + (getattr(s, "module_size", 0) or 0x10000000)
    regs = enum_regions(s.pm.process_handle, READABLE, max_size=0x10000000)
    print("scanning %d committed regions (streaming, no cache)..." % len(regs), flush=True)

    # PASS 1: locate the name strings
    str_hits = scan_needles(s, regs, [n.encode() for n in names], "str")
    for n in names:
        print("PASS1 %r -> %d hit(s): %s" % (n, len(str_hits[n.encode()]),
              ", ".join("0x%X" % a for a in str_hits[n.encode()][:6])), flush=True)
    # PASS 2: locate pointers to each string occurrence (method-table entries)
    ptr_needles = _build_ptr_needles(names, str_hits)
    xref_hits = scan_needles(s, regs, ptr_needles, "xref") if ptr_needles else {}

    for name in names:
        _report_name(s, base, hi, name, str_hits, xref_hits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
