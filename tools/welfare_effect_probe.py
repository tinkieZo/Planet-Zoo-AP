"""welfare_effect_probe - DIAGNOSTIC (read-only): where the welfare EFFECT lives for the progressive
breeding/supplement/education families, so we can gate the effect (not just the rs+0x148 unlocked byte).

Live 2026-07-08: with 1 Progressive Breeding received, a FULLY-RESEARCHED African Buffalo shows 30%
fertility (level 2), because research completion applies the effect via FUN_140e3fbf0 independent of our
byte gate. The award fn applies each reward ONCE at the locked->unlocked transition (rec+0x12 0->1) and
never recomputes it - so a written-down value STICKS. This probe dumps, per welfare content record:

  * type (0 supplement / 2 breeding / 3 education), unlocked byte, bookkeep key (rec+0xC)
  * the count-map record at rs+0x210 keyed by that bookkeep key: f32 @+4 (breeding = fertility LEVEL),
    i32 @+8 (supplement = count) - the field to cap
  * the level-map record at rs+0x1e8 keyed by the content id: f32 @+4 (the target level research grants)
  * rs+0x52c GLOBAL education counter (line 543 of the decomp - NOT per-species, so per-animal education
    level must live elsewhere; we flag type-3 records that DO have a count-map entry as the candidate)

Correlate a known animal's number against its in-game UI (e.g. Buffalo breeding f32 <-> 30%).

    python -m tools.welfare_effect_probe [species_substring=buffalo]

Run in the loaded zoo with the target animal fully researched.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner            # noqa: E402
from pz_ap_client.memory.research import ResearchReader          # noqa: E402
from pz_ap_client.memory.registry import RegistryResolver        # noqa: E402
from pz_ap_client.memory import rewards                          # noqa: E402
from pz_ap_client.memory.rewards import (                        # noqa: E402
    InternRegistry, UnlockMap, RewardGranter,
    COUNT_MAP_OFF, LEVEL_MAP_OFF, REC_BOOKKEEP, EDU_COUNTER_OFF,
)

WELFARE_TYPES = {0: "supplement", 2: "breeding", 3: "education"}


def _q(s, addr):
    try:
        return int.from_bytes(s.read_bytes(addr, 8), "little")
    except Exception:
        return None


def _f32(s, addr):
    try:
        return struct.unpack("<f", s.read_bytes(addr, 4))[0]
    except Exception:
        return None


def _i32(s, addr):
    try:
        return struct.unpack("<i", s.read_bytes(addr, 4))[0]
    except Exception:
        return None


def _u32(s, addr):
    try:
        return struct.unpack("<I", s.read_bytes(addr, 4))[0]
    except Exception:
        return None


def _count_map_effect(g, rs, rec):
    """The count-map record (rs+0x210) for this reward record's bookkeep key: (f32 level, i32 count)."""
    bk = _u32(g.scanner, rec + REC_BOOKKEEP)
    if bk is None:
        return None
    crec = g._intmap_find(rs + COUNT_MAP_OFF, 0xC, bk)
    if crec is None:
        return None
    return bk, _f32(g.scanner, crec + 4), _i32(g.scanner, crec + 8)


def _level_map_target(g, rs, cid):
    """The level-map record (rs+0x1e8) for this content id: the f32 level research grants."""
    lrec = g._intmap_find(rs + LEVEL_MAP_OFF, 0x10, cid)
    return _f32(g.scanner, lrec + 4) if lrec is not None else None


HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def _dump_raw_map(s, base, stride, label, want_keys=None, limit=48):
    """Dump an engine int-map {count@+8, cap@+0x10 pow2, buckets@+0x18} raw: header + records as
    (key, f32@+4, i32@+8). Prints keys in `want_keys` first; if NONE matched, prints a sample of the
    map's actual keys so we can see the real key namespace (why the by-cid lookup missed)."""
    count = _q(s, base + 0x08)
    cap = _q(s, base + 0x10)
    buckets = _q(s, base + 0x18)
    print("\n-- %s @0x%X: count=%s cap=%s buckets=%s" % (label, base, count, cap, buckets))
    if cap is None or buckets is None or not cap or cap > (1 << 20) or (cap & (cap - 1)) != 0 \
            or not (HEAP_LO < buckets < HEAP_HI):
        print("   (header not a valid int-map here - layout differs / wrong offset)")
        return
    bitmap_sz = ((cap >> 3) + 7) & ~7
    try:
        bitmap = s.read_bytes(buckets, bitmap_sz)
        records = buckets + bitmap_sz
        blob = s.read_bytes(records, cap * stride)
    except Exception as e:
        print("   (unreadable: %s)" % e)
        return

    def rec_at(i):
        key = struct.unpack_from("<I", blob, i * stride)[0]
        f = struct.unpack_from("<f", blob, i * stride + 4)[0] if stride >= 8 else None
        iv = struct.unpack_from("<i", blob, i * stride + 8)[0] if stride >= 0xC else None
        return key, f, iv

    live = [i for i in range(cap) if (bitmap[i >> 3] >> (i & 7)) & 1]
    matched = [i for i in live if want_keys is None or rec_at(i)[0] in want_keys]
    if want_keys is not None and not matched:
        print("   (NONE of the %d wanted keys are in this map -> different key namespace. Sample of its "
              "actual keys:)" % len(want_keys))
        matched = live[:16]
    for i in matched[:limit]:
        key, f, iv = rec_at(i)
        print("   key=0x%-6X f32@+4=%s i32@+8=%s" % (key, f, iv))
    if len(matched) > limit:
        print("   ... (%d more)" % (len(matched) - limit))


def _dump_record(g, rs, reg, rec, cid, typ, flag):
    name = reg._name(cid) or "<unnamed>"
    parts = ["%-40s" % name, "cid=0x%X" % cid, "type=%d(%s)" % (typ, WELFARE_TYPES[typ]),
             "unlocked=%d" % flag]
    eff = _count_map_effect(g, rs, rec)
    if eff is not None:
        bk, level_f, count_i = eff
        parts.append("countmap[bk=0x%X]: f32=%s i32=%s" % (bk, level_f, count_i))
    else:
        parts.append("countmap: (none)")
    tgt = _level_map_target(g, rs, cid)
    if tgt is not None:
        parts.append("levelmap.f32=%s" % tgt)
    print("   " + " | ".join(parts))


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    needle = (sys.argv[1] if len(sys.argv) > 1 else "buffalo").lower()
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1

    rr = ResearchReader(s, registry=RegistryResolver(s))
    rs = rr._research_system()
    if not rs:
        print("FAIL: no research system (zoo not loaded?)")
        return 1
    print("research system: 0x%X" % rs)
    edu = _i32(s, rs + EDU_COUNTER_OFF)
    print("rs+0x52c GLOBAL education counter = %s (NOT per-species)" % edu)

    try:
        reg = InternRegistry(s)
        reg.build_index()
        m = UnlockMap(s, rs)
    except Exception as e:
        print("FAIL: %s" % e)
        return 1
    g = RewardGranter(s, rr)

    print("\n=== welfare records (type 0/2/3) matching %r ===" % needle)
    shown = 0
    matched_cids, matched_bks = set(), set()
    for rec, cid, typ, flag in m.iter_records():
        if typ not in WELFARE_TYPES:
            continue
        name = (reg._name(cid) or "").lower()
        if needle and needle not in name:
            continue
        _dump_record(g, rs, reg, rec, cid, typ, flag)
        matched_cids.add(cid)
        bk = _u32(s, rec + REC_BOOKKEEP)
        if bk is not None:
            matched_bks.add(bk)
        shown += 1
    print("   (%d records shown)" % shown)

    # Raw map dumps: the level-map (rs+0x1e8, keyed by content id) gives the per-LEVEL target the fix
    # needs; the count-map (rs+0x210, keyed per-species bookkeep key) is the field we cap. Restricting
    # to the matched keys keeps it readable.
    _dump_raw_map(s, rs + LEVEL_MAP_OFF, 0x10, "level-map rs+0x1e8 (key=content id)", want_keys=matched_cids)
    _dump_raw_map(s, rs + COUNT_MAP_OFF, 0xC, "count-map rs+0x210 (key=bookkeep)", want_keys=matched_bks)
    print("\nInterpretation:")
    print("  - breeding (type 2): countmap f32 is the fertility LEVEL - correlate with the UI %% for the")
    print("    fully-researched animal (e.g. 30%% -> confirm which f32 == level 2). That field is what a")
    print("    fix must CAP to the received progressive count (write persists; effect isn't recomputed).")
    print("  - education (type 3): if countmap is (none) for every record, the per-species education level")
    print("    is NOT in this map -> it lives elsewhere (likely derived from completed research status);")
    print("    that needs a follow-up hunt before education can be effect-gated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
