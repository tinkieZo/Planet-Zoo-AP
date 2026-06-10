"""unlock_flip_test - validate the runtime reward-grant primitive (research-data-layer spike).

Ghidra session 2026-06-10 decoded the research completion path (FUN_140E3FBF0): each completed
research item's reward content-ids (map rs+0x198, = the data-side `children`) are unlocked by
setting byte +0x12 = 1 on the content's record in the unlockables map at rs+0x148. This tool
proves/disproves that writing that flag from outside makes the content available in the UI
(enrichment shop tab etc.) - THE go/no-go for the v1.0 reward-decoupling architecture.

RESULT (2026-06-10): GO. Flag-only flip of EN_Grazing_Ball made it appear in the build menu
and placeable, live, with no event broadcast. Note type 1 (enrichment) has no bookkeeping
branch in the grant fn; types 0/2/3 have small side-effects to mirror (see layout notes).

Content names (from tools/research_catalog.json, e.g. EN_Grazing_Ball) resolve to runtime ids
via the global intern registry at module+0x298AE00 (DAT_14298AE00). The bucket hash turned out
NOT to be the plain DJB2 of fn 0x14BFB22E0 (live debug 2026-06-10: every chained name lands in
a bucket != djb2%count, though slot = 21*djb2 mod 32 holds - some variant/index transform we
have not cracked). Irrelevant for us: ids are dense interning-order indices into the pool
(entry = *(pool_top - id*stride)), so lookup() just enumerates the id space once and builds a
name->id dict. READ-ONLY by construction (the game's own resolver INTERNS missing names; we
never touch the buckets at all).

    python -m tools.unlock_flip_test scan                 # dump map stats + sample locked items
    python -m tools.unlock_flip_test name EN_Grazing_Ball # resolve + show one record
    python -m tools.unlock_flip_test flip EN_Grazing_Ball # set its unlocked flag (+0x12) = 1
    python -m tools.unlock_flip_test flip EN_Grazing_Ball 0   # revert (flag = 0)

Layout (verified live 2026-06-10 against headless-Ghidra decompiles of FUN_140E3FBF0 +
FUN_1468CBF30, dumped in tools/_decomp/):
  registry (module+0x298AE00): +0x10 i64 stride, +0x30 u64 pool_top, +0x9C u32 bucket_count;
      entry = *(pool_top - id*stride) & ~1 -> {+0 refcount, +4 u32 next id, +8 name cstr};
      ids are dense pool indices (we enumerate; the bucket hash is uncracked and unneeded).
  unlockables map (rs+0x148): {+8 count, +0x10 cap pow2, +0x18 buckets}; occupancy BITMAP of
      ((cap>>3)+7)&~7 bytes at buckets (bit j = slot j live), then records stride 0x14:
      {+0x00 u32 aux id, +0x04 u8 type (0..4), +0x08 u32 content id (KEY), +0x0C u32
       bookkeeping id (key into the per-type count map rs+0x210 for type 0), +0x12 u8 unlocked}.
  The game's grant path (FUN_140E3FBF0 reward loop) does: if rec[+0x12]==0 -> set 1, then
  type bookkeeping (0: rs+0x210 count++ keyed by rec[+0xC]; 2: max-level float via rs+0x1E8;
  3: rs+0x52C++; 4: Zoopedia named-dispatch) and finally one event broadcast (DAT_142945354).
  VERIFIED: the event broadcast is NOT needed - flag-only flip shows up in the build menu.

  Sibling maps (same family: {+8 count, +0x10 cap, +0x18 buckets}, occupancy bitmap, then
  records; key u32 at record+0):
    rs+0x210 count map, stride 0x0C: {+0 u32 key (= unlock rec +0xC id), +4 float max-level,
        +8 i32 count} (fn_14729B400.c). Type 0 grant: count++; type 2 grant: max-level float.
    rs+0x1E8 breeding-level map, stride 0x10: {+0 u32 content id, ...} (fn_144521730.c, which
        also exposes the int-key hash: h=k*0x1001; h=(h>>22^h)*0x11; h=(h>>9^h)*0x401;
        h=(h>>2^h)*0x81; slot=(h>>12^h)&(cap-1), linear probing. NB: the unlockables map's
        probe (FUN_1468d3100) uses a DIFFERENT scheme - we full-scan that one).
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Iterator, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.research import ResearchReader  # noqa: E402

REGISTRY_RVA = 0x298AE00
UNLOCK_MAP_OFF = 0x148
REC_STRIDE = 0x14
REC_AUX = 0x00
REC_TYPE = 0x04
REC_KEY = 0x08
REC_BOOKKEEP = 0x0C
REC_UNLOCKED = 0x12
# Known research-reward content names (from tools/research_catalog.json) - the true stride must
# show (some of) their ids as record keys.
ANCHOR_NAMES = ("EN_Grazing_Ball", "EN_Grab_Ball", "EN_Herbs", "EN_Rubbing_Pillar",
                "EN_Hanging_Grazer", "EN_Small_Barrel_Feeder")
HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def djb2(name: str) -> int:
    """Engine string hash (fn 0x14BFB22E0): h=5381; h=h*33+signed(c)."""
    h = 5381
    for ch in name.encode("ascii"):
        c = ch - 0x100 if ch >= 0x80 else ch
        h = (h * 33 + c) & 0xFFFFFFFF
    return h


class InternRegistry:
    """Read-only view of the global name<->id intern registry (DAT_14298AE00)."""

    def __init__(self, s: MemoryScanner):
        self.s = s
        base = s.module_base + REGISTRY_RVA
        # The global may be the object itself or a pointer to it - pick the readable layout.
        self.base = base
        if not self._plausible():
            self.base = s.read_qword(base) or 0
            if not self._plausible():
                raise RuntimeError("intern registry not readable at module+0x298AE00 (or deref)")
        self.stride = struct.unpack("<q", s.read_bytes(self.base + 0x10, 8))[0]
        self.pool_top = struct.unpack("<Q", s.read_bytes(self.base + 0x30, 8))[0]
        self.bucket_off = struct.unpack("<I", s.read_bytes(self.base + 0x98, 4))[0]
        self.bucket_count = struct.unpack("<I", s.read_bytes(self.base + 0x9C, 4))[0]
        self.buckets = self.pool_top - self.bucket_off * self.stride

    def _plausible(self) -> bool:
        try:
            stride = struct.unpack("<q", self.s.read_bytes(self.base + 0x10, 8))[0]
            top = struct.unpack("<Q", self.s.read_bytes(self.base + 0x30, 8))[0]
            count = struct.unpack("<I", self.s.read_bytes(self.base + 0x9C, 4))[0]
        except Exception:
            return False
        return 0 < stride <= 0x100 and HEAP_LO < top < HEAP_HI and 0 < count <= (1 << 24)

    def _entry(self, cid: int) -> Optional[int]:
        """Entry record pointer for a content id, or None."""
        try:
            slot = struct.unpack("<Q", self.s.read_bytes(self.pool_top - cid * self.stride, 8))[0]
        except Exception:
            return None
        rec = slot & ~1
        return rec if (slot and slot == rec and HEAP_LO < rec < HEAP_HI) else None

    def name(self, cid: int) -> Optional[str]:
        rec = self._entry(cid)
        if not rec:
            return None
        try:
            raw = self.s.read_bytes(rec + 8, 96)
        except Exception:
            return None
        end = raw.find(b"\x00")
        if end <= 0:
            return None
        try:
            txt = raw[:end].decode("ascii")
        except UnicodeDecodeError:
            return None
        return txt if txt.isprintable() else None

    MAX_IDS = 1 << 20  # hard cap on id-space enumeration (16 MiB of slots)
    SCAN_CHUNK = 4096  # ids per bulk slot read

    def build_index(self) -> dict:
        """Enumerate the whole id space (ids are dense pool indices) -> {name: id}.
        Keys are LOWERCASED: the game interns content names case-folded (live finding
        2026-06-10 - the registry holds 'en_grazing_ball', not 'EN_Grazing_Ball').
        Read-only; terminates when the slot region becomes unreadable."""
        if getattr(self, "_index", None) is not None:
            return self._index
        index: dict = {}
        max_id = 0
        for base in range(0, self.MAX_IDS, self.SCAN_CHUNK):
            ids = self._chunk_ids(base)
            if ids is None:
                break
            for cid in ids:
                nm = self.name(cid)
                if nm and nm.lower() not in index:
                    index[nm.lower()] = cid
                    max_id = max(max_id, cid)
            if not ids and base > 0:
                break
        self._index = index
        print(f"intern index built: {len(index)} names (max id 0x{max_id:X})")
        return index

    def _chunk_ids(self, base: int) -> Optional[list]:
        """Ids in [base+1, base+SCAN_CHUNK] whose pool slot holds a valid entry pointer.
        None = slot region unreadable (end of pool)."""
        try:
            blob = self.s.read_bytes(self.pool_top - (base + self.SCAN_CHUNK) * self.stride,
                                     self.SCAN_CHUNK * self.stride)
        except Exception:
            return None
        ids = []
        for i in range(self.SCAN_CHUNK):
            # slot for id n sits at pool_top - n*stride, so chunk order is reversed
            slot = struct.unpack_from("<Q", blob, (self.SCAN_CHUNK - (i + 1)) * self.stride)[0]
            rec = slot & ~1
            if slot and slot == rec and HEAP_LO < rec < HEAP_HI:
                ids.append(base + i + 1)
        return ids

    def lookup(self, name: str) -> Optional[int]:
        """name -> content id via full id-space enumeration; case-insensitive, READ-ONLY."""
        return self.build_index().get(name.lower())


class GameIntMap:
    """Generic view of the engine's int-keyed hashmap family: object {+8 i64 count,
    +0x10 i64 cap pow2, +0x18 buckets}; at buckets an occupancy bitmap of
    ((cap>>3)+7)&~7 bytes, then records of `stride` bytes with a u32 key at +key_off."""

    def __init__(self, s: MemoryScanner, base: int, stride: int, key_off: int = 0):
        self.s = s
        self.stride = stride
        self.key_off = key_off
        self.count = struct.unpack("<q", s.read_bytes(base + 0x08, 8))[0]
        self.cap = struct.unpack("<q", s.read_bytes(base + 0x10, 8))[0]
        self.buckets = struct.unpack("<Q", s.read_bytes(base + 0x18, 8))[0]
        if not (0 <= self.count <= self.cap and self.cap
                and (self.cap & (self.cap - 1)) == 0 and HEAP_LO < self.buckets < HEAP_HI):
            raise RuntimeError(f"map @ 0x{base:X} header implausible "
                               f"(count={self.count} cap={self.cap})")
        self.bitmap = s.read_bytes(self.buckets, ((self.cap >> 3) + 7) & ~7)
        self.records = self.buckets + len(self.bitmap)

    def _occupied(self) -> Iterator[int]:
        for i in range(self.cap):
            if (self.bitmap[i >> 3] >> (i & 7)) & 1:
                yield i

    def find(self, key: int) -> Optional[int]:
        """Live record address for a u32 key, or None (full scan; no insert)."""
        blob = self.s.read_bytes(self.records, self.cap * self.stride)
        for i in self._occupied():
            if struct.unpack_from("<I", blob, i * self.stride + self.key_off)[0] == key:
                return self.records + i * self.stride
        return None


class UnlockMap:
    """The unlockables map at research_system+0x148 (content id -> unlock record)."""

    def __init__(self, s: MemoryScanner, rs: int, reg: InternRegistry):
        self.s = s
        self.reg = reg
        self.count = struct.unpack("<q", s.read_bytes(rs + UNLOCK_MAP_OFF + 0x08, 8))[0]
        self.cap = struct.unpack("<q", s.read_bytes(rs + UNLOCK_MAP_OFF + 0x10, 8))[0]
        self.buckets = struct.unpack("<Q", s.read_bytes(rs + UNLOCK_MAP_OFF + 0x18, 8))[0]
        if not (0 < self.count <= self.cap and self.cap and (self.cap & (self.cap - 1)) == 0
                and HEAP_LO < self.buckets < HEAP_HI):
            raise RuntimeError("rs+0x148 does not look like a populated hashmap "
                               f"(count={self.count} cap={self.cap} buckets=0x{self.buckets:X})")
        self.bitmap = s.read_bytes(self.buckets, ((self.cap >> 3) + 7) & ~7)
        self.records = self.buckets + len(self.bitmap)
        self._validate()

    def _occupied(self) -> Iterator[int]:
        for i in range(self.cap):
            if (self.bitmap[i >> 3] >> (i & 7)) & 1:
                yield i

    def _validate(self) -> None:
        """The bitmap popcount must equal the header count, and the catalog anchor
        contents must appear among the record keys with sane type/flag bytes."""
        pop = sum(bin(b).count("1") for b in self.bitmap)
        if pop != self.count:
            raise RuntimeError(f"occupancy bitmap popcount {pop} != header count {self.count}")
        anchors = {cid for n in ANCHOR_NAMES if (cid := self.reg.lookup(n)) is not None}
        print(f"anchor ids resolved: {len(anchors)}/{len(ANCHOR_NAMES)}")
        found = bad = 0
        for _, cid, typ, flag in self.iter_records():
            if cid in anchors:
                found += 1
            if typ > 5 or flag > 1:
                bad += 1
        print(f"bitmap popcount == count ({pop}); anchors found as keys: "
              f"{found}/{len(anchors)}; records with off-spec type/flag: {bad}")
        if anchors and found < len(anchors):
            raise RuntimeError("anchor contents missing from the map - layout drift, "
                               "re-derive from tools/_decomp/fn_140E3FBF0.c")

    def iter_records(self) -> Iterator[Tuple[int, int, int, int]]:
        """Yield (record_addr, content_id, type, unlocked) for every live slot."""
        for i in self._occupied():
            rec = self.records + i * REC_STRIDE
            try:
                raw = self.s.read_bytes(rec, REC_STRIDE)
            except Exception:
                continue
            cid = struct.unpack_from("<I", raw, REC_KEY)[0]
            yield rec, cid, raw[REC_TYPE], raw[REC_UNLOCKED]

    def find(self, cid: int) -> Optional[Tuple[int, int, int]]:
        for rec, rcid, typ, flag in self.iter_records():
            if rcid == cid:
                return rec, typ, flag
        return None


def attach():
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: could not attach to PlanetZoo.exe (is the game running?)")
        sys.exit(1)
    reader = ResearchReader(s)
    rs = reader._research_system()
    if not rs:
        print("FAIL: research system unresolved (is a zoo loaded?)")
        sys.exit(1)
    print(f"research_system @ 0x{rs:X}")
    unlock_all = s.read_bytes(rs + 0x260, 1)[0]
    print(f"global unlock-all byte (rs+0x260) = {unlock_all}"
          + ("  *** NONZERO - everything reports unlocked! ***" if unlock_all else ""))
    reg = InternRegistry(s)
    print(f"intern registry @ 0x{reg.base:X} (stride 0x{reg.stride:X}, "
          f"{reg.bucket_count} buckets)")
    m = UnlockMap(s, rs, reg)
    print(f"unlockables map rs+0x148: count={m.count} cap={m.cap}")
    return s, reg, m, rs


def cmd_scan():
    _, reg, m, _ = attach()
    by_type: dict = {}
    locked_samples = []
    for _, cid, typ, flag in m.iter_records():
        tot, unl = by_type.get(typ, (0, 0))
        by_type[typ] = (tot + 1, unl + flag)
        if not flag and len(locked_samples) < 25:
            locked_samples.append((cid, typ, reg.name(cid) or "?"))
    for typ in sorted(by_type):
        tot, unl = by_type[typ]
        print(f"  type {typ}: {tot} records, {unl} unlocked / {tot - unl} locked")
    print("sample LOCKED content (flip candidates):")
    for cid, typ, name in locked_samples:
        print(f"  id 0x{cid:X} type {typ}  {name}")


def cmd_name(content: str):
    _, reg, m, _ = attach()
    cid = reg.lookup(content)
    if cid is None:
        print(f"'{content}' not in intern registry (read-only lookup; not interned)")
        return
    print(f"'{content}' -> content id 0x{cid:X}")
    hit = m.find(cid)
    if not hit:
        print("  NOT in the unlockables map (this content is not research-reward-gated)")
        return
    rec, typ, flag = hit
    print(f"  record @ 0x{rec:X}  type={typ}  unlocked={flag}")


COUNT_MAP_OFF = 0x210    # GameIntMap stride 0xC: {+0 key, +4 float max-level, +8 i32 count}
LEVEL_MAP_OFF = 0x1E8    # GameIntMap stride 0x10: {+0 content id, +4 f32 level (best guess)}
EDU_COUNTER_OFF = 0x52C  # plain i32 on the research system


def _bookkeep(s: MemoryScanner, rs: int, rec: int, cid: int, typ: int, delta: int):
    """Mirror FUN_140E3FBF0's per-type side-effects for an unlock (delta=+1) / revert (-1).
    Only mutates EXISTING records - the game's find-or-insert allocation is not replicated."""
    if typ == 1:
        print("  type 1 (enrichment): no bookkeeping needed (verified in-game)")
        return
    if typ == 4:
        print("  type 4 (zoopedia): script-dispatch bookkeeping NOT mirrored (cosmetic)")
        return
    if typ == 3:
        addr = rs + EDU_COUNTER_OFF
        cur = struct.unpack("<i", s.read_bytes(addr, 4))[0]
        s.write_bytes(addr, struct.pack("<i", max(0, cur + delta)))
        print(f"  type 3 (education): rs+0x52C counter {cur} -> {max(0, cur + delta)}")
        return
    if typ in (0, 2):
        bk = struct.unpack("<I", s.read_bytes(rec + REC_BOOKKEEP, 4))[0]
        crec = GameIntMap(s, rs + COUNT_MAP_OFF, 0xC).find(bk)
        if crec is None:
            print(f"  type {typ}: count-map record for key 0x{bk:X} ABSENT - the game "
                  "would insert one (not replicated); UI counters may lag until it exists")
        elif typ == 0:
            cur = struct.unpack("<i", s.read_bytes(crec + 8, 4))[0]
            s.write_bytes(crec + 8, struct.pack("<i", max(0, cur + delta)))
            print(f"  type 0 (supplement): count[0x{bk:X}] {cur} -> {max(0, cur + delta)}")
        else:
            _bookkeep_breeding(s, rs, cid, bk, crec, delta)
        return
    print(f"  type {typ}: unknown - no bookkeeping applied")


