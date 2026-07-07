"""AnimalResolver - resolve an animal entity handle to its entity + read fields.

Planet Zoo has no static per-animal anchor (entities reallocate), but the insert
hook (hook.py / tools/insert_hook.py) captures, for each animal added to a habitat,
its entity *handle* (rsi) and the *zoo* object (r13 = the insert fn's param_1).
This resolves handle -> animal entity by replicating the game's own lookup
(``FUN_146EC8630`` + its hash-map ``FUN_1444F29B0``), so we can read the
**life-stage** byte and tell a BIRTH (newborn) from a market BUY:

  manager       = *(zoo + 0x2F8)                      # zoo[0x5f]
  hashmap       = manager + 0xB90                     # {+0x10 cap, +0x18 buckets}
  index         = hashmap_lookup(handle)              # open-addressing probe
  entity        = *(manager + 0xC20) + index*0x3F0    # record table, stride 0x3F0
  life stage    = entity[0x3A7]  (0 = NEWBORN/baby, 1 = juvenile/grown, 2+ adult)
  species id    = entity[0x54]   (entity namespace; container[rbx+8] is another)

Validated live: the only stage-0 animals were exactly the observed births; every
bought animal read stage 1. So **a captured insert whose entity stage == 0 is a
birth** - path-independent and robust.
"""

from __future__ import annotations

import struct
from typing import Optional

MASK64 = (1 << 64) - 1

# entity-record offsets (stride 0x3F0)
OFF_SPECIES_HANDLE = 0x50   # u32 species HANDLE - same namespace as ResearchReader.current_handle,
                            # so a birth's species is reverse-mapped via the research map (verified
                            # exact: wolf 0x46DA, zebra 0x309F, gorilla 0x3084, panda 0x3096, ...).
                            # This is the RELIABLE per-species id (container[+8] is a habitat id).
OFF_SPECIES = 0x54          # ushort per-species index (also distinguishes species; +0x50 preferred)
OFF_LIFE_STAGE = 0x3A7      # byte: 0 = newborn/baby (BIRTH), 1 = juvenile, 2+ adult
LIFE_STAGE_NEWBORN = 0

# zoo / manager / hashmap offsets
OFF_ZOO_ANIMAL_MGR = 0x2F8  # zoo[0x5f]
OFF_MGR_HASHMAP = 0xB90     # manager + 0xB90 -> hashmap object
OFF_MGR_TABLE = 0xC20       # manager + 0xC20 -> *record table base
OFF_MAP_CAP = 0x10
OFF_MAP_BUCKETS = 0x18
RECORD_STRIDE = 0x3F0

# The owned-animal roster manager hangs off the PARK (root -> park -> +0x188). The SAME roster holds both
# placed (habitat) and stored animals (live-confirmed: a bought-unplaced animal appears here), so a single
# enumeration caches every owned animal. This is the race-free source for release attribution: a released
# animal is removed from the roster within ms (faster than the ~1s poll can resolve it live), but a periodic
# sweep cached it beforehand - and it covers loaded/continued saves whose animals never tripped the insert
# hook. park = *(*(base+ANIMAL_ROOT_RVA)+0x20)+0x658 (same chain market.py uses for the exchange managers).
ANIMAL_ROOT_RVA = 0x29446A0
PARK_CHAIN = (0x20, 0x658)
OFF_PARK_ANIMAL_MGR = 0x188

# EXHIBIT animals live in a SEPARATE manager (park + 0x1D0; matches *(exhibit_obj+0x1d0) in the
# GetCCValueOfReleasingExhibitAnimal decompile), NOT the +0x188 habitat roster - which is why a habitat
# roster sweep never sees exhibit animals. That manager carries a {species_handle(u32) -> count(u32)}
# CENSUS open-addressing map at +0x318 (count @+0x320, cap @+0x328 [power of two], buckets @+0x330;
# 8-byte bitmap-then-entries, entry stride 8 = [u32 species_handle, u32 count]). The census is the
# race-free source for per-species exhibit conservation_release: exhibit animals have no +0x50 species
# handle reachable via a simple roster walk (the roster maps are id<->id only), but a release DECREMENTS
# this census, so diffing it across a detected release attributes the species (see triggers).
OFF_PARK_EXHIBIT_MGR = 0x1D0
OFF_EXHIBIT_CENSUS = 0x318   # {species_handle -> count} map header (obj @+0, count @+8, cap @+0x10, buckets @+0x18)

