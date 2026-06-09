"""ResearchReader - per-species welfare-research completion + current species handle.

The research state lives in the research-system's items hashmap. Resolve the system via
a restart-stable master-root chain, then read records directly:

  research_system = *( *( *(base+0x2944690) + 0x18) + 0x350 )   # stable (cash/zoo root)
  items map       = research_system + 0xF8   { +0x08 count, +0x10 cap, +0x18 buckets }
                    open-addressing: occupancy bitmap of ((cap>>3)+7)&~7 bytes, then
                    value records of stride 0x58 at buckets+bitmap + slot*0x58.
  record (0x58):    +0x00 u32 = RESEARCH-ITEM ID (the map key; CONTENT-STABLE across
                    restarts), +0x0C u32 = LEVEL, +0x10 u32 = species HANDLE (a per-session
                    runtime index - NOT restart-stable), +0x3C byte = category (7 = animal),
                    +0x49 byte = STATUS (4 = ResearchedAndCompleted).

*** Restart-validation (2026-06-01) proved the species HANDLE is volatile across a game
restart (zebra 0x30A2->0x309D) while the research-item ID is stable. So we key everything
off the stable item id and resolve the current-session handle from the map at runtime. ***

`welfare_<species>` is the leveled animal-research track; complete when all STANDARD levels
are done (the high level ~10 is the optional "advanced research" the vet keeps running). A
species is identified by a representative welfare item id (its level-0 record); from that we
find the record's current handle, then require all of that handle's level<ADVANCED_LEVEL
cat-7 records to be status 4.

Degrades to "incomplete"/None (no false positives) if the chain/map can't be read.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Dict, Optional, Tuple

from .hook import (HookManager, make_research_gate, RESEARCH_GATE_COUNT,
                   RESEARCH_GATE_CATS)

logger = logging.getLogger("PZClient")

# master-root chain to the *address that holds* the research_system pointer (then deref).
RESEARCH_CHAIN = (0x2944690, 0x18, 0x350)
RESEARCH_CHAIN_ALT = (0x29446A0, 0x20, 0x518)  # 2nd proven path (guest/zoo root)
# Both chains above are LAYOUT-FRAGILE - they miss the research system in some saves (validated live
# 2026-06-08: a fully-loaded zoo where the primary walks a wrong cluster and the alt breaks at +0x518).
# The robust locator is a heap scan for the research-system VTABLE (the pointer at research_system+0x000)
# validating the items map at +0xF8. Build-specific; re-derive with tools/research_vtable_scan.py if a
# game patch breaks research resolution.
RESEARCH_VTABLE_RVA = 0x26C3490
SCAN_COOLDOWN_S = 5.0        # min seconds between vtable heap-scans when unresolved (don't stall poll)
ITEMS_MAP_OFF = 0xF8
REC_STRIDE = 0x58
REC_ITEMID = 0x00            # u32 research-item id (map key) - RESTART-STABLE content id
REC_LEVEL = 0x0C            # u32 level
REC_SPECIES = 0x10           # u32 species handle (per-session; resolve at runtime)
REC_CATEGORY = 0x3C          # byte: 7 = animal research
REC_STATUS = 0x49            # byte: 4 = ResearchedAndCompleted
ANIMAL_CATEGORY = 7
# ResearchStatus enum: 0 NotStarted, 1 Researchable, 2 Researching, 3 ResearchedButNotCompleted
# (researched but reward not yet "Collect"-ed), 4 ResearchedAndCompleted (collected). We treat
# 4 = complete (matches the game's "Completed" + the Collect-All step). To fire as soon as a
# research is *researched* (before Collect), change STATUS_COMPLETE checks to `>= 3`.
STATUS_COMPLETE = 4
ADVANCED_LEVEL = 10          # levels >= this are "advanced research" (optional, vet-ongoing)

# data.json species_key -> a representative welfare research-item id (its level-0 record).
# CONTENT-STABLE across restarts (unlike the species handle). Captured via tools/
# capture_species.py (buy-logger gives the session handle -> look up its records' item ids).
# KNOWN: zebra/warthog (confirmed identical across a restart). Extend with the gated trio
# (saltwater_croc/lowland_gorilla/giant_panda) + other welfare species as captured.
SPECIES_WELFARE_ITEM: Dict[str, int] = {
    # value = the species' level-0 welfare research-item id (restart-stable content id).
    "plains_zebra": 0xDAC,      # welfare run 0xDAC..0xDB1
    "common_warthog": 0x640,    # welfare run 0x640.. - not an AP key
    "saltwater_croc": 0x10CC,   # gated; run 0x10CC..0x10D2 (+adv 0x10D3)
    "lowland_gorilla": 0x1450,  # gated; run 0x1450..0x1459 (+adv 0x145A)
    "giant_panda": 0x834,       # gated; run 0x834..0x83A (+adv 0x83B)
    "nile_hippo": 0x9C4,        # PZ "Hippopotamus"; run 0x9C4..0x9CB
    "grey_wolf": 0x1324,        # PZ "Timber Wolf"; run 0x1324..0x132A
    "american_bison": 0x1F4,    # run 0x1F4..0x1FA
    "african_elephant": 0x13,   # run 0x13..0x19
    "bengal_tiger": 0x320,      # run 0x320..0x326
    "snow_leopard": 0x1194,     # run 0x1194..0x119A
    # all 10 AP welfare species captured.
}

# Non-welfare research keys -> their research-item id (mechanic research = category 3, no
# species handle). Mechanic research is a run of consecutive per-level item-ids; levels are
# researched sequentially, so "fully researched" = the FINAL level's status == 4. We map each
# key to its FINAL-level item id (complete when that item is status 4). Runs found via the
# sorted cat-3 item-ids (consecutive block = one research's levels):
#   Barriers     = 0x2793..0x2794 (2 levels); "Advanced Barriers" = the advanced level 0x2794
#   Drink Shops  = 0x2727..0x272D (7 levels); fully researched = level 7 = 0x272D
RESEARCH_ITEM: Dict[str, int] = {
    "habitat_advanced_barriers": 0x2794,   # Barriers advanced/final level
    "drink_shops": 0x272D,                 # Drink Shops final level (fully researched)
}


class ResearchReader:
    def __init__(self, scanner, welfare_items: Optional[Dict[str, int]] = None,
                 research_items: Optional[Dict[str, int]] = None):
        self.scanner = scanner
        self.items = dict(SPECIES_WELFARE_ITEM)
        if welfare_items:
            self.items.update(welfare_items)
        self.research_items = dict(RESEARCH_ITEM)
        if research_items:
            self.research_items.update(research_items)
        self._warned_unmapped: set = set()
        self._rs_cache: Optional[int] = None  # last good research-system address (revalidated each use)
        self._last_scan = 0.0                 # monotonic time of the last vtable heap-scan (throttle)

    def _map_ok(self, rs: int) -> bool:
        """Cheap check that rs+ITEMS_MAP_OFF is a readable items map - lets us tell the real research
        system from a valid-but-WRONG object a chain may dereference to. Matches _snapshot's own gate."""
        try:
            cap = struct.unpack("<q", self.scanner.read_bytes(rs + ITEMS_MAP_OFF + 0x10, 8))[0]
            bk = struct.unpack("<Q", self.scanner.read_bytes(rs + ITEMS_MAP_OFF + 0x18, 8))[0]
        except Exception:
            return False
        return 0 < cap <= (1 << 20) and 0x10000 < bk < (1 << 47)

    def _walk_chain(self, base: int, chain) -> Optional[int]:
        addr = base
        for off in chain:
            nxt = self.scanner.read_qword(addr + off)
            if not nxt:
                return None
            addr = nxt
        return addr

    def _research_system(self) -> Optional[int]:
        """Resolve the research system, robust across saves. The master-root chains are layout-fragile
        (they miss in some saves), so: reuse a cached object if it's still valid, else try the fast
        chains, else fall back to a heap scan for the system's vtable. Returns None if truly unreachable
        (no garbage fallback) so callers fail safe."""
        base = getattr(self.scanner, "module_base", None)
        if not base:
            return None
        if self._rs_cache and self._map_ok(self._rs_cache):
            return self._rs_cache                       # cached object still valid for this zoo (cheap)
        for chain in (RESEARCH_CHAIN, RESEARCH_CHAIN_ALT):
            addr = self._walk_chain(base, chain)
            if addr and self._map_ok(addr):
                self._rs_cache = addr
                return addr
        now = time.monotonic()                          # throttle the (expensive) heap scan so a
        if now - self._last_scan < SCAN_COOLDOWN_S:      # never-resolvable zoo can't stall the poll
            return None                                  # loop with a full scan every tick
        self._last_scan = now
        addr = self._scan_for_system(base)              # robust fallback: the chains missed
        if addr:
            self._rs_cache = addr
        return addr

    def _scan_for_system(self, base: int) -> Optional[int]:
        """Chain-independent locate: scan the heap for the research-system vtable and return the first
        object with a valid items map (skips the decoy object that shares the vtable). Slow (heap scan)
        but only runs when the chains miss, and the result is cached. No-op if the scanner can't scan."""
        scan = getattr(self.scanner, "scan_heap_for_qword", None)
        if scan is None:
            return None
        for obj in scan(base + RESEARCH_VTABLE_RVA):
            if self._map_ok(obj):
                logger.info("research: located via vtable scan @0x%X (master-root chains missed)", obj)
                return obj
        return None

    def _snapshot(self) -> Optional[Tuple[dict, dict]]:
        """Read the items map once: returns (by_item, by_handle) or None.
          by_item[item_id]   = (handle, level, status, category)
          by_handle[handle]  = list of (level, status, category)  for occupied records."""
        rs = self._research_system()
        if not rs:
            return None
        try:
            cap = struct.unpack("<q", self.scanner.read_bytes(rs + ITEMS_MAP_OFF + 0x10, 8))[0]
            bk = struct.unpack("<Q", self.scanner.read_bytes(rs + ITEMS_MAP_OFF + 0x18, 8))[0]
            if cap <= 0 or cap > (1 << 20) or not (0x10000 < bk < (1 << 47)):
                return None
            bm = ((cap >> 3) + 7) & ~7
            bitmap = self.scanner.read_bytes(bk, bm)
            recs = self.scanner.read_bytes(bk + bm, cap * REC_STRIDE)
        except Exception:
            return None
        by_item: dict = {}
        by_handle: dict = {}
        for i in range(cap):
            if not ((bitmap[i >> 3] >> (i & 7)) & 1):
                continue
            r = recs[i * REC_STRIDE:(i + 1) * REC_STRIDE]
            item = struct.unpack_from("<I", r, REC_ITEMID)[0]
            handle = struct.unpack_from("<I", r, REC_SPECIES)[0]
            level = struct.unpack_from("<I", r, REC_LEVEL)[0]
            cat, status = r[REC_CATEGORY], r[REC_STATUS]
            by_item[item] = (handle, level, status, cat)
            by_handle.setdefault(handle, []).append((level, status, cat))
        return by_item, by_handle

    def scan_records(self):
        """Yield (item_id, level, status, category, status_addr) for every occupied record.
        ``status_addr`` is the live address of the record's status byte (REC_STATUS) so a
        gate can write it back. Returns [] if the map can't be read."""
        rs = self._research_system()
        if not rs:
            return []
        try:
            cap = struct.unpack("<q", self.scanner.read_bytes(rs + ITEMS_MAP_OFF + 0x10, 8))[0]
            bk = struct.unpack("<Q", self.scanner.read_bytes(rs + ITEMS_MAP_OFF + 0x18, 8))[0]
            if cap <= 0 or cap > (1 << 20) or not (0x10000 < bk < (1 << 47)):
                return []
            bm = ((cap >> 3) + 7) & ~7
            base = bk + bm  # first record
            bitmap = self.scanner.read_bytes(bk, bm)
            recs = self.scanner.read_bytes(base, cap * REC_STRIDE)
        except Exception:
            return []
        out = []
        for i in range(cap):
            if not ((bitmap[i >> 3] >> (i & 7)) & 1):
                continue
            r = recs[i * REC_STRIDE:(i + 1) * REC_STRIDE]
            item = struct.unpack_from("<I", r, REC_ITEMID)[0]
            level = struct.unpack_from("<I", r, REC_LEVEL)[0]
            cat, status = r[REC_CATEGORY], r[REC_STATUS]
            out.append((item, level, status, cat, base + i * REC_STRIDE + REC_STATUS))
        return out

    def _welfare_item(self, species_key: str) -> Optional[int]:
        i0 = self.items.get(species_key)
        if i0 is None and species_key not in self._warned_unmapped:
            self._warned_unmapped.add(species_key)
            logger.info("research: no welfare item id for %r yet - add it to "
                        "SPECIES_WELFARE_ITEM (capture via tools/capture_species.py)", species_key)
        return i0

    def current_handle(self, species_key: str, snap: Optional[Tuple[dict, dict]] = None) -> Optional[int]:
        """Resolve the species' CURRENT-session handle (volatile) from its stable item id."""
        i0 = self._welfare_item(species_key)
        if i0 is None:
            return None
        snap = snap or self._snapshot()
        if snap is None:
            return None
        rec = snap[0].get(i0)
        return rec[0] if rec else None

    def is_welfare_complete(self, species_key: str, snap: Optional[Tuple[dict, dict]] = None) -> bool:
        """True iff every STANDARD (level<ADVANCED_LEVEL) animal-research record for this
        species is status 4. False if unmapped, unreadable, or incomplete."""
        i0 = self._welfare_item(species_key)
        if i0 is None:
            return False
        snap = snap or self._snapshot()
        if snap is None:
            return False
        by_item, by_handle = snap
        rec = by_item.get(i0)
        if rec is None:
            return False
        handle = rec[0]
        std = [st for (lvl, st, cat) in by_handle.get(handle, ())
               if cat == ANIMAL_CATEGORY and lvl < ADVANCED_LEVEL]
        return bool(std) and all(st == STATUS_COMPLETE for st in std)

    def is_research_complete(self, research_key: str, snap: Optional[Tuple[dict, dict]] = None) -> bool:
        """Generic dispatch for any data.json research_key. `welfare_<species>` uses the
        leveled animal-research rule; a key in RESEARCH_ITEM (e.g. a mechanic research) is
        complete when its single item's status == 4. Unknown keys -> False."""
        item = self.research_items.get(research_key)
        if item is not None:
            snap = snap or self._snapshot()
            if snap is None:
                return False
            rec = snap[0].get(item)  # (handle, level, status, category)
            return rec is not None and rec[2] == STATUS_COMPLETE
        if research_key.startswith("welfare_"):
            return self.is_welfare_complete(research_key[len("welfare_"):], snap)
        if research_key not in self._warned_unmapped:
            self._warned_unmapped.add(research_key)
            logger.info("research: key %r not mapped (add to SPECIES_WELFARE_ITEM or RESEARCH_ITEM)", research_key)
        return False


