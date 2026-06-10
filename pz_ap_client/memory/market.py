"""Animal-market control: SpeciesMarketGate (autofill allow-list) + ScheduleSpawner
(the scenario-market "Goodwin House hijack").

Two complementary mechanisms, both rooted at the LocalAnimalExchange manager:

1. SpeciesMarketGate - filters the AUTOFILL candidate pool via the default whitelist's
   include-set. Live-tested 2026-06-10: the write works, but in scenario mode the
   autofill pool is DORMANT (the FDB query returns zero rows), so this gates nothing
   there. It is the correct gate for sandbox/autofill-driven markets only.

2. ScheduleSpawner - drives the SCENARIO market, which is fed exclusively from the
   schedule array @mgr+0x278/+0x280 (stride 0x260). Vanilla staging (Goodwin House
   tutorial: market fills as objectives complete) works by objective code submitting
   RequestExchangeListingSpawnMessage(tag); the native handler (FUN_14A176870) merely
   walks the schedule and sets entry+0x24A=1 on tag match - Advance (FUN_140EA1080,
   mode 0) then spawns any entry with +0x249==0 and (+0x24A set or spawn time
   reached). Spawn-prep (FUN_14A097600) with entry+0x24B==1 (the ctor default)
   REGENERATES the animal from the species id @entry+0x10, so repointing a dormant
   entry's species id and firing +0x24A natively lists ANY species. Because nothing
   ever submits our entries' tags, the AP scenario market is empty until the client
   spawns listings => only unlocked species ever appear (PermitGate stays as the
   purchase-level belt-and-braces).

RE: memory ap-custom-scenario.md §5a + tools/_decomp/market/. Manager located via the
same stable master-root chain the cash/rating anchors use (anchors.json zoo_rating note):
    PARK         = *(*(base+0x29446A0)+0x20) + 0x658     (embedded, no final deref)
    exchange_mgr = *(PARK + 0x168)
Whitelist object (DEFAULT @ mgr+0x3c0): exclude-set @+0x00, include-ACTIVE flag (u8)
@+0x28, include-set @+0x30. Each set is an int32 open-addressing hash-set:
    +0x08 count, +0x10 capacity (pow2), +0x18 buffer = occupancy bitvector
    (capacity bits, 64-bit words; byte size ((cap>>3)+7)&~7) followed by int32 keys[cap].
Lookup (FUN_1444E1A90) reads only cap/buffer; count@+0x08 is written best-effort
(its exact use is the one UNCONFIRMED field - see "LIVE VALIDATION" below).
"""

from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes
from typing import List, Optional, Sequence

logger = logging.getLogger("PZClient")

# -- manager / whitelist offsets ----------------------------------------------
MASTER_ROOT_RVA = 0x29446A0
PARK_CHAIN = (0x20, 0x658)        # *(root) -> +0x20 deref -> +0x658 added = PARK (embedded)
OFF_PARK_EXCHANGE_MGR = 0x168     # *(PARK + 0x168) = LocalAnimalExchange manager
OFF_MGR_ACTIVATION = 0x210        # u8: pool-rebuild runs only when set
OFF_MGR_POOL_DIRTY = 0x211        # u8: write 0 -> Advance rebuilds the candidate pool next tick
OFF_MGR_MODE = 0x41C             # u8: 0=scenario 1=sandbox 2=challenge/franchise 3=off
OFF_MGR_DEFAULT_WHITELIST = 0x3C0  # the default whitelist OBJECT (used when active id misses)

# whitelist-object field offsets (relative to the whitelist object base)
WL_INCLUDE_ACTIVE = 0x28          # u8: 1 = include-set is an allow-list filter
WL_INCLUDE_SET = 0x30             # int32 hash-set struct