def _bookkeep_breeding(s: MemoryScanner, rs: int, cid: int, bk: int, crec: int, delta: int):
    """Type 2: push the count-map record's max-level float (+4) up to this content's level
    (read from the rs+0x1E8 record; the game's exact source is decompiler-garbled)."""
    lrec = GameIntMap(s, rs + LEVEL_MAP_OFF, 0x10).find(cid)
    if lrec is None or delta < 0:
        print("  type 2 (breeding): no level record / revert - max-level untouched")
        return
    level = struct.unpack("<f", s.read_bytes(lrec + 4, 4))[0]
    cur = struct.unpack("<f", s.read_bytes(crec + 4, 4))[0]
    if level > cur:
        s.write_bytes(crec + 4, struct.pack("<f", level))
    print(f"  type 2 (breeding, EXPERIMENTAL): max-level[0x{bk:X}] {cur} -> {max(cur, level)}")


def cmd_flip(content: str, value: int):
    s, reg, m, rs = attach()
    cid = reg.lookup(content)
    if cid is None:
        print(f"'{content}' not in intern registry - aborting")
        return
    hit = m.find(cid)
    if not hit:
        print(f"'{content}' (id 0x{cid:X}) not in the unlockables map - aborting")
        return
    rec, typ, flag = hit
    print(f"'{content}' id 0x{cid:X} record @ 0x{rec:X} type={typ}: unlocked {flag} -> {value}")
    s.write_bytes(rec + REC_UNLOCKED, bytes([value]))
    print(f"written. re-read: unlocked={s.read_bytes(rec + REC_UNLOCKED, 1)[0]}")
    delta = (1 if value else 0) - (1 if flag else 0)
    if delta:
        _bookkeep(s, rs, rec, cid, typ, delta)
    else:
        print("  flag unchanged - bookkeeping skipped")
    print("now check in-game (re-open the relevant menu; no UI event is fired).")