# Research facilities gate research by CATEGORY (placement isn't hookable and capacity getters
# crash - see the progression-unlock notes). The two research-enabling facilities map cleanly
# onto the two research categories present in the items map:
#   research_centre -> ANIMAL_CATEGORY (7)  : welfare_<species> locations
#   workshop        -> MECHANIC_CATEGORY (3): mechanic research (drink_shops, barriers)
MECHANIC_CATEGORY = 3
FACILITY_RESEARCH_CATEGORY: Dict[str, int] = {
    "research_centre": ANIMAL_CATEGORY,
    "workshop": MECHANIC_CATEGORY,
}
STATUS_NOTSTARTED = 0
STATUS_RESEARCHABLE = 1
STATUS_RESEARCHING = 2
# Record layout decoded from the research tick fn @0x140E48E60 (`progress += delta` at [rec+0x20],
# `comiss` vs per-level threshold, then level++ [rec+0x24] + status [rec+0x49]). Kept for reference;
# the gate does NOT touch progress (research completes via multiple inlined progress paths - only
# the STATUS writes are true chokepoints). Zeroing capacity getters CRASHED (div-by-zero).
REC_PROGRESS = 0x20        # f32 accumulated research progress
REC_PROGRESS_BAR = 0x1C    # f32 smoothed/displayed bar value
REC_LEVEL_FIELD = 0x24     # u32 current research level