# -- scenario schedule / live listings (the ScheduleSpawner surface) -----------
OFF_MGR_LIVE_COUNT = 0x240        # i64: live listings count (what the UI shows)
OFF_MGR_LIVE_DATA = 0x248         # ptr: live listings array, stride 0x240
OFF_MGR_REWARDS_ENABLED = 0x270   # u8: gates bIsReward schedule entries
OFF_MGR_SCHED_COUNT = 0x278       # i64: schedule entries count
OFF_MGR_SCHED_DATA = 0x280        # ptr: schedule array, stride 0x260

SCHED_STRIDE = 0x260
LIVE_STRIDE = 0x240

# schedule/listing entry field offsets (relative to the entry base)
ENT_SPECIES = 0x10                # i32 species symbol id (== research-map handle)
ENT_FEMALE = 0x68                 # u8 bFemale (read by the +0x24B==1 generate path)
ENT_COST_FLAGS = 0x1F8            # u8: |0x40 -> cost block dirty, FUN_14A07F780 recomputes
ENT_TAG = 0x220                   # ptr -> native string {len i64 @+0x00, refcount @+0x10, chars @+0x14}
ENT_USE_SPAWNTIME = 0x244         # u8 bUseSpawnTime (0 -> entry only fires via +0x24A)
ENT_SPAWNED = 0x249               # u8: 1 = consumed; clear to re-arm the slot
ENT_IMMEDIATE = 0x24A             # u8: 1 = spawn on next Advance tick (what the tag handler sets)
ENT_GEN_MODE = 0x24B              # u8: 1 = regenerate animal from ENT_SPECIES (ctor default); 0 = named template
ENT_REWARD = 0x258                # u8 bIsReward

# hash-set struct field offsets (relative to the set base)
SET_COUNT = 0x08
SET_CAP = 0x10
SET_BUFFER = 0x18

# -- VirtualAllocEx plumbing (data alloc in the target; hook.py's _alloc_near is code-only) --
_MEM_COMMIT = 0x1000
_MEM_RESERVE = 0x2000
_MEM_RELEASE = 0x8000
_PAGE_READWRITE = 0x04
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.VirtualAllocEx.restype = ctypes.c_void_p
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]


def set_hash(key: int, cap: int) -> int:
    """Replicate the int32 hash-set slot hash (FUN_1444E1A90). ``cap`` must be a power of two."""
    x = (key * 0x1001) & 0xFFFFFFFF
    x = (((x >> 0x16) ^ x) * 0x11) & 0xFFFFFFFF
    x = (((x >> 0x09) ^ x) * 0x401) & 0xFFFFFFFF
    x = (((x >> 0x02) ^ x) * 0x81) & 0xFFFFFFFF
    return ((x >> 0x0C) ^ x) & (cap - 1)


def _next_pow2_cap(n: int) -> int:
    """Smallest power-of-two capacity that keeps the set under ~70% load (>=8)."""
    cap = 8
    need = max(1, n) * 10 // 7 + 1
    while cap < need:
        cap <<= 1
    return cap


def build_int32_set(keys: Sequence[int]) -> "tuple[int, bytes]":
    """Build the in-memory image of an int32 open-addressing hash-set (occupancy bitvector + keys[]).

    Returns ``(capacity, blob)`` where ``blob`` is what lives at the set's buffer pointer (+0x18):
    a 64-bit-word occupancy bitvector (capacity bits) immediately followed by ``int32 keys[capacity]``.
    Empty key slots are left 0; the bitvector is authoritative (matches the lookup, which stops at the
    first clear occupancy bit). Mirrors the game's insert path: hash -> linear probe -> first free slot."""
    uniq = list(dict.fromkeys(int(k) & 0xFFFFFFFF for k in keys))
    cap = _next_pow2_cap(len(uniq))
    bitvec_bytes = ((cap >> 3) + 7) & ~7         # 64-bit-word aligned, >= cap bits
    occ = bytearray(bitvec_bytes)
    table = [0] * cap
    for key in uniq:
        slot = set_hash(key, cap)
        while (occ[slot >> 3] >> (slot & 7)) & 1:   # occupied -> linear probe with wraparound
            slot = (slot + 1) & (cap - 1)
        occ[slot >> 3] |= 1 << (slot & 7)
        table[slot] = key
    blob = bytes(occ) + struct.pack("<%dI" % cap, *table)
    return cap, blob


