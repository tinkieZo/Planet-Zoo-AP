"""exhibit_roster_hunt - DIAGNOSTIC (read-only): find the exhibit manager's STORAGE-inclusive owned-animal
roster, since +0x2A0 is NOT the hash-set we assumed (read_exhibit_ids returns None live, 2026-07-08).

The def-names map (*(mgr+0x358)+0x108) is keyed by ANIMAL ID, so its keys are a ground-truth set of owned
exhibit animal ids. This scans the manager object mgr+0..+SPAN (qword stride) for any int hash-SET whose
decoded members CONTAIN those ids - that structure is the owned-id roster; the offset it's found at is the
correct OFF_EXHIBIT_ID_SET (replacing the wrong 0x2A0). Reports every candidate + how well its members
match the known ids, so a placed-only vs placed+stored structure is distinguishable (do a storage release
between runs and watch which candidate loses the id).

    python -m tools.exhibit_roster_hunt [span_bytes=0x800]

Run in the loaded zoo with exhibit animals present (ideally some placed AND some in storage).
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner        # noqa: E402
from pz_ap_client.memory.animals import AnimalResolver        # noqa: E402

HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def _q(s, addr):
    try:
        return int.from_bytes(s.read_bytes(addr, 8), "little")
    except Exception:
        return None


def _decode_int_set(s, base):
    """Decode a candidate int hash-SET header {count@+8, cap@+0x10 pow2, buckets@+0x18}: bitmap then
    cap * u32 entries. Returns the set of present u32 members, or None if it isn't a plausible set."""
    count = _q(s, base + 0x08)
    cap = _q(s, base + 0x10)
    buckets = _q(s, base + 0x18)
    if cap is None or buckets is None or not cap or cap > (1 << 20) or (cap & (cap - 1)) != 0:
        return None
    if count is None or not (0 <= count <= cap) or not (HEAP_LO < buckets < HEAP_HI):
        return None
    bitvec = ((cap >> 3) + 7) & ~7
    try:
        data = s.read_bytes(buckets, bitvec + cap * 4)
    except Exception:
        return None
    out = set()
    for i in range(cap):
        if (data[i >> 3] >> (i & 7)) & 1:
            out.add(struct.unpack_from("<I", data, bitvec + i * 4)[0])
    return out if len(out) == count else None   # member count must match the header count


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    span = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x800
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

    def_names = res.read_exhibit_def_names(mgr) or {}
    known = set(def_names)
    census = res.read_exhibit_census(mgr) or {}
    print("known animal ids (def-map keys): %d %s" % (len(known), sorted("0x%X" % i for i in known)))
    print("placed census (+0x318): %d species-handles %s" % (len(census), {("0x%X" % h): c for h, c in census.items()}))
    if not known:
        print("no known ids - place/store an exhibit animal first.")
        return 0

    print("\n=== scanning mgr+0..+0x%X for an int hash-set containing the known ids ===" % span)
    hits = []
    for off in range(0, span, 8):
        members = _decode_int_set(s, mgr + off)
        if not members:
            continue
        overlap = members & known
        if not overlap:
            continue
        extra = members - known
        hits.append((off, len(members), len(overlap), len(extra)))
        tag = "ALL known" if overlap == known else "%d/%d known" % (len(overlap), len(known))
        supset = " +%d beyond-known (STORAGE candidate)" % len(extra) if extra else ""
        print("   +0x%-4X : %d members, %s%s" % (off, len(members), tag, supset))
    if not hits:
        print("   no int-set candidate contained the known ids in this span "
              "(try a larger span, or the roster isn't a flat id-set - may be a map/vector).")
    else:
        full = [h for h in hits if h[2] == len(known)]
        print("\n%d candidate set(s); %d contain ALL known ids." % (len(hits), len(full)))
        print("The correct OFF_EXHIBIT_ID_SET is the offset holding ALL owned ids (placed+stored). Re-run")
        print("after a STORAGE release: the right candidate loses exactly the released id.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
