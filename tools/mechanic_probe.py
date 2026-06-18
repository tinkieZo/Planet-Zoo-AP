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
from pz_ap_client.memory.research import ResearchReader, MECHANIC_CATEGORY, REC_STATUS  # noqa: E402

KNOWN_WELFARE_ID = 0xDAC  # plains zebra welfare L0 (a known cat-7 item id) - intern-space probe
REC_NAME_INTERN = 0x08    # record+0x08 = the item NAME's content-intern id (the real name bridge)


def _name_at_record(scanner, reg, status_addr):
    """Resolve a record's NAME via +0x08 (the content-intern id), given the record's status addr."""
    if reg is None:
        return None
    try:
        nid = int.from_bytes(scanner.read_bytes(status_addr - REC_STATUS + REC_NAME_INTERN, 4), "little")
        return reg._name(nid)  # noqa: SLF001 - probe
    except Exception:
        return None


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
    cat3 = sorted((item, level, status, addr) for item, level, status, cat, addr in recs
                  if cat == MECHANIC_CATEGORY)
    print(f"research records: {len(recs)} total, {len(cat3)} category-3 (mechanic)")

    reg = _try_intern(s)
    # The NAME bridge is record+0x08 (a content-intern id), NOT the item-id at +0x00. Verify on the
    # known welfare record (its +0x08 should be a real research name) by finding its addr.
    welfare_addr = next((a for it, _l, _s, a in
                         sorted((i, l, st, a) for i, l, st, c, a in recs) if it == KNOWN_WELFARE_ID), None)
    if reg is not None and welfare_addr is not None:
        print(f"name-bridge check: id 0x{KNOWN_WELFARE_ID:X} (zebra welfare L0) +0x08 -> "
              f"{_name_at_record(s, reg, welfare_addr)!r}  (vs +0x00 -> {reg._name(KNOWN_WELFARE_ID)!r})")  # noqa: SLF001

    # group consecutive item-ids into runs; resolve each run-start's NAME via +0x08
    runs = []
    for item, level, status, addr in cat3:
        if runs and item == runs[-1][-1][0] + 1:
            runs[-1].append((item, level, status, addr))
        else:
            runs.append([(item, level, status, addr)])
    print(f"\n{len(runs)} consecutive cat-3 runs (one per mechanic branch):")
    for run in runs:
        start, _lv, _st, addr = run[0]
        name = _name_at_record(s, reg, addr)
        done = sum(1 for _i, _l, st, _a in run if st == 4)
        print(f"  run 0x{start:X}..0x{run[-1][0]:X}  len={len(run):2d}  complete={done}/{len(run)}"
              f"  start-name={name!r}")

    # --- validate the CLIENT's mechanic detection path (name-resolved is_research_complete) ---
    from pz_ap_client.memory.research import MECHANIC_RESEARCH_NAME, _norm_token
    from pz_ap_client.memory.registry import RegistryResolver
    rr2 = ResearchReader(s, registry=RegistryResolver(s))
    snap = rr2._snapshot()  # noqa: SLF001 - probe
    mmap = rr2._mechanic_item_map()  # noqa: SLF001 - probe
    unresolved = sorted(k for k, n in MECHANIC_RESEARCH_NAME.items() if _norm_token(n) not in mmap)
    complete = sorted(k for k in MECHANIC_RESEARCH_NAME if rr2.is_research_complete(k, snap))
    print(f"\nmechanic detection: {len(mmap)} cat-3 names resolved; "
          f"{len(MECHANIC_RESEARCH_NAME) - len(unresolved)}/{len(MECHANIC_RESEARCH_NAME)} apworld keys "
          f"-> a live record; {len(complete)} currently complete (would fire)")
    if unresolved:
        print(f"  UNRESOLVED (name not in this scenario's research map): {unresolved}")
    print(f"  complete now: {complete}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