def resolve_exchange_mgr(scanner) -> Optional[int]:
    """Resolve the LocalAnimalExchange manager pointer (or None if not in a loaded zoo)."""
    if not scanner.attached:
        return None
    from .signatures import resolve_root
    root = resolve_root(scanner, MASTER_ROOT_RVA)
    if root is None:
        return None
    park = scanner.resolve_pointer_chain(root, [0, *PARK_CHAIN])
    if park is None:
        return None
    mgr = scanner.read_qword(park + OFF_PARK_EXCHANGE_MGR)
    return mgr or None


def read_native_string(scanner, str_ptr: int, max_len: int = 64) -> str:
    """Read the engine's refcounted string {len i64 @+0x00, chars @+0x14} (tag strings)."""
    if not str_ptr:
        return ""
    try:
        n = scanner.read_i64(str_ptr)
        if not 0 < n <= max_len:
            return ""
        return scanner.read_bytes(str_ptr + 0x14, n).decode("ascii", errors="replace")
    except Exception:
        return ""


class SpeciesMarketGate:
    """Species IDs are resolved via the RESEARCH MAP (ResearchReader.current_handle, keyed off stable
    welfare research-item ids) - the same restart-stable path PermitGate uses, so both gates agree on the
    id set. This was VALIDATED live (2026-06-10): the research handle equals the registry symbol id for
    every species (incl. the name-divergent Grey Wolf -> internal 'TimberWolf' and Nile Hippo ->
    'Hippopotamus'), so resolving by name is unnecessary and fragile. The RegistryResolver is kept only as
    the tool that proved this + a name->id fallback (apply_unlocked_names)."""
    def __init__(self, scanner, research=None, registry=None):
        self.scanner = scanner
        from .research import ResearchReader
        self.research = research or ResearchReader(scanner)
        self.registry = registry          # lazily created only if the name fallback is used
        self._buffer: Optional[int] = None          # our allocated set buffer in the target
        self._buffer_cap = 0
        self._mgr_cache: Optional[int] = None
        self._warned_unmapped: set = set()

    # -- manager location ------------------------------------------------------

    def exchange_mgr(self) -> Optional[int]:
        """Resolve the LocalAnimalExchange manager pointer (or None if not in a loaded zoo)."""
        if not self._mgr_cache:
            self._mgr_cache = resolve_exchange_mgr(self.scanner)
        return self._mgr_cache

    # -- the gate --------------------------------------------------------------

    def _alloc_buffer(self, size: int) -> Optional[int]:
        """(Re)allocate a target-process RW buffer big enough for ``size`` bytes. Frees a prior one."""
        handle = self.scanner.pm.process_handle
        if self._buffer and size > self._buffer_cap:
            _k32.VirtualFreeEx(handle, ctypes.c_void_p(self._buffer), 0, _MEM_RELEASE)
            self._buffer = None
        if not self._buffer:
            got = _k32.VirtualAllocEx(handle, None, max(size, 0x1000),
                                      _MEM_COMMIT | _MEM_RESERVE, _PAGE_READWRITE)
            if not got:
                logger.warning("market: VirtualAllocEx failed (err %d)", ctypes.get_last_error())
                return None
            self._buffer = int(got)
            self._buffer_cap = max(size, 0x1000)
        return self._buffer

    def apply_unlocked(self, species_ids: Sequence[int]) -> bool:
        """Install the allow-list: only ``species_ids`` (their symbol ids) may spawn as listings.

        Builds the int32 set image, writes it into a target buffer, repoints the DEFAULT whitelist's
        include-set at it (count/cap/buffer), flips include-active=1 and pool-dirty=0 so Advance rebuilds
        the candidate pool. Idempotent: call again with the new set on every AP species unlock. Returns
        False (no change) if the manager isn't resolvable or a write fails."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        cap, blob = build_int32_set(species_ids)
        buf = self._alloc_buffer(len(blob))
        if buf is None:
            return False
        wl = mgr + OFF_MGR_DEFAULT_WHITELIST
        inc = wl + WL_INCLUDE_SET
        try:
            self.scanner.write_bytes(buf, blob)
            # set struct: buffer ptr (+0x18), capacity (+0x10), count (+0x08, best-effort)
            self.scanner.write_i64(inc + SET_BUFFER, buf)
            self.scanner.write_i64(inc + SET_CAP, cap)
            self.scanner.write_i64(inc + SET_COUNT, len({int(s) & 0xFFFFFFFF for s in species_ids}))
            self.scanner.write_bytes(wl + WL_INCLUDE_ACTIVE, b"\x01")    # include-set is now an allow-list
            self.scanner.write_bytes(mgr + OFF_MGR_POOL_DIRTY, b"\x00")  # Advance rebuilds the pool
        except Exception as e:
            logger.warning("market: failed to write species gate: %s", e)
            return False
        logger.info("market: allow-list set to %d species (cap=%d) @mgr 0x%X", len(species_ids), cap, mgr)
        return True

    def _resolve_handles(self, species_keys: Sequence[str]) -> List[int]:
        """Resolve species_keys -> current-session symbol ids via the research map (the proven, restart-
        stable path; same source as PermitGate, so the gates agree). Keys without a mapped welfare item
        are skipped with a one-time warning (add them to research.SPECIES_WELFARE_ITEM)."""
        snap = self.research._snapshot()
        if snap is None:
            return []
        out = []
        for key in species_keys:
            h = self.research.current_handle(key, snap)
            if h is None:
                if key not in self._warned_unmapped:
                    self._warned_unmapped.add(key)
                    logger.warning("market: no research handle for %r (add to research.SPECIES_WELFARE_ITEM)", key)
                continue
            out.append(h)
        return out

    def apply_unlocked_keys(self, species_keys: Sequence[str]) -> bool:
        """PRIMARY entry point: install the allow-list for these species_keys, resolving their symbol ids
        via the research map (handles == registry ids, validated live). Idempotent; call on every unlock."""
        return self.apply_unlocked(self._resolve_handles(species_keys))

    def apply_unlocked_names(self, species_keys: Sequence[str]) -> bool:
        """FALLBACK: resolve species NAMES to symbol ids via the registry (RegistryResolver), then apply.
        Prefer apply_unlocked_keys (research map) - names diverge from display names (Grey Wolf ->
        'TimberWolf', Nile Hippo -> 'Hippopotamus'), so callers would need PascalCase internal names here."""
        if self.registry is None:
            from .registry import RegistryResolver
            self.registry = RegistryResolver(self.scanner)
        resolved = self.registry.resolve_many(species_keys)
        missing = [k for k in species_keys if k not in resolved]
        if missing:
            logger.info("market: %d names not interned, skipped: %s", len(missing), missing[:8])
        return self.apply_unlocked(list(resolved.values()))

    def activate(self) -> bool:
        """Belt-and-braces: set the manager's activation flag (+0x210) so the pool rebuild runs even if
        the script-side ``SetLocalAnimalExchangeActiveWhitelist("")`` activation didn't land. The script
        call is preferred (proven safe); this is the client-side alternative noted in the recipe."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            self.scanner.write_bytes(mgr + OFF_MGR_ACTIVATION, b"\x01")
        except Exception:
            return False
        return True

    def shutdown(self) -> None:
        """Free our buffer. NOTE: this leaves the whitelist's include-set pointing at freed memory; only
        call on process exit, or re-point the set first. On a clean game exit the buffer dies with it."""
        if self._buffer and self.scanner.attached:
            try:
                _k32.VirtualFreeEx(self.scanner.pm.process_handle,
                                   ctypes.c_void_p(self._buffer), 0, _MEM_RELEASE)
            except Exception:
                pass
        self._buffer = None
        self._mgr_cache = None


