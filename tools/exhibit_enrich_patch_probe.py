"""exhibit_enrich_patch_probe - manual CLI for the exhibit-enrichment Lua patch (RE + live checks).

PRODUCTIZED 2026-07-10 as pz_ap_client.memory.exhibit_enrich.ExhibitEnrichmentGate (the client
drives it from the received Progressive Exhibit Enrichment count) - the code arrays, patch windows
and variants live THERE now; this probe is a thin manual driver over the same constants for live
verification/debugging. Full RE story (native map, two consumer sites, GO result) in that module's
docstring; original derivation in memory/exhibit-enrichment-gate.md.

    python -m tools.exhibit_enrich_patch_probe            # locate copies + decode patch windows
    python -m tools.exhibit_enrich_patch_probe --set 1    # unlocked = (level <= 1), both sites
    python -m tools.exhibit_enrich_patch_probe --restore  # write the original instructions back
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner                    # noqa: E402
from pz_ap_client.memory.exhibit_enrich import ExhibitEnrichmentGate, SITES, MAX_LEVEL  # noqa: E402


def _label(site, window: bytes) -> str:
    if window == site.orig:
        return "ORIGINAL"
    for n in range(0, MAX_LEVEL + 1):
        if window == site.variant(n):
            return "PATCH set=%d" % n
    return "UNKNOWN?!"


def _parse_args(args) -> tuple:
    """(restore, setn) from the CLI args; setn None unless --set N given."""
    restore = "--restore" in args
    setn = int(args[args.index("--set") + 1]) if "--set" in args else None
    return restore, setn


def _report_site(s, site, copies) -> None:
    """Print the heap copies of one site's code array + decode each patch window."""
    print("%s code array: %d heap cop%s: %s" % (
        site.name, len(copies), "y" if len(copies) == 1 else "ies",
        ", ".join(hex(a) for a in copies)))
    for a in copies:
        cur = s.read_bytes(a + site.off, site.len)
        print("   @0x%X window: %s  [%s]" % (a, cur.hex(), _label(site, cur)))


def _write_site(s, site, copies, blob: bytes, desc: str) -> None:
    """Write ``blob`` into each copy's patch window, skipping any with unexpected bytes."""
    for a in copies:
        cur = s.read_bytes(a + site.off, site.len)
        if cur not in site.known:
            print("   %s @0x%X SKIPPED (unexpected bytes - not touching)" % (site.name, a))
            continue
        s.write_bytes(a + site.off, blob)
        print("   %s @0x%X -> %s" % (site.name, a, desc))


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    restore, setn = _parse_args(sys.argv[1:])

    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1
    gate = ExhibitEnrichmentGate(s)
    hits = gate._find()
    for site in SITES:
        _report_site(s, site, hits[site.name])
    if not all(hits[site.name] for site in SITES):
        print(">>> not loaded - open an exhibit's info panel once, then re-run.")
        return 1

    if restore or setn is not None:
        desc = "RESTORED" if restore else "PATCHED set=%d" % setn
        for site in SITES:
            _write_site(s, site, hits[site.name], site.orig if restore else site.variant(setn), desc)
        print("done. REOPEN the exhibit info panel to re-run both functions and see the effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