def _dump_bucket_chain(s, reg, b, budget) -> int:
    """Walk one bucket's chain printing up to `budget` entries; -1 = bucket array unreadable (stop)."""
    try:
        cid = struct.unpack("<I", s.read_bytes(reg.buckets + b * 4, 4))[0]
    except Exception:
        return -1
    shown = 0
    hops = 0
    while cid and hops < 50 and shown < budget:
        name = reg.name(cid)
        if name:
            want = djb2(name) % reg.bucket_count
            ok = "OK " if want == b else f"MISMATCH (hash->bucket {want})"
            print(f"  bucket {b:4d} id 0x{cid:06X} {ok} {name}")
        else:
            print(f"  bucket {b:4d} id 0x{cid:06X} <unreadable name>")
        shown += 1
        rec = reg._entry(cid)
        if not rec:
            break
        cid = struct.unpack("<I", s.read_bytes(rec + 4, 4))[0]
        hops += 1
    return shown


def cmd_debug():
    """Dump raw registry entries + self-validate the hash: for found (id, name) pairs the
    name must hash back to the bucket it was found in."""
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        sys.exit(1)
    reg = InternRegistry(s)
    flags = s.read_bytes(reg.base + 0xA0, 1)[0]
    print(f"registry @ 0x{reg.base:X}: stride 0x{reg.stride:X} pool_top 0x{reg.pool_top:X} "
          f"bucket_off {reg.bucket_off} buckets {reg.bucket_count} cmpflag(+0xA0)={flags}")
    shown = 0
    for b in range(reg.bucket_count):
        n = _dump_bucket_chain(s, reg, b, 30 - shown)
        if n < 0:
            break
        shown += n
        if shown >= 30:
            break


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "scan"
    if cmd == "scan":
        cmd_scan()
    elif cmd == "debug":
        cmd_debug()
    elif cmd == "name" and len(args) >= 2:
        cmd_name(args[1])
    elif cmd == "flip" and len(args) >= 2:
        cmd_flip(args[1], int(args[2]) if len(args) >= 3 else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