class ScheduleSpawner:
    """Drive the SCENARIO animal market by hijacking the schedule array (the mechanism behind the
    Goodwin House tutorial's staged market). Each spawn repoints a dormant schedule entry's species
    id and fires the immediate flag; the engine's own Advance tick then generates the animal
    (spawn-prep FUN_14A097600, +0x24B==1 path), prices it, and lists it - fully native, no Set call.

    Species ids resolve via the research map (same restart-stable path as SpeciesMarketGate /
    PermitGate). Slots rotate round-robin over non-reward schedule entries; a consumed slot is
    re-armed by clearing +0x249, so 11 baked entries serve unlimited spawns.

    Write order matters: ENT_IMMEDIATE is written LAST - Advance reads entries under the manager
    lock per tick, and the entry must be fully retargeted before it becomes spawnable."""

    def __init__(self, scanner, research=None):
        self.scanner = scanner
        from .research import ResearchReader
        self.research = research or ResearchReader(scanner)
        self._mgr_cache: Optional[int] = None
        self._next_slot = 0
        self._warned_unmapped: set = set()

    def exchange_mgr(self) -> Optional[int]:
        if not self._mgr_cache:
            self._mgr_cache = resolve_exchange_mgr(self.scanner)
        return self._mgr_cache

    def scenario_mode(self) -> bool:
        """True iff the market is in scenario mode (mode byte 0) - the only mode whose
        Advance walks the schedule, i.e. the only mode this spawner can drive."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            return self.scanner.read_bytes(mgr + OFF_MGR_MODE, 1)[0] == 0
        except Exception:
            return False

    # -- introspection (probe/diagnostic surface) --------------------------------

    def schedule_entries(self) -> List[dict]:
        """Dump the schedule array. Empty list when unresolvable/empty (sane count: 1..256)."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return []
        try:
            count = self.scanner.read_i64(mgr + OFF_MGR_SCHED_COUNT)
            data = self.scanner.read_qword(mgr + OFF_MGR_SCHED_DATA)
        except Exception:
            return []
        if not data or not 0 < count <= 256:
            return []
        out = []
        for i in range(count):
            ent = data + i * SCHED_STRIDE
            try:
                out.append({
                    "index": i,
                    "addr": ent,
                    "species_id": self.scanner.read_i32(ent + ENT_SPECIES),
                    "tag": read_native_string(self.scanner, self.scanner.read_qword(ent + ENT_TAG) or 0),
                    "spawned": self.scanner.read_bytes(ent + ENT_SPAWNED, 1)[0],
                    "immediate": self.scanner.read_bytes(ent + ENT_IMMEDIATE, 1)[0],
                    "gen_mode": self.scanner.read_bytes(ent + ENT_GEN_MODE, 1)[0],
                    "female": self.scanner.read_bytes(ent + ENT_FEMALE, 1)[0],
                    "reward": self.scanner.read_bytes(ent + ENT_REWARD, 1)[0],
                })
            except Exception:
                break
        return out

    def live_species(self) -> List[int]:
        """Species ids of the LIVE listings (what the market UI shows) - spawn verification."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return []
        try:
            count = self.scanner.read_i64(mgr + OFF_MGR_LIVE_COUNT)
            data = self.scanner.read_qword(mgr + OFF_MGR_LIVE_DATA)
            if not data or not 0 <= count <= 1024:
                return []
            return [self.scanner.read_i32(data + i * LIVE_STRIDE + ENT_SPECIES) for i in range(count)]
        except Exception:
            return []

    # -- the spawner -------------------------------------------------------------

    def spawn_species_id(self, species_id: int, female: Optional[bool] = None) -> bool:
        """Retarget the next non-reward schedule slot to ``species_id`` and fire it. The listing
        appears on the next Advance tick (game unpaused). Returns False if the manager/schedule is
        unresolvable, the market is not in scenario mode (the schedule is only walked in mode 0),
        or a write fails."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            mode = self.scanner.read_bytes(mgr + OFF_MGR_MODE, 1)[0]
        except Exception:
            return False
        if mode != 0:
            logger.warning("market: schedule spawn needs scenario mode (mode byte=%d)", mode)
            return False
        entries = self.schedule_entries()
        slots = [e for e in entries if not e["reward"]]
        if not slots:
            logger.warning("market: no usable schedule slots (schedule empty?)")
            return False
        slot = slots[self._next_slot % len(slots)]
        self._next_slot += 1
        ent = slot["addr"]
        try:
            self.scanner.write_i32(ent + ENT_SPECIES, int(species_id))
            self.scanner.write_bytes(ent + ENT_GEN_MODE, b"\x01")        # regenerate from species id
            if female is not None:
                self.scanner.write_bytes(ent + ENT_FEMALE, b"\x01" if female else b"\x00")
            flags = self.scanner.read_bytes(ent + ENT_COST_FLAGS, 1)[0]
            self.scanner.write_bytes(ent + ENT_COST_FLAGS, bytes([flags | 0x40]))  # reprice for the new species
            self.scanner.write_bytes(ent + ENT_USE_SPAWNTIME, b"\x00")   # only fire via the immediate flag
            self.scanner.write_bytes(ent + ENT_SPAWNED, b"\x00")         # re-arm if the slot was consumed
            self.scanner.write_bytes(ent + ENT_IMMEDIATE, b"\x01")       # LAST: arms the spawn
        except Exception as e:
            logger.warning("market: schedule spawn write failed: %s", e)
            return False
        logger.info("market: schedule slot %d armed for species 0x%X", slot["index"], species_id)
        return True

    def spawn_keys(self, species_keys: Sequence[str], female: Optional[bool] = None) -> int:
        """Spawn one listing per species key (research-map id resolution, same as the gate).
        Returns the number of slots armed. Call per unlock - and again whenever a fresh listing
        for an unlocked species should be offered (e.g. after purchase/expiry)."""
        snap = self.research._snapshot()
        if snap is None:
            return 0
        armed = 0
        for key in species_keys:
            handle = self.research.current_handle(key, snap)
            if handle is None:
                if key not in self._warned_unmapped:
                    self._warned_unmapped.add(key)
                    logger.warning("market: no research handle for %r (add to research.SPECIES_WELFARE_ITEM)", key)
                continue
            if self.spawn_species_id(handle, female=female):
                armed += 1
        return armed


# ── STATUS / LIVE VALIDATION (handoff) ───────────────────────────────────────────────────────────────
# SpeciesMarketGate: write path live-tested 2026-06-10 (include-set lands, pool rebuild consumed) but the
#   autofill pool it gates is DORMANT in scenario mode (FDB query empty) - it only matters for autofill/
#   sandbox-driven markets. count@+0x08 semantics still unconfirmed (written best-effort).
# ScheduleSpawner: mechanism fully RE'd static (tag handler FUN_14A176870 = set +0x24A on tag match;
#   Advance spawns +0x24A entries; spawn-prep +0x24B==1 regenerates the animal from entry+0x10) and
#   BOOT-VALIDATED LIVE 2026-06-10 in the AP scenario: --schedule dumped the 11 baked entries sane;
#   --spawn plains_zebra produced a live PlainsZebra listing (species in NO schedule entry); re-arming
#   the consumed slot with american_bison produced a second live listing. Engine consumed +0x24A and
#   set +0x249 itself; no crash. Still user-eyeball: listing price matches the species (cost-dirty
#   honoured), purchase completes, the generated animal is healthy/valid.