# The exhibit manager ALSO carries per-ANIMAL structures (decomp of the exhibit release executor
# FUN_146048940, tools/_decomp + scratchpad fn_146048940.c, 2026-07-06) - these are what enable
# STORAGE release attribution (the +0x318 census covers only PLACED animals; these cover every owned
# exhibit animal - the release action checks id-set membership before proceeding, and storage releases
# succeed, so stored animals are in it; the id is removed from the set SYNCHRONOUSLY at release):
#   +0x2A0: int hash-SET of all owned exhibit ANIMAL IDS - {count @+0x08, cap @+0x10 pow2,
#           buffer @+0x18 -> occupancy bitvector then u32 keys[cap]} (the market include-set family).
#   +0x358 -> obj; obj+0x108: open-addressing MAP {animal_id -> def entry}: {count @+0x08, cap @+0x10
#           pow2, buckets @+0x18} -> bitmap then entries stride 0x160 = {u32 animal_id @+0, animal-def
#           object inline @+0x8} whose leading fields are refcounted native strings ({len i64 @+0,
#           chars @+0x14}); one of them is the SPECIES NAME token (e.g. 'GoliathBeetle' - the engine
#           resolves the released species from this object when posting ExhibitAnimalReleasedMessage).
OFF_EXHIBIT_ID_SET = 0x2A0
OFF_EXHIBIT_DEF_SUB = 0x358
OFF_DEF_SUB_MAP = 0x108
DEF_ENTRY_STRIDE = 0x160
DEF_ENTRY_OBJ = 0x8
DEF_OBJ_STRING_OFFS = (0x0, 0x8, 0x10, 0x20)   # candidate name-string ptr fields of the def object


