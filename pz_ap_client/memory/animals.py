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
