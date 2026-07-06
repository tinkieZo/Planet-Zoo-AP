"""Animal-market control: SpeciesMarketGate + ExhibitMarketGate (autofill allow-lists) +
ScheduleSpawner (the scenario-market "Goodwin House hijack").

Three mechanisms, all rooted at park exchange managers:

1. SpeciesMarketGate (HABITAT, LocalAnimalExchange @park+0x168) / ExhibitMarketGate (EXHIBIT,
   ExhibitAnimalExchange @park+0x1C0) - filter the AUTOFILL candidate pool via a default whitelist's
   include-set. On Scenario_15_Empty BOTH markets autofill (unlike the old Scenario_01 base where the
   pool query was dormant), so both gates are live. They share _AutofillMarketGate; only the manager
   offsets differ (the two managers are NOT layout-parallel). The client applies the SAME unlocked-
   species id set to both: each pool holds only its own type, so it self-filters.

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
   purchase-level belt-and-braces). NOTE: on Scenario_15_Empty the schedule has 0 usable
   non-reward slots, so the autofill gate (above) is the primary lever there.

RE: memory ap-custom-scenario.md §5a + tools/_decomp/market/ + tools/_decomp/exhibit/. Managers
located via the same stable master-root chain the cash/rating anchors use (anchors.json zoo_rating note):
    PARK         = *(*(base+0x29446A0)+0x20) + 0x658     (embedded, no final deref)
    exchange_mgr = *(PARK + 0x168)   |   exhibit_mgr = *(PARK + 0x1C0)
Whitelist object (DEFAULT @ mgr+OFF): exclude-set @+0x00, include-ACTIVE flag (u8)
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
import time
from ctypes import wintypes
from typing import List, Optional, Sequence

logger = logging.getLogger("PZClient")

# -- shared root / park ---------------------------------------------------------
MASTER_ROOT_RVA = 0x29446A0
PARK_CHAIN = (0x20, 0x658)        # *(root) -> +0x20 deref -> +0x658 added = PARK (embedded)

# -- HABITAT (LocalAnimalExchange) manager / whitelist offsets ------------------
OFF_PARK_EXCHANGE_MGR = 0x168     # *(PARK + 0x168) = LocalAnimalExchange manager
OFF_MGR_ACTIVATION = 0x210        # u8: pool-rebuild runs only when set
OFF_MGR_POOL_DIRTY = 0x211        # u8: write 0 -> Advance rebuilds the candidate pool next tick
OFF_MGR_MODE = 0x41C             # u8: 0=scenario 1=sandbox 2=challenge/franchise 3=off
OFF_MGR_DEFAULT_WHITELIST = 0x3C0  # the default whitelist OBJECT (used when active id misses)
# Active-whitelist SELECTION (FUN_14a098760): the rebuild uses the ACTIVE whitelist, chosen by an id
# field looked up in the map @mgr+0x390; on a MISS it returns the default @+0x3c0. The id field is
# +0x418 in sandbox (mode 1) and +0x3b8 otherwise (incl. scenario mode 0). Clearing the id so the
# lookup misses routes the rebuild to the default whitelist (what apply_unlocked writes) - the
# memory-only equivalent of the script's SetLocalAnimalExchangeActiveWhitelist("").
OFF_MGR_ACTIVE_WL_ID_SCEN = 0x3B8     # i32 active whitelist id (mode != 1)
OFF_MGR_ACTIVE_WL_ID_SANDBOX = 0x418  # i32 active whitelist id (mode == 1)

# whitelist-object field offsets (relative to the whitelist object base) - SAME for both managers
WL_INCLUDE_ACTIVE = 0x28          # u8: 1 = include-set is an allow-list filter
WL_INCLUDE_SET = 0x30             # int32 hash-set struct

# -- scenario schedule / live listings (the ScheduleSpawner surface, HABITAT) ----
OFF_MGR_LIVE_COUNT = 0x240        # i64: live listings count (what the UI shows)
OFF_MGR_LIVE_DATA = 0x248         # ptr: live listings array, stride 0x240
OFF_MGR_REWARDS_ENABLED = 0x270   # u8: gates bIsReward schedule entries
OFF_MGR_SCHED_COUNT = 0x278       # i64: schedule entries count
OFF_MGR_SCHED_DATA = 0x280        # ptr: schedule array, stride 0x260

SCHED_STRIDE = 0x260
LIVE_STRIDE = 0x240
LIVE_ENT_EXPIRY = 0x214           # f32 listing time-left; Advance removes a listing once this goes <0
                                  # (FUN_140ea1080 lines 248-265 -> FUN_140ea88e0). The gate only
                                  # filters FUTURE autofill spawns, so already-spawned listings of a
                                  # now-blocked species must be expired to clear them from the UI.

# -- autofill candidate POOL (the include-set filters THIS during the rebuild FUN_140ea0740, HABITAT) --
OFF_MGR_POOL_COUNT = 0x2A0        # i64 candidate-pool count
OFF_MGR_POOL_DATA = 0x2A8         # ptr candidate-pool array
OFF_MGR_POOL_CAP = 0x2B0          # i64 candidate-pool capacity
POOL_STRIDE = 0x0C
POOL_ENT_SPECIES = 0x04           # i32 species symbol id within a pool entry

# -- autofill FILL controls (the "bootstrap fill" lever, HABITAT) ---
OFF_MGR_TARGET = 0x26C            # i32 target live-listing count (Advance spawns until live >= this;
                                  # FUN_14A07D670 recomputes it from pool size * economy factor -> tiny
                                  # early-game pool => tiny target => near-empty market)
OFF_MGR_FORCE_SPAWN = 0x238       # u8 force-spawn flag (Advance spawns this tick regardless of the timer)

# -- EXHIBIT (ExhibitAnimalExchange) manager offsets ----------------------------
# NOT layout-parallel to the local exchange. RE'd from fn_140EAE9B0 (rebuild) + fn_140EAF260 (Advance);
# the whitelist filter is the SAME shape (include-active @obj+0x28, include-set @obj+0x30) at a
# different base. Live-confirmed on Scenario_15_Empty (pool collapsed to a 2-species allow-list).
OFF_PARK_EXHIBIT_MGR = 0x1C0      # *(PARK + 0x1C0) = ExhibitAnimalExchange manager
OFF_EXH_MODE = 0x304              # u8 mode byte (0 = scenario)
OFF_EXH_ACTIVATION = 0x1C0        # u8 ready flag (rebuild gates on it != 0)
OFF_EXH_POOL_DIRTY = 0x1C1        # u8: write 0 -> Advance (fn_140EAF260 ln 34) rebuilds the pool
OFF_EXH_DEFAULT_WHITELIST = 0x2A8  # default whitelist OBJECT (on active-id miss; fn_140EAE9B0 ln 71)
OFF_EXH_WL_ID_SCEN = 0x2A0        # i32 active whitelist id (mode != 1)
OFF_EXH_WL_ID_SANDBOX = 0x300     # i32 active whitelist id (mode == 1)
OFF_EXH_POOL_COUNT = 0x200        # i64 candidate-pool count (fn_140EAE9B0 ln 233)
OFF_EXH_POOL_DATA = 0x208         # ptr candidate-pool array (stride 0xc, species @+0x4)
OFF_EXH_LIVE_COUNT = 0x1C8        # i64 live listings count
OFF_EXH_LIVE_DATA = 0x1D0         # ptr live listings array, stride 0x180
EXH_LIVE_STRIDE = 0x180
EXH_ENT_SPECIES = 0x28            # i32 species symbol id within a live listing
EXH_LIVE_ENT_EXPIRY = 0x164       # f32 listing time-left (fn_140EAF260 ln 179)
OFF_EXH_TARGET = 0x1F4            # i32 target live-listing count (fn_140EAF260 ln 115: live < target -> spawn)
OFF_EXH_FORCE_SPAWN = 0x1C2       # u8 force-spawn flag (fn_140EAF260 ln 106; engine self-clears at target ln 138)

# schedule/listing entry field offsets (relative to the entry base, HABITAT schedule)
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

# Orphan-detection sentinel. Our include-set buffer is allocated with a SENTINEL_SIZE-byte prefix holding
# SENTINEL_MAGIC; the engine's include-set pointer is set to (base + SENTINEL_SIZE), so the magic sits
# immediately BEFORE the pointer the engine holds. If a prior client process is HARD-KILLED (Task Manager /
# crash) before restoring, the manager keeps pointing at our orphaned buffer; a fresh client recognises it
# by this magic and neutralises it (see _neutralize_orphan_includeset) so the engine never free()s a foreign
# pointer on park teardown = the Exit crash. 16 bytes, fixed offset (cap-independent).
SENTINEL_MAGIC = b"PZAP-MKT-ORPHAN!"
SENTINEL_SIZE = 0x10

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


def resolve_exchange_mgr(scanner, park_off: int = OFF_PARK_EXCHANGE_MGR) -> Optional[int]:
    """Resolve a park exchange-manager pointer (LocalAnimalExchange @park+0x168, the default, or
    ExhibitAnimalExchange @park+0x1C0), or None if not in a loaded zoo."""
    if not scanner.attached:
        return None
    from .signatures import resolve_root
    root = resolve_root(scanner, MASTER_ROOT_RVA)
    if root is None:
        return None
    park = scanner.resolve_pointer_chain(root, [0, *PARK_CHAIN])
    if park is None:
        return None
    mgr = scanner.read_qword(park + park_off)
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


class _AutofillMarketGate:
    """Shared logic for the two autofill-driven species markets (habitat + exhibit). Both gate their
    candidate pool through a default whitelist's include-set (include-active @obj+0x28, include-set
    @obj+0x30) during the rebuild, then a per-tick Advance spawns live listings from the gated pool.
    Subclasses supply the manager offsets (the managers are NOT layout-parallel).

    Species IDs are resolved via the RESEARCH MAP (ResearchReader.current_handle, keyed off stable
    welfare research-item ids) - the same restart-stable path PermitGate uses, so all the gates agree
    on the id set. VALIDATED live (2026-06-10): the research handle equals the registry symbol id for
    every species (incl. name-divergent Grey Wolf -> internal 'TimberWolf', Nile Hippo ->
    'Hippopotamus'), so resolving by name is unnecessary and fragile. RegistryResolver is kept only as
    the tool that proved this + a name->id fallback (apply_unlocked_names)."""

    # subclasses MUST set these (manager-relative offsets)
    _PARK_MGR: int          # park-struct offset to the manager pointer
    _MODE: int              # u8 mode byte (0 = scenario, the gateable autofill mode)
    _ACTIVATION: int        # u8 activation/ready flag
    _POOL_DIRTY: int        # u8: write 0 -> next Advance rebuilds the pool
    _DEFAULT_WL: int        # the default whitelist OBJECT (used when the active id misses)
    _WL_ID_SCEN: int        # i32 active whitelist id (scenario / mode != sandbox)
    _WL_ID_SANDBOX: int     # i32 active whitelist id (sandbox)
    _POOL_COUNT: int
    _POOL_DATA: int
    _POOL_STRIDE: int
    _POOL_SP: int
    _LIVE_COUNT: int
    _LIVE_DATA: int
    _LIVE_STRIDE: int
    _LIVE_SP: int
    _LIVE_EXPIRY: int
    _TARGET: int            # i32 target live-listing count (the bootstrap-fill lever)
    _FORCE_SPAWN: int       # u8 force-spawn flag (spawn now, don't wait for the pacing timer)

    # The engine recomputes TARGET from pool size every frame and repaces spawns itself, so waking the
    # autofill (POOL_DIRTY clear + re-route + force-spawn) every poll tick makes it rebuild the gated pool
    # per tick = visible game stutter. One wake per cooldown is plenty; listings persist for in-game months.
    _FILL_WAKE_COOLDOWN_S = 10.0

    def __init__(self, scanner, research=None, registry=None):
        self.scanner = scanner
        from .research import ResearchReader
        self.research = research or ResearchReader(scanner)
        self.registry = registry          # lazily created only if the name fallback is used
        self._buffer: Optional[int] = None          # our allocated set buffer in the target
        self._buffer_cap = 0
        self._mgr_cache: Optional[int] = None
        self._warned_unmapped: set = set()
        # The manager's PRE-GATE include-set state, captured once on the first apply. Restoring it before
        # the game tears the park down is what stops the engine from free()-ing OUR VirtualAllocEx buffer
        # (a foreign pointer -> heap corruption -> crash on Exit-to-Menu/Exit-Game). See restore()/shutdown().
        self._orig: Optional[dict] = None
        self._orphan_checked = False   # one-shot: neutralise a prior process's orphan before first capture
        self._fill_wake_last = 0.0     # monotonic time of the last autofill wake (see _FILL_WAKE_COOLDOWN_S)
        self._fill_raise_logged: Optional[tuple] = None  # last (cur, floor, pool) logged for a target raise

    # -- manager location ------------------------------------------------------

    def exchange_mgr(self) -> Optional[int]:
        """Resolve the manager pointer (or None if not in a loaded zoo)."""
        if not self._mgr_cache:
            self._mgr_cache = resolve_exchange_mgr(self.scanner, self._PARK_MGR)
        return self._mgr_cache

    def scenario_mode(self) -> bool:
        """True iff the market is in scenario mode (mode byte 0) - the autofill-driven mode the gate
        restricts; also the AP-session mode_check (habitat gate)."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            return self.scanner.read_bytes(mgr + self._MODE, 1)[0] == 0
        except Exception:
            return False

    def expire_blocked_listings(self, allowed_ids: Sequence[int]) -> int:
        """Force every LIVE listing whose species isn't in ``allowed_ids`` to expire on the next
        Advance tick (timer := very negative -> the engine removes it natively, no leak/refcount
        breakage). The autofill then refills the freed slots from the gated pool. Returns the count
        marked. Idempotent. This is what makes the gate visible IMMEDIATELY rather than only as
        listings slowly expire."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return 0
        allowed = {int(i) & 0xFFFFFFFF for i in allowed_ids}
        try:
            count = self.scanner.read_i64(mgr + self._LIVE_COUNT)
            data = self.scanner.read_qword(mgr + self._LIVE_DATA)
        except Exception:
            return 0
        if not data or not 0 <= count <= 1024:
            return 0
        marked = 0
        for i in range(count):
            ent = data + i * self._LIVE_STRIDE
            try:
                if (self.scanner.read_i32(ent + self._LIVE_SP) & 0xFFFFFFFF) not in allowed:
                    self.scanner.write_bytes(ent + self._LIVE_EXPIRY, struct.pack("<f", -1.0e9))
                    marked += 1
            except Exception:
                continue
        if marked:
            logger.info("market: expired %d live listing(s) of now-blocked species", marked)
        return marked

    def disable(self) -> bool:
        """Turn the gate OFF: clear include-active so the default whitelist stops filtering, and
        request a rebuild -> the autofill pool returns to the full 'ready' set."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            self.scanner.write_bytes(mgr + self._DEFAULT_WL + WL_INCLUDE_ACTIVE, b"\x00")
            self.scanner.write_bytes(mgr + self._POOL_DIRTY, b"\x00")
        except Exception as e:
            logger.warning("market: failed to disable gate: %s", e)
            return False
        return True

    def pool_species(self) -> List[int]:
        """Species ids in the autofill CANDIDATE POOL. This is what the include-set gate filters during
        the rebuild, so it's the direct read-back that proves the gate took: after apply_unlocked + a
        rebuild tick, the pool should equal the allow-list. [] if empty/unresolvable (count 0..4096)."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return []
        try:
            count = self.scanner.read_i64(mgr + self._POOL_COUNT)
            data = self.scanner.read_qword(mgr + self._POOL_DATA)
        except Exception:
            return []
        if not data or not 0 <= count <= 4096:
            return []
        try:
            return [self.scanner.read_i32(data + i * self._POOL_STRIDE + self._POOL_SP) for i in range(count)]
        except Exception:
            return []

    # -- the gate --------------------------------------------------------------

    def _alloc_buffer(self, size: int) -> Optional[int]:
        """(Re)allocate a target-process RW buffer for ``size`` DATA bytes, prefixed with SENTINEL_MAGIC.
        Returns the DATA pointer (base + SENTINEL_SIZE) - what the include-set is repointed at; self._buffer
        holds the allocation BASE (what we VirtualFreeEx). The sentinel lets a fresh client process detect
        this buffer as OURS if a prior process was hard-killed without restoring. Frees a prior allocation."""
        handle = self.scanner.pm.process_handle
        if self._buffer and size > self._buffer_cap:
            _k32.VirtualFreeEx(handle, ctypes.c_void_p(self._buffer), 0, _MEM_RELEASE)
            self._buffer = None
        if not self._buffer:
            total = max(size + SENTINEL_SIZE, 0x1000)
            got = _k32.VirtualAllocEx(handle, None, total, _MEM_COMMIT | _MEM_RESERVE, _PAGE_READWRITE)
            if not got:
                logger.warning("market: VirtualAllocEx failed (err %d)", ctypes.get_last_error())
                return None
            self._buffer = int(got)
            self._buffer_cap = total - SENTINEL_SIZE
            try:
                self.scanner.write_bytes(self._buffer, SENTINEL_MAGIC)  # tag the base for orphan detection
            except Exception:
                pass
        return self._buffer + SENTINEL_SIZE

    def use_default_whitelist(self) -> bool:
        """Route the autofill rebuild to the DEFAULT whitelist (the one apply_unlocked writes) by
        clearing the active-whitelist id so the map lookup MISSES and returns the default. Without this
        the scenario's active whitelist (include-active=0 = no filter) is used and the gate is ignored.
        Sets pool-dirty so the next Advance rebuilds against the default."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            self.scanner.write_i32(mgr + self._WL_ID_SCEN, 0)
            self.scanner.write_i32(mgr + self._WL_ID_SANDBOX, 0)
            self.scanner.write_bytes(mgr + self._POOL_DIRTY, b"\x00")
        except Exception as e:
            logger.warning("market: failed to clear active whitelist id: %s", e)
            return False
        return True

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
        wl = mgr + self._DEFAULT_WL
        inc = wl + WL_INCLUDE_SET
        self._neutralize_orphan_includeset(mgr, wl, inc)  # clear a prior process's orphan FIRST (else we'd
        self._capture_original(mgr, wl, inc)   # capture it as the "original" + restore back to it later)
        try:
            self.scanner.write_bytes(buf, blob)
            # set struct: buffer ptr (+0x18), capacity (+0x10), count (+0x08, best-effort)
            self.scanner.write_i64(inc + SET_BUFFER, buf)
            self.scanner.write_i64(inc + SET_CAP, cap)
            self.scanner.write_i64(inc + SET_COUNT, len({int(s) & 0xFFFFFFFF for s in species_ids}))
            self.scanner.write_bytes(wl + WL_INCLUDE_ACTIVE, b"\x01")    # include-set is now an allow-list
            self.scanner.write_i32(mgr + self._WL_ID_SCEN, 0)     # route the rebuild to THIS default
            self.scanner.write_i32(mgr + self._WL_ID_SANDBOX, 0)  # whitelist (else a scenario one wins)
            self.scanner.write_bytes(mgr + self._POOL_DIRTY, b"\x00")  # Advance rebuilds the pool
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
        """Belt-and-braces: set the manager's activation flag so the pool rebuild runs even if the
        script-side activation didn't land. The script call is preferred (proven safe); this is the
        client-side alternative noted in the recipe."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return False
        try:
            self.scanner.write_bytes(mgr + self._ACTIVATION, b"\x01")
        except Exception:
            return False
        return True

    def ensure_min_fill(self, min_listings: int = 8) -> "Optional[int]":
        """Bootstrap fill: raise the autofill TARGET live-listing count toward min_listings so an
        early-game market with a tiny unlocked pool still offers a usable selection AND (with the
        engine's per-species spawn backoff) ~one of each unlocked species. The ask is CAPPED at
        ~2 listings per unlocked species (the engine's own observed target ratio) so a tiny pool is
        never asked for an unreachable count - once live listings meet a reachable target the wake
        below stops firing entirely, instead of rebuilding the pool every cooldown forever.
        SOFT: the engine still paces spawns + only spawns from the GATED pool, so it can't offer anything
        locked and won't strictly guarantee one-of-each. No-op once the target already meets the floor.
        SANITY-GUARDED: only writes if the current target reads as a sane count (0..1024) - protects
        against a wrong offset / the manager not being ready. Returns the ensured target, else None.
        Call each tick (cheap); the engine fills toward it at its own pace."""
        mgr = self.exchange_mgr()
        if mgr is None:
            return None
        try:
            cur = self.scanner.read_i32(mgr + self._TARGET)
        except Exception:
            return None
        if not 0 <= cur <= 1024:           # not a sane listing count -> wrong offset / manager not ready
            return None
        pool = len(self.pool_species())
        floor = max(pool, min(int(min_listings), 2 * pool))
        target = max(cur, floor)
        try:
            if cur < floor:                 # keep the target raised (the engine recomputes it every frame)
                self.scanner.write_i32(mgr + self._TARGET, floor)
                if self._fill_raise_logged != (cur, floor, pool):   # log per state change, not per tick
                    self._fill_raise_logged = (cur, floor, pool)
                    logger.info("market: raised fill target %d -> %d (pool=%d) @mgr 0x%X", cur, floor, pool, mgr)
            live = self.scanner.read_i64(mgr + self._LIVE_COUNT)
            now = time.monotonic()
            if 0 <= live < target and now - self._fill_wake_last >= self._FILL_WAKE_COOLDOWN_S:
                # under-filled -> WAKE the autofill, don't just bump the target.
                # Raising the target + force-spawn alone does NOT trigger a rebuild: after the initial
                # listings expire the engine leaves the market dormant (observed: empty for in-game
                # months until a new permit). The permit path refills precisely because apply_unlocked
                # clears POOL_DIRTY -> Advance rebuilds the gated pool + spawns. Do the same here: re-route
                # to our default whitelist (in case the engine reset the active-id) + mark the pool dirty.
                # THROTTLED to one wake per cooldown - a per-tick pool rebuild stutters the game, and with
                # a tiny pool the engine may never reach the target, so this would otherwise fire forever.
                self._fill_wake_last = now
                self.scanner.write_i32(mgr + self._WL_ID_SCEN, 0)
                self.scanner.write_i32(mgr + self._WL_ID_SANDBOX, 0)
                self.scanner.write_bytes(mgr + self._POOL_DIRTY, b"\x00")
                self.scanner.write_bytes(mgr + self._FORCE_SPAWN, b"\x01")
        except Exception as e:
            logger.warning("market: ensure_min_fill write failed: %s", e)
            return None
        return target

    def _neutralize_orphan_includeset(self, mgr: int, wl: int, inc: int) -> None:
        """Before the FIRST capture/apply this session, check whether the manager's include-set still points
        at one of OUR buffers orphaned by a prior client process that was hard-killed (so its restore never
        ran). Such a buffer carries SENTINEL_MAGIC in the SENTINEL_SIZE bytes immediately before the engine's
        pointer. If found, reset the include-set to EMPTY (null buffer/cap/count, include-active off) so the
        engine frees nothing on park teardown - this prevents the foreign-pointer free crash even when the
        prior exit bypassed every cleanup path. One-shot; a no-op for a genuine engine buffer (no magic)."""
        if self._orphan_checked:
            return
        self._orphan_checked = True
        try:
            p = self.scanner.read_i64(inc + SET_BUFFER)
            if not p:
                return
            if self.scanner.read_bytes(p - SENTINEL_SIZE, len(SENTINEL_MAGIC)) != SENTINEL_MAGIC:
                return  # a genuine engine buffer (or unreadable) - never touch it
            self.scanner.write_i64(inc + SET_BUFFER, 0)
            self.scanner.write_i64(inc + SET_CAP, 0)
            self.scanner.write_i64(inc + SET_COUNT, 0)
            self.scanner.write_bytes(wl + WL_INCLUDE_ACTIVE, b"\x00")
            self.scanner.write_i32(mgr + self._WL_ID_SCEN, 0)
            self.scanner.write_i32(mgr + self._WL_ID_SANDBOX, 0)
            self.scanner.write_bytes(mgr + self._POOL_DIRTY, b"\x00")
            logger.warning("market: found an ORPHAN include-set buffer @0x%X from a prior unclean exit - "
                           "neutralised to empty (the engine now frees nothing on Exit = no crash)", p)
        except Exception as e:
            logger.warning("market: orphan include-set check failed (%s) - continuing", e)

    def _capture_original(self, mgr: int, wl: int, inc: int) -> None:
        """Snapshot the manager's pre-gate include-set state ONCE (before the first apply overwrites it),
        so restore() can put the engine's OWN buffer pointer + flags back and the game frees its own
        allocation (not our VirtualAllocEx buffer) when the park tears down."""
        if self._orig is not None:
            return
        try:
            self._orig = {
                "mgr": mgr,
                "buffer": self.scanner.read_i64(inc + SET_BUFFER),
                "cap": self.scanner.read_i64(inc + SET_CAP),
                "count": self.scanner.read_i64(inc + SET_COUNT),
                "active": self.scanner.read_bytes(wl + WL_INCLUDE_ACTIVE, 1),
                "wl_scen": self.scanner.read_i32(mgr + self._WL_ID_SCEN),
                "wl_sandbox": self.scanner.read_i32(mgr + self._WL_ID_SANDBOX),
            }
        except Exception as e:
            logger.warning("market: couldn't capture original include-set (restore will be skipped): %s", e)
            self._orig = None

    def restore(self) -> bool:
        """Re-point the manager's include-set back to the GAME's original buffer/cap/count + flags, so the
        engine's park teardown frees its OWN allocation instead of our VirtualAllocEx buffer (which would
        be a foreign-pointer free -> crash on Exit). Re-RESOLVES the manager (never writes through a stale/
        freed pointer) and only writes if it still resolves to the SAME manager we gated. No-op if we never
        applied. Call on park unload / before freeing our buffer / on shutdown. Idempotent."""
        if self._orig is None:
            return False
        self._mgr_cache = None
        mgr = self.exchange_mgr()                 # re-resolve; None if the park already unloaded
        if mgr is None or mgr != self._orig.get("mgr"):
            self._orig = None                     # park gone/changed - can't safely write; drop (buffer leak is benign)
            return False
        wl = mgr + self._DEFAULT_WL
        inc = wl + WL_INCLUDE_SET
        try:
            self.scanner.write_i64(inc + SET_BUFFER, self._orig["buffer"])
            self.scanner.write_i64(inc + SET_CAP, self._orig["cap"])
            self.scanner.write_i64(inc + SET_COUNT, self._orig["count"])
            self.scanner.write_bytes(wl + WL_INCLUDE_ACTIVE, self._orig["active"])
            self.scanner.write_i32(mgr + self._WL_ID_SCEN, self._orig["wl_scen"])
            self.scanner.write_i32(mgr + self._WL_ID_SANDBOX, self._orig["wl_sandbox"])
            self.scanner.write_bytes(mgr + self._POOL_DIRTY, b"\x00")   # rebuild against the restored state
        except Exception as e:
            logger.warning("market: restore failed: %s", e)
            return False
        logger.info("market: restored original include-set @mgr 0x%X (gate off; engine owns its buffer again)", mgr)
        self._orig = None
        return True

    def shutdown(self) -> None:
        """Restore the manager's original include-set (so the engine frees its OWN buffer, not ours), THEN
        free our buffer - but ONLY if the engine no longer points at it. Safe to call on park unload or
        process exit - restore() re-resolves the manager and skips if the park is already gone.

        Free ONLY when it's provably safe: restore() succeeded (engine now points at its own buffer) OR the
        park is gone (the manager + its include-set are already freed, so our buffer is unreferenced). If we
        can't confirm either, we LEAK our buffer instead of freeing it: a leaked VirtualAllocEx page is
        benign (reclaimed when the game exits), but freeing a buffer the engine still references leaves it a
        dangling pointer -> the engine free()s it on park teardown -> crash on Exit. The leak is the safe side."""
        restored = False
        try:
            restored = self.restore()
        except Exception:
            logger.exception("market: restore during shutdown failed")
        self._mgr_cache = None
        park_gone = False
        try:
            park_gone = self.exchange_mgr() is None
        except Exception:
            park_gone = False
        if self._buffer and self.scanner.attached and (restored or park_gone):
            try:
                _k32.VirtualFreeEx(self.scanner.pm.process_handle,
                                   ctypes.c_void_p(self._buffer), 0, _MEM_RELEASE)
            except Exception:
                pass
        elif self._buffer:
            logger.warning("market: include-set re-point unconfirmed - LEAKING our buffer 0x%X (benign, "
                           "reclaimed on game exit) rather than risk a dangling free/crash", self._buffer)
        self._buffer = None
        self._buffer_cap = 0
        self._mgr_cache = None


class SpeciesMarketGate(_AutofillMarketGate):
    """The HABITAT animal market (LocalAnimalExchange @park+0x168). Live-proven 2026-06-18 +
    2026-06-21 (install build): apply a 2-species allow-list -> the habitat market collapses to it."""
    _PARK_MGR = OFF_PARK_EXCHANGE_MGR
    _MODE = OFF_MGR_MODE
    _ACTIVATION = OFF_MGR_ACTIVATION
    _POOL_DIRTY = OFF_MGR_POOL_DIRTY
    _DEFAULT_WL = OFF_MGR_DEFAULT_WHITELIST
    _WL_ID_SCEN = OFF_MGR_ACTIVE_WL_ID_SCEN
    _WL_ID_SANDBOX = OFF_MGR_ACTIVE_WL_ID_SANDBOX
    _POOL_COUNT = OFF_MGR_POOL_COUNT
    _POOL_DATA = OFF_MGR_POOL_DATA
    _POOL_STRIDE = POOL_STRIDE
    _POOL_SP = POOL_ENT_SPECIES
    _LIVE_COUNT = OFF_MGR_LIVE_COUNT
    _LIVE_DATA = OFF_MGR_LIVE_DATA
    _LIVE_STRIDE = LIVE_STRIDE
    _LIVE_SP = ENT_SPECIES
    _LIVE_EXPIRY = LIVE_ENT_EXPIRY
    _TARGET = OFF_MGR_TARGET
    _FORCE_SPAWN = OFF_MGR_FORCE_SPAWN


class ExhibitMarketGate(_AutofillMarketGate):
    """The EXHIBIT animal market (ExhibitAnimalExchange @park+0x1C0). On Scenario_15_Empty this market
    AUTOFILLS (unlike the old Scenario_01 base, where the pool was dormant), so it needs the same
    include-set gate as the habitat market. Offsets RE'd from fn_140EAE9B0 (rebuild) + fn_140EAF260
    (Advance); the whitelist filter is the same shape at a different base. The client applies the SAME
    unlocked-species id set to both gates: the exhibit pool holds only exhibit species, so it
    self-filters to the unlocked exhibit species. Live-proven 2026-06-21 (pool 5 -> 2 allow-list)."""
    _PARK_MGR = OFF_PARK_EXHIBIT_MGR
    _MODE = OFF_EXH_MODE
    _ACTIVATION = OFF_EXH_ACTIVATION
    _POOL_DIRTY = OFF_EXH_POOL_DIRTY
    _DEFAULT_WL = OFF_EXH_DEFAULT_WHITELIST
    _WL_ID_SCEN = OFF_EXH_WL_ID_SCEN
    _WL_ID_SANDBOX = OFF_EXH_WL_ID_SANDBOX
    _POOL_COUNT = OFF_EXH_POOL_COUNT
    _POOL_DATA = OFF_EXH_POOL_DATA
    _POOL_STRIDE = POOL_STRIDE
    _POOL_SP = POOL_ENT_SPECIES
    _LIVE_COUNT = OFF_EXH_LIVE_COUNT
    _LIVE_DATA = OFF_EXH_LIVE_DATA
    _LIVE_STRIDE = EXH_LIVE_STRIDE
    _LIVE_SP = EXH_ENT_SPECIES
    _LIVE_EXPIRY = EXH_LIVE_ENT_EXPIRY
    _TARGET = OFF_EXH_TARGET
    _FORCE_SPAWN = OFF_EXH_FORCE_SPAWN


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
# SpeciesMarketGate (habitat): live-proven 2026-06-18 + 2026-06-21 on the install build - apply a 2-species
#   allow-list -> habitat candidate pool + market collapse to it. count@+0x08 semantics still best-effort.
# ExhibitMarketGate: offsets RE'd from fn_140EAE9B0 (rebuild) + fn_140EAF260 (Advance) and live-proven
#   2026-06-21: pool 5 -> {DeathAdder,Tarantula}, live listings collapsed to the allow-list. The client
#   applies the SAME unlocked-id set to both gates (each pool self-filters by type).
# ScheduleSpawner: tag-spawn hijack RE'd static + boot-validated 2026-06-10, but DEAD on Scenario_15_Empty
#   (1 reward-only schedule entry, 0 usable slots) -> the autofill gates are primary there.
