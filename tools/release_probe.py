"""release_probe - discover where the released animal's SPECIES lives, to finish conservation_release.

The release executor (FUN_145D84690) resolves the animal through the cobra VM (a tagged-value handle),
so the species offset can't be read off the decompile alone. This probe finds it live, safely:

  * installs a PROBE-ONLY capture detour at the release entry that records rcx (the manager) +
    an entry counter (make_release_capture - captures one live register, no asm derefs -> can't fault;
    it does NOT gate, releases proceed normally);
  * polls fast; when a release fires (counter bumps) it grabs the manager pointer and SCANS the
    manager region + one level of pointed-to regions for any int that the symbol RegistryResolver
    resolves to a SPECIES name (== a species handle). Those hits, with their offsets, reveal the
    field the real ReleaseDetector should read for species attribution.

    python -m tools.release_probe [seconds=30]

Run it in the loaded ARCHIPELAGO scenario, then RELEASE one animal of a known species to the wild.
Paste the "SPECIES-HANDLE HITS" back; that pins the offset and I wire the per-species check.
Auto-restores the detour on exit (the release gate/count is unaffected - this is a separate probe hook).
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.hook import (HookManager, make_release_capture,  # noqa: E402
                                      RELEASE_CAP_COUNT, RELEASE_CAP_MGR)
from pz_ap_client.memory.registry import RegistryResolver  # noqa: E402
from pz_ap_client.memory.signatures import resolve_hook  # noqa: E402

HEAP_LO, HEAP_HI = 0x10000, (1 << 47)
SCAN_SPAN = 0x600          # bytes of the manager (and each pointee) to scan


def _u(scanner, addr, n=8):
    try:
        return int.from_bytes(scanner.read_bytes(addr, n), "little")
    except Exception:
        return None


def _read_blob(scanner, addr, n):
    try:
        return scanner.read_bytes(addr, n)
    except Exception:
        return None


def _species_name(reg, v32):
    """The interned species name for a plausible symbol-id value, else None."""
    if not (0x3000 <= v32 <= 0x60000):                # plausible species symbol-id range
        return None
    name = reg.id_to_name(v32)
    return name if (name and any(c.isalpha() for c in name)) else None


def _scan_blob(blob, label, reg, hits):
    """Append (label, off, value, name) species hits found in `blob` to `hits`; return the heap
    pointers (off, ptr) it contains (for one-level recursion)."""
    ptrs = []
    for off in range(0, SCAN_SPAN - 8, 4):
        name = _species_name(reg, struct.unpack_from("<I", blob, off)[0])
        if name:
            hits.append((label, off, struct.unpack_from("<I", blob, off)[0], name))
        if off % 8 == 0:
            p = struct.unpack_from("<Q", blob, off)[0]
            if HEAP_LO < p < HEAP_HI:
                ptrs.append((off, p))
    return ptrs


def _scan_for_species(scanner, reg, base, label, hits, seen):
    """Scan [base, base+SCAN_SPAN) for species-handle hits; return heap pointers found.
    `seen` guards against re-scanning / cycles. Returns [] when unreadable/already-seen."""
    if base in seen or not (HEAP_LO < base < HEAP_HI):
        return []
    seen.add(base)
    blob = _read_blob(scanner, base, SCAN_SPAN)
    return _scan_blob(blob, label, reg, hits) if blob is not None else []


def _install_probe(s):
    """Build the registry + install the capture detour. Returns (reg, hm, scratch) or None."""
    reg = RegistryResolver(s)
    if not reg.build_name_map():
        print("WARN: symbol registry empty (in a loaded zoo?) - species names won't resolve")
    resolved = resolve_hook(s, "release")
    if resolved is None:
        print("FAIL: release site unresolved (RVA stale / patched?)")
        return None
    site, orig = resolved
    hm = HookManager(s)
    if not hm.install("release_probe", site, orig,
                      lambda r, sc, res: make_release_capture(r, sc, res, orig)):
        print("FAIL: could not install probe detour")
        return None
    print(f"probe installed @0x{site:X}; RELEASE one animal now...")
    return reg, hm, hm.scratch("release_probe")


def _report_release(s, reg, count, scratch):
    """Scan the captured manager (+ one level of its pointers) for the released species + print."""
    mgr = _u(s, scratch + RELEASE_CAP_MGR, 8)
    print(f"\n*** release #{count} - manager=0x{(mgr or 0):X} - scanning ***")
    if not mgr:
        return
    hits, seen = [], set()
    for off, p in _scan_for_species(s, reg, mgr, "mgr", hits, seen):
        _scan_for_species(s, reg, p, f"mgr+0x{off:X}->*", hits, seen)
    if hits:
        print("  SPECIES-HANDLE HITS (path, offset, value, name):")
        for label, off, val, name in hits[:40]:
            print(f"    {label:18s} +0x{off:<4X} 0x{val:X}  {name!r}")
    else:
        print("  no species-handle hits in the manager region (selection likely already "
              "consumed; try releasing again / a different species)")


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached")
        return 1
    installed = _install_probe(s)
    if installed is None:
        return 1
    reg, hm, scratch = installed
    print(f"watching {secs}s...")
    last = 0
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            count = _u(s, scratch + RELEASE_CAP_COUNT, 4) or 0
            if count != last:
                last = count
                _report_release(s, reg, count, scratch)
            time.sleep(0.03)
    finally:
        hm.restore("release_probe")
        print("\nprobe detour restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