class AnimalResolver:
    def __init__(self, scanner):
        self.scanner = scanner

    @staticmethod
    def _hash(key: int, cap: int) -> int:
        """Replicates FUN_1444F29B0's key hash -> initial slot (cap is a power of two)."""
        h = (key * 0x40000 + (~key & MASK64)) & MASK64
        h = ((h >> 0x1F ^ h) * 0x15) & MASK64
        h = ((h >> 0x0B ^ h) * 0x41) & MASK64
        h = (h >> 0x16 ^ h) & (cap - 1)
        return h

    def _lookup_index(self, buckets: int, cap: int, key: int) -> Optional[int]:
        """Open-addressing probe (FUN_1444F29B0): returns the stored index or None."""
        bitmap_sz = ((cap >> 3) + 7) & ~7
        try:
            data = self.scanner.read_bytes(buckets, bitmap_sz + cap * 0x10)
        except Exception:
            return None
        i = self._hash(key, cap)
        start = i
        while True:
            word = struct.unpack_from("<Q", data, (i >> 6) * 8)[0]
            if not ((word >> (i & 0x3F)) & 1):
                return None  # empty slot ends the probe -> not found
            entry = bitmap_sz + i * 0x10
            if struct.unpack_from("<Q", data, entry)[0] == key:
                idx = struct.unpack_from("<I", data, entry + 8)[0]
                return None if idx == 0xFFFFFFFF else idx
            i = (i + 1) % cap
            if i == start:
                return None

    def resolve_entity_via_manager(self, mgr: int, handle: int) -> Optional[int]:
        """handle -> animal entity address using the animal-roster MANAGER directly (or None).
        The hashmap cap must be a power of two (the structure's invariant); when it isn't, ``mgr``
        is the wrong object (a garbage read), so we bail with None instead of indexing a bogus
        table. This guard is what lets callers safely *try* a candidate pointer (e.g. a value
        captured at the release site) as a manager - a wrong guess resolves to nothing, never to a
        false entity."""
        if not mgr:
            return None
        cap = self.scanner.read_qword(mgr + OFF_MGR_HASHMAP + OFF_MAP_CAP)
        buckets = self.scanner.read_qword(mgr + OFF_MGR_HASHMAP + OFF_MAP_BUCKETS)
        table = self.scanner.read_qword(mgr + OFF_MGR_TABLE)
        if not cap or not buckets or not table:
            return None
        if cap > (1 << 24) or (cap & (cap - 1)) != 0:   # power-of-two invariant; reject a wrong mgr
            return None
        idx = self._lookup_index(buckets, cap, handle)
        if idx is None:
            return None
        return table + idx * RECORD_STRIDE

    def resolve_entity(self, zoo: int, handle: int) -> Optional[int]:
        """handle -> animal entity address via the ZOO (manager = *(zoo+0x2F8)), or None."""
        if not zoo:
            return None
        mgr = self.scanner.read_qword(zoo + OFF_ZOO_ANIMAL_MGR)
        return self.resolve_entity_via_manager(mgr, handle) if mgr else None

    def life_stage(self, entity: int) -> Optional[int]:
        try:
            return self.scanner.read_bytes(entity + OFF_LIFE_STAGE, 1)[0]
        except Exception:
            return None

    def species_id(self, entity: int) -> Optional[int]:
        try:
            return struct.unpack("<H", self.scanner.read_bytes(entity + OFF_SPECIES, 2))[0]
        except Exception:
            return None

    def species_handle(self, entity: int) -> Optional[int]:
        """The animal's species HANDLE (entity+0x50) - reverse-map via ResearchReader.current_handle
        to get the species_key. This is the reliable species id for birth attribution."""
        try:
            return struct.unpack("<I", self.scanner.read_bytes(entity + OFF_SPECIES_HANDLE, 4))[0]
        except Exception:
            return None

    def resolve_animal_manager(self) -> Optional[int]:
        """The owned-animal roster manager (root -> park -> +0x188), or None if no zoo is loaded.
        Reachable from a stable root, so it works on a freshly-loaded save before any insert/release."""
        from .signatures import resolve_root
        if getattr(self.scanner, "module_base", None) is None:
            return None
        root = resolve_root(self.scanner, ANIMAL_ROOT_RVA)
        if not root:
            return None
        park = self.scanner.resolve_pointer_chain(root, [0, *PARK_CHAIN])
        if not park:
            return None
        mgr = self.scanner.read_qword(park + OFF_PARK_ANIMAL_MGR)
        return mgr or None

    def resolve_park(self) -> Optional[int]:
        """The PARK object (root -> [0] -> +0x20 -> +0x658), or None if no zoo is loaded. The anchor for
        both the habitat roster (+0x188) and the exhibit manager (+0x1D0)."""
        from .signatures import resolve_root
        if getattr(self.scanner, "module_base", None) is None:
            return None
        root = resolve_root(self.scanner, ANIMAL_ROOT_RVA)
        if not root:
            return None
        return self.scanner.resolve_pointer_chain(root, [0, *PARK_CHAIN]) or None

    def resolve_exhibit_manager(self) -> Optional[int]:
        """The exhibit-animal manager (park + 0x1D0), or None if no zoo is loaded / unreachable. Separate
        from the habitat roster (+0x188). Reachable from the stable root, so it works on a freshly-loaded
        save before any exhibit insert/release."""
        park = self.resolve_park()
        if not park:
            return None
        return self.scanner.read_qword(park + OFF_PARK_EXHIBIT_MGR) or None

    def _decode_count_map(self, hdr: int) -> "Optional[dict]":
        """Decode an open-addressing {u32 key -> u32 count} map whose header is at ``hdr`` (cap @+0x10
        [power of two], buckets @+0x18; 8-byte bitmap then stride-8 [u32 key, u32 count]). None if the
        header isn't a valid map (the power-of-two cap guard rejects a bad pointer, so callers can probe
        addresses safely). Used for the exhibit census (placed + stored share this layout)."""
        cap = self.scanner.read_qword(hdr + OFF_MAP_CAP)
        buckets = self.scanner.read_qword(hdr + OFF_MAP_BUCKETS)
        if not cap or not buckets or cap > (1 << 20) or (cap & (cap - 1)) != 0:
            return None
        bitmap_sz = ((cap >> 3) + 7) & ~7
        try:
            data = self.scanner.read_bytes(buckets, bitmap_sz + cap * 8)
        except Exception:
            return None
        if not data or len(data) < bitmap_sz + cap * 8:
            return None
        out: dict = {}
        for i in range(cap):
            word = struct.unpack_from("<Q", data, (i >> 6) * 8)[0]
            if not ((word >> (i & 0x3F)) & 1):
                continue
            handle, count = struct.unpack_from("<II", data, bitmap_sz + i * 8)
            if count:
                out[handle] = count
        return out

    def read_exhibit_census(self, mgr: int) -> "Optional[dict]":
        """{species_handle -> count} for the PLACED exhibit animals (the manager's +0x318 census map). None
        if mgr is wrong/unreadable. NOTE: this is placed animals only - stored/trade-center exhibit animals
        are NOT in this roster (live-pop == census sum confirmed it), so a storage release won't drop it.
        Reverse-map the handles through the research map to get species_keys (habitat entity+0x50 namespace)."""
        if not mgr:
            return None
        return self._decode_count_map(mgr + OFF_EXHIBIT_CENSUS)

    def read_exhibit_ids(self, mgr: int) -> "Optional[set]":
        """Ids of ALL owned exhibit animals - placed AND stored - from the manager's +0x2A0 id set.
        None if mgr is wrong/unreadable (pow2-cap guard). The release action removes the released id
        from this set SYNCHRONOUSLY, so diffing it across a detected release names the animal - the
        storage-release attribution the placed-only census can't provide."""
        if not mgr:
            return None
        base = mgr + OFF_EXHIBIT_ID_SET
        try:
            cap = self.scanner.read_qword(base + 0x10)
            buf = self.scanner.read_qword(base + 0x18)
        except Exception:
            return None
        if not buf or not cap or cap > (1 << 20) or (cap & (cap - 1)) != 0:
            return None
        bitvec = ((cap >> 3) + 7) & ~7
        try:
            data = self.scanner.read_bytes(buf, bitvec + cap * 4)
        except Exception:
            return None
        out = set()
        for i in range(cap):
            if (data[i >> 3] >> (i & 7)) & 1:
                out.add(struct.unpack_from("<I", data, bitvec + i * 4)[0])
        return out

    def _native_string(self, ptr: int, max_len: int = 64) -> Optional[str]:
        """Engine refcounted string {len i64 @+0, chars @+0x14}, or None if implausible."""
        if not ptr:
            return None
        try:
            n = struct.unpack("<q", self.scanner.read_bytes(ptr, 8))[0]
            if not 0 < n <= max_len:
                return None
            txt = self.scanner.read_bytes(ptr + 0x14, n).decode("ascii")
        except Exception:
            return None
        return txt if txt.isprintable() else None

    def read_exhibit_def_names(self, mgr: int) -> "Optional[dict]":
        """{animal_id -> [candidate name strings]} from the exhibit def map (*(mgr+0x358)+0x108).
        One of the candidates is the species token (the caller matches them against the species
        token map - self-validating, so it doesn't matter which def field holds the species). None
        if the structures don't validate; {} if the map is just empty."""
        if not mgr:
            return None
        try:
            sub = self.scanner.read_qword(mgr + OFF_EXHIBIT_DEF_SUB)
            if not sub:
                return None
            hdr = sub + OFF_DEF_SUB_MAP
            count = self.scanner.read_qword(hdr + 0x08)
            cap = self.scanner.read_qword(hdr + 0x10)
            buckets = self.scanner.read_qword(hdr + 0x18)
        except Exception:
            return None
        if not buckets or not cap or cap > (1 << 16) or (cap & (cap - 1)) != 0 or not 0 <= count <= cap:
            return None
        bitmap_sz = ((cap >> 3) + 7) & ~7
        try:
            bitmap = self.scanner.read_bytes(buckets, bitmap_sz)
        except Exception:
            return None
        out: dict = {}
        for i in range(cap):
            if (bitmap[i >> 3] >> (i & 7)) & 1:
                parsed = self._parse_def_entry(buckets + bitmap_sz + i * DEF_ENTRY_STRIDE)
                if parsed is not None:
                    out[parsed[0]] = parsed[1]
        return out

    def _parse_def_entry(self, entry: int) -> "Optional[tuple]":
        """(animal_id, [candidate name strings]) for one def-map entry, or None if unreadable."""
        try:
            aid = struct.unpack("<I", self.scanner.read_bytes(entry, 4))[0]
            names = []
            for off in DEF_OBJ_STRING_OFFS:
                nm = self._native_string(self.scanner.read_qword(entry + DEF_ENTRY_OBJ + off))
                if nm:
                    names.append(nm)
        except Exception:
            return None
        return aid, names

    def iter_roster(self, mgr: int):
        """Yield (handle, entity) for every animal in the manager's roster (habitat + storage). Reads the
        whole open-addressing bucket array once; same structure _lookup_index probes. No-op if mgr is wrong
        (cap not a power of two) or unreadable."""
        if not mgr:
            return
        cap = self.scanner.read_qword(mgr + OFF_MGR_HASHMAP + OFF_MAP_CAP)
        buckets = self.scanner.read_qword(mgr + OFF_MGR_HASHMAP + OFF_MAP_BUCKETS)
        table = self.scanner.read_qword(mgr + OFF_MGR_TABLE)
        if not (cap and buckets and table) or cap > (1 << 24) or (cap & (cap - 1)) != 0:
            return
        bitmap_sz = ((cap >> 3) + 7) & ~7
        try:
            data = self.scanner.read_bytes(buckets, bitmap_sz + cap * 0x10)
        except Exception:
            return
        for i in range(cap):
            word = struct.unpack_from("<Q", data, (i >> 6) * 8)[0]
            if not ((word >> (i & 0x3F)) & 1):
                continue
            entry = bitmap_sz + i * 0x10
            handle = struct.unpack_from("<Q", data, entry)[0]
            idx = struct.unpack_from("<I", data, entry + 8)[0]
            if idx != 0xFFFFFFFF:
                yield handle, table + idx * RECORD_STRIDE

    def is_newborn(self, zoo: int, handle: int) -> Optional[bool]:
        """True if the handle resolves to a newborn (life stage 0) = a BIRTH.
        None if it can't be resolved (caller should skip, not treat as buy)."""
        entity = self.resolve_entity(zoo, handle)
        if entity is None:
            return None
        stage = self.life_stage(entity)
        if stage is None:
            return None
        return stage == LIFE_STAGE_NEWBORN
