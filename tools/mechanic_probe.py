"""mechanic_probe - map the apworld's per-item mechanic-research locations to the live research map.

The apworld made one location per mechanic ITEM (drink_shop1=DrinkShopsGulpeeSlush ...), but the game
researches per BRANCH with levels (Drink Shops = one research, a consecutive run of cat-3 level records).
To fire those checks the client must map each apworld mechanic location to a cat-3 research record. This
read-only probe gathers exactly what's needed to finalise that mapping (no writes, no hooks):

  1. Enumerates the research items map, filters category 3 (mechanic), sorts by item-id, and groups
     consecutive ids into RUNS (one run == one branch's level records).
  2. For each run, tries to resolve the record's item-id -> NAME via the intern registry (rewards
     InternRegistry). THE key question: do cat-3 research-item-ids resolve to names? If yes, the client
     maps runs -> branches by name (clean, like welfare). If no, we fall back to size/order alignment
     with research_catalog.json.
  3. Sanity-resolves a known WELFARE item-id (0xDAC, plains zebra L0) the same way, to tell whether
     research-item-ids live in the intern content-id space at all.

    python -m tools.mechanic_probe

Run it in the loaded ARCHIPELAGO scenario. Paste the output back; it determines the final mechanic
mapping that ResearchReader.mechanic detection consumes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.research import ResearchReader, MECHANIC_CATEGORY  # noqa: E402

KNOWN_WELFARE_ID = 0xDAC  # plains zebra welfare L0 (a known cat-7 item id) - intern-space probe


def _try_intern(scanner):
    """Return an InternRegistry or None (it's the rewards module's; resolves content-id -> name)."""
    try:
        from pz_ap_client.memory.rewards import InternRegistry
        return InternRegistry(scanner)
    except Exception as e:
        print(f"  (intern registry unavailable: {e})")
        return None


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is the game running?)")
        return 1
    rr = ResearchReader(s)
    recs = rr.scan_records()
    if not recs:
        print("FAIL: research map unreadable (in a loaded zoo?)")
        return 1
    cat3 = sorted((item, level, status) for item, level, status, cat, _ in recs if cat == MECHANIC_CATEGORY)
    print(f"research records: {len(recs)} total, {len(cat3)} category-3 (mechanic)")

    reg = _try_intern(s)
    if reg is not None:
        nm = reg._name(KNOWN_WELFARE_ID)  # noqa: SLF001 - probe
        print(f"intern-space check: id 0x{KNOWN_WELFARE_ID:X} (zebra welfare L0) -> name {nm!r}")
        print("  => if that's a zebra research name, research-item-ids ARE interned (name mapping works).")

    # group consecutive item-ids into runs
    runs = []
    for item, level, status in cat3:
        if runs and item == runs[-1][-1][0] + 1:
            runs[-1].append((item, level, status))
        else:
            runs.append([(item, level, status)])
    print(f"\n{len(runs)} consecutive cat-3 runs (one per mechanic branch):")
    for run in runs:
        start = run[0][0]
        name = reg._name(start) if reg else None  # noqa: SLF001
        done = sum(1 for _, _, st in run if st == 4)
        print(f"  run 0x{start:X}..0x{run[-1][0]:X}  len={len(run):2d}  complete={done}/{len(run)}"
              f"  start-name={name!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
