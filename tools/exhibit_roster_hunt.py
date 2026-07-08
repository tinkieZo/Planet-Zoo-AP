"""exhibit_roster_hunt - DIAGNOSTIC (read-only): find an exhibit-manager census that INCLUDES stored
animals, so storage releases can be attributed by census-diff (the +0x2A0 id-set is dead - returns None).

The placed census (+0x318, {species_handle -> count}) works but excludes stored/trade-center animals, so a
storage release doesn't drop it. This scans the manager object mgr+0..+SPAN (qword stride) for OTHER maps
with the SAME {species_handle -> count} layout, and flags any whose contents are a SUPERSET of the placed
census - that candidate also counts stored animals and is the structure to diff for storage releases.

NO in-game action needed - the probe reads passively. For a useful result:
  * have some exhibit animals PLACED, and at least ONE in STORAGE (buy one from the exhibit market and
    leave it unplaced, or move a placed one to storage). A stored-inclusive map then visibly exceeds the
    placed census.
  * to CONFIRM the winner: run once, RELEASE the stored animal, run again - the right map's total drops
    by one for that species while the placed census is unchanged.

    python -m tools.exhibit_roster_hunt [span_bytes=0x800]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner        # noqa: E402
from pz_ap_client.memory.animals import AnimalResolver, OFF_EXHIBIT_CENSUS   # noqa: E402


def _fmt_map(d):
    return "{" + ", ".join("0x%X:%d" % (k, v) for k, v in sorted(d.items())) + "}"


def _is_superset(cand, placed):
    """True if `cand` has every placed species-handle with count >= placed, and at least one extra
    (more of a species, or a species the placed census lacks) - i.e. it also counts stored animals."""
    if not placed:
        return False
    for h, c in placed.items():
        if cand.get(h, 0) < c:
            return False
    extra = sum(cand.values()) - sum(placed.values())
    return extra > 0


def _annotate(cand, placed):
    if _is_superset(cand, placed):
        return "  <-- SUPERSET of placed (stored-inclusive candidate)"
    if cand == placed:
        return "  (identical to placed census - a mirror)"
    if placed and (cand.keys() & placed.keys()):
        return "  (shares species-handle keys with placed)"
    return ""


def _scan(res, mgr, placed, span):
    """Scan mgr+0..+span for ANY {u32 key -> u32 count} map (loose - report all, since the storage
    census may use non-overlapping keys or a large total). Returns (superset_list, maps_scanned)."""
    supersets, scanned = [], 0
    for off in range(0, span, 8):
        if off == OFF_EXHIBIT_CENSUS:
            continue  # skip the placed census itself
        cand = res._decode_count_map(mgr + off)
        if not cand:
            continue
        scanned += 1
        total = sum(cand.values())
        note = _annotate(cand, placed)
        if _is_superset(cand, placed):
            supersets.append((off, total, cand))
        # keep each line short: a huge map (e.g. 196 stored) prints its size, not every entry
        body = _fmt_map(cand) if len(cand) <= 12 else "%d keys, sample %s" % (
            len(cand), _fmt_map(dict(sorted(cand.items())[:6])))
        print("   +0x%-4X : total=%d %s%s" % (off, total, body, note))
    return supersets, scanned


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    span = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x1000
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1
    res = AnimalResolver(s)
    mgr = res.resolve_exhibit_manager()
    if not mgr:
        print("FAIL: no exhibit manager (zoo not loaded?)")
        return 1
    print("exhibit manager: 0x%X" % mgr)

    placed = res._decode_count_map(mgr + OFF_EXHIBIT_CENSUS) or {}
    print("placed census (+0x%X): total=%d %s" % (OFF_EXHIBIT_CENSUS, sum(placed.values()), _fmt_map(placed)))
    print("\n=== scanning mgr+0..+0x%X for {species_handle->count} maps ===" % span)

    supersets, scanned = _scan(res, mgr, placed, span)
    print("\n%d count-map(s) found in span; %d superset candidate(s)." % (scanned, len(supersets)))
    print("With ~196 in storage, look for a map whose total is your (placed + stored) count, or a")
    print("storage-only total (~196). If NO map here matches, stored animals aren't in a sibling count-")
    print("map - they're a list/collection of animal objects; re-run with a bigger span (e.g. 0x4000),")
    print("else we RE the storage collection directly (count its size / hook its add-remove).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
