"""validate_anchors - restart-robustness test for the A2 anchor chains.

The real proof a module_offset chain is patch/restart-stable is to re-resolve it
in a FRESH process and confirm it still lands on the right value. This harness:

  1. takes the player's CURRENT cash value (dollars) after a restart,
  2. scans i64 cents to find the live finance container (cash field with CC at
     +0x210), giving the ground-truth cash address,
  3. tests every candidate cash chain below and reports which still resolve to it,
  4. re-checks the already-saved anchors (guest_count, species_roster_base) read
     sane values.

Usage (game running, save reloaded):
    python -m tools.validate_anchors <current_cash_dollars>     e.g. 15736.13
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools.memscan import iter_regions  # noqa: E402

# Candidate cash chains found by pointer_scan (offsets from module base).
CASH_CANDIDATES = [
    [0x293CB58, 0x460, 0x10, 0xE8],
    [0x2944690, 0x18, 0xEF8, 0xE8],
    [0x29446A0, 0x20, 0x590, 0xE8],
    [0x296B100, 0xE88, 0xED0, 0xE8],
    [0x29836A8, 0x228, 0x1F8, 0xE8],
    [0x298AE00, 0x7E8, 0xED0, 0xE8],
    [0x2BFD178, 0x368, 0xEF8, 0xE8],
    [0x2D41A98, 0x88, 0x1F8, 0xE8],
    [0x2D6D1A8, 0x310, 0x870, 0xE8],
]
CC_DELTA = 0x210  # CC = cash + 0x210 in the finance container


def _find_aligned8(buf: bytes, needle: bytes, base: int) -> list[int]:
    """Addresses of every 8-aligned occurrence of `needle` in one region buffer."""
    out, start = [], 0
    while (i := buf.find(needle, start)) != -1:
        if i % 8 == 0:
            out.append(base + i)
        start = i + 1
    return out


def find_cash(scanner: MemoryScanner, cents: int) -> "int | None":
    """Return the finance-container cash address (the i64==cents whose +0x210 is CC)."""
    needle = int(cents).to_bytes(8, "little")
    hits: list[int] = []
    for base, size in iter_regions(scanner.pm.process_handle, writable_only=True):
        try:
            hits += _find_aligned8(scanner.read_bytes(base, size), needle, base)
        except Exception:
            continue
    # Disambiguate by the CC sibling being a plausible POSITIVE credit balance
    # (a coincidental cash-valued i64 elsewhere had CC+0x210 == 0 -> reject 0).
    for a in hits:
        try:
            if 0 < scanner.read_i64(a + CC_DELTA) < 50_000_000:
                return a
        except Exception:
            continue
    return hits[0] if hits else None


def _resolve(scanner: MemoryScanner, chain: list) -> "int | None":
    """resolve_pointer_chain that returns None instead of raising on a dead chain."""
    try:
        return scanner.resolve_pointer_chain(scanner.module_base, chain)
    except Exception:
        return None


def _fmt(chain: list) -> str:
    return "base + " + " -> ".join("0x%X" % o for o in chain)


def _report_survivors(label: str, survivors: list, total: int, cc_hint: bool = False) -> None:
    print("\n%d/%d %s chains survived." % (len(survivors), total, label))
    if survivors:
        print("recommended %s chain: %s" % (label, _fmt(min(survivors, key=len))))
        if cc_hint:
            print("  (CC chain = same with last offset 0x2F8 instead of 0xE8)")


def validate_cash(scanner: MemoryScanner, cents: int) -> int:
    cash_addr = find_cash(scanner, cents)
    if cash_addr is None:
        print("could not locate cash=%d cents; is the value exact?" % cents)
        return 1
    print("ground-truth cash addr: 0x%X (CC@+0x210 = %d)" % (cash_addr, scanner.read_i64(cash_addr + CC_DELTA)))
    print("\n-- cash chain survival --")
    survivors = []
    for ch in CASH_CANDIDATES:
        got = _resolve(scanner, ch)
        ok = got == cash_addr
        if ok:
            survivors.append(ch)
        print("  [%s] %-40s -> %s" % ("SURVIVES" if ok else "broke", _fmt(ch)[7:], ("0x%X" % got) if got else "err"))
    _report_survivors("cash", survivors, len(CASH_CANDIDATES), cc_hint=True)
    return 0


def validate_guest(scanner: MemoryScanner, guest_hint: int) -> None:
    """A guest chain survives if it re-resolves to a field holding a plausible guest
    count (within +/-50 of the hint, since guests drift). Robust to drift/false
    positives by picking the address the most surviving chains AGREE on."""
    from collections import Counter
    cand_path = Path(__file__).resolve().parent / ".guest_candidates.json"
    if not cand_path.exists():
        return
    cands = json.loads(cand_path.read_text())["candidates"]
    print("\n-- guest_count chain survival (plausible ~%d, agreement-based) --" % guest_hint)
    resolved = [(ch, a, _read_i32_safe(scanner, a)) for ch in cands if (a := _resolve(scanner, ch)) is not None]
    votes = Counter(a for _, a, v in resolved if v is not None and abs(v - guest_hint) <= 50)
    if not votes:
        print("0/%d guest chains survived." % len(cands))
        return
    best_addr, n = votes.most_common(1)[0]
    survivors = [ch for ch, a, _ in resolved if a == best_addr]
    print("ground-truth guest addr: 0x%X (value %d, %d chains agree)"
          % (best_addr, _read_i32_safe(scanner, best_addr), n))
    for ch in sorted(survivors, key=len):
        print("  [SURVIVES] %s" % _fmt(ch))
    _report_survivors("guest", survivors, len(cands))


def _read_i32_safe(scanner: MemoryScanner, addr: int) -> "int | None":
    try:
        return scanner.read_i32(addr)
    except Exception:
        return None


def validate_roster(scanner: MemoryScanner, zebra_hint: int) -> None:
    """A roster-object chain survives if resolved + field_off holds the current
    zebra count. Agreement-based, like guest, to tolerate any drift/false hits."""
    from collections import Counter
    cand_path = Path(__file__).resolve().parent / ".roster_candidates.json"
    if not cand_path.exists():
        return
    blob = json.loads(cand_path.read_text())
    cands, foff = blob["candidates"], blob["field_off"]
    print("\n-- species_roster_base chain survival (zebra ~%d @ +0x%X, agreement) --" % (zebra_hint, foff))
    resolved = [(ch, a) for ch in cands if (a := _resolve(scanner, ch)) is not None]
    votes = Counter(a for _, a in resolved
                    if (v := _read_i32_safe(scanner, a + foff)) is not None and abs(v - zebra_hint) <= 3)
    if not votes:
        print("0/%d roster chains survived." % len(cands))
        return
    best_addr, n = votes.most_common(1)[0]
    survivors = [ch for ch, a in resolved if a == best_addr]
    print("ground-truth roster obj: 0x%X (zebra@+0x%X = %d, %d chains agree)"
          % (best_addr, foff, _read_i32_safe(scanner, best_addr + foff), n))
    for ch in sorted(survivors, key=len):
        print("  [SURVIVES] %s" % _fmt(ch))
    _report_survivors("roster", survivors, len(cands))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m tools.validate_anchors <current_cash_dollars> [current_guest_count]")
        return 2
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached")
        return 1
    print("attached, base 0x%X" % s.module_base)
    rc = validate_cash(s, int(round(float(argv[0]) * 100)))
    if len(argv) > 1:
        validate_guest(s, int(argv[1]))
    if len(argv) > 2:
        validate_roster(s, int(argv[2]))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