# The research-START status writer: `mov byte [r15+0x49], 2` @ 0x140E461C6 (Researchable->
# Researching). Hooking it and skipping the write for gated categories = research never starts.
RESEARCH_START_RVA = 0xE461C6
RESEARCH_START_ORIG = bytes.fromhex("41c6474902")
RESEARCH_START_CAT_MODRM = 0x4F   # movzx ecx, byte [r15+0x3C] (record in r15 at this site)


class ResearchGate:
    """Memory-enforced research gate for the research-enabling facilities (research_centre,
    workshop). Installs a code hook on the research-START writer (0x140E461C6): for a gated
    category ([r15+0x3C] in the client-written scratch set) the status->2 write is SKIPPED, so
    that research NEVER enters "Researching" - no progress bar, no level tick, no completion, no
    reward, no AP check - and it auto-starts once the facility item arrives. (Validated live
    2026-06-02; the cleanest player-facing gate. Progress-zeroing was abandoned: research
    completes via multiple inlined progress paths, so only the status chokepoints are reliable.)

    ``reconcile(received_facilities)`` is authoritative + idempotent - driven from the full
    received-facility set each tick, so it's restart-correct. On a (re)gate it also resets any
    in-progress (status 2) gated research back to Researchable so the currently-active research
    stops (the hook then blocks it re-starting). Safe: software detour; ``shutdown()`` restores
    the site; degrades to a no-op if the game/site aren't available."""

    def __init__(self, reader: Optional["ResearchReader"] = None, scanner=None):
        if reader is None and scanner is None:
            raise ValueError("ResearchGate needs a ResearchReader or a scanner")
        self.reader = reader or ResearchReader(scanner)
        self.scanner = self.reader.scanner
        self.hm = HookManager(self.scanner)
        self.installed = False
        self.scratch: Optional[int] = None
        self.unlocked: set = set()
        # research facilities that GATE research in this seed (have an AP item). Default: both;
        # the client narrows this to the facilities actually present as items (set_gated).
        self.gated_facilities: set = set(FACILITY_RESEARCH_CATEGORY)
        self._last_gated: Optional[set] = None
        self._warned_pending = False

    def set_gated(self, facility_keys) -> None:
        """Declare which research facilities gate research in this seed (those with an AP item).
        Only their categories are ever blocked; a facility with no item never gates."""
        self.gated_facilities = set(facility_keys) & set(FACILITY_RESEARCH_CATEGORY)
        self._last_gated = None  # force a resync on next reconcile

    def gated_categories(self, unlocked_facilities) -> set:
        """Research categories whose enabling facility gates this seed but is NOT yet received."""
        unlocked = set(unlocked_facilities)
        return {cat for fac, cat in FACILITY_RESEARCH_CATEGORY.items()
                if fac in self.gated_facilities and fac not in unlocked}

    # -- lifecycle -------------------------------------------------------------

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        base = getattr(self.scanner, "module_base", None)
        if not base:
            return False
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "research_start")
        if resolved is None:
            logger.warning("research gate: start site unresolved (RVA stale + AOB miss - game patched?); not installing")
            return False
        site, orig = resolved
        try:
            ok = self.hm.install(
                "research_start", site, orig,
                lambda r, sc, res: make_research_gate(r, sc, res, orig,
                                                      cat_modrm=RESEARCH_START_CAT_MODRM))
        except Exception as e:
            logger.warning("research gate: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("research_start")
            logger.info("research start-gate installed @0x%X", site)
        return self.installed

    def shutdown(self) -> None:
        try:
            self.hm.restore("research_start")
        except Exception:
            pass
        self.installed = False

    # -- gated-set sync --------------------------------------------------------

    def _write_gated(self, cats) -> None:
        """Write the gated category set into the hook scratch (cats first, count last)."""
        if self.scratch is None:
            return
        cats = sorted(cats)
        try:
            for i, c in enumerate(cats):
                self.scanner.write_bytes(self.scratch + RESEARCH_GATE_CATS + i, bytes([c & 0xFF]))
            self.scanner.write_bytes(self.scratch + RESEARCH_GATE_COUNT, struct.pack("<I", len(cats)))
            logger.info("research gate: gating categories %s", cats or "none")
        except Exception as e:
            logger.warning("research gate: failed to write gated set: %s", e)

    def _stop_in_progress(self, cats) -> int:
        """Reset gated-category Researching(2) records to Researchable(1) so the currently-active
        research stops (the start hook then blocks it re-starting). Returns count reset."""
        if not cats:
            return 0
        n = 0
        for item, level, status, cat, status_addr in self.reader.scan_records():
            if cat in cats and status == STATUS_RESEARCHING:
                try:
                    self.scanner.write_bytes(status_addr, bytes([STATUS_RESEARCHABLE]))
                    n += 1
                except Exception:
                    pass
        return n

    def reconcile(self, unlocked_facilities) -> bool:
        """Authoritative gate sync: block research-start for categories whose facility item has
        not been received. Idempotent; only re-syncs on change. Returns True if installed."""
        self.unlocked = set(unlocked_facilities)
        gated = self.gated_categories(self.unlocked)
        if not self.ensure_installed():
            if not self._warned_pending:
                self._warned_pending = True
                logger.info("research gate: start hook not installable yet - will retry")
            return False
        if gated != self._last_gated:
            self._write_gated(gated)
            self._stop_in_progress(gated)
            self._last_gated = set(gated)
        return True
