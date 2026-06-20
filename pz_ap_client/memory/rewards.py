"""RewardGranter - grant a decoupled research reward at runtime (v1.0 data layer).

Productionises the spike proven in tools/unlock_flip_test.py (2026-06-10, GO): each
research reward is a content record in the research-system's *unlockables map*
(rs+0x148); the engine "grants" it by setting the unlocked byte (record+0x12)=1, plus
small per-type bookkeeping. Flag-only flip was verified live to make the content appear
in the build menu - no event broadcast needed. The reward-decoupling architecture means
the AP item ``research_reward {content: "EN_Grazing_Ball"}`` is applied by flipping that
byte for the named content.

Name -> content id is via the global intern registry (module+0x298AE00): ids are dense
pool indices, so we enumerate the id space once (READ-ONLY; the game's own resolver is
the only thing that ever interns/writes) and build a {name: id} index. Names are interned
lowercased.

Layout + bookkeeping decoded from headless-Ghidra decompiles of FUN_140E3FBF0 /
FUN_1468CBF30 (archived in tools/_decomp/); see unlock_flip_test.py for the full notes.
Degrades to False (never a crash, never a false success) if the game/zoo/maps aren't
readable, so a not-yet-applied progression reward surfaces + retries rather than being
silently skipped.
"""

from __future__ import annotations

import logging
import struct
from typing import Dict, Iterator, Optional, Tuple

logger = logging.getLogger("PZClient")

REGISTRY_RVA = 0x298AE00
UNLOCK_MAP_OFF = 0x148
REC_STRIDE = 0x14
REC_TYPE = 0x04
REC_KEY = 0x08          # u32 content id (the map key)
REC_BOOKKEEP = 0x0C     # u32 per-type bookkeeping key
REC_UNLOCKED = 0x12     # u8 unlocked flag (the byte we flip)
COUNT_MAP_OFF = 0x210   # stride 0xC {+0 key, +4 f32 max-level, +8 i32 count}
LEVEL_MAP_OFF = 0x1E8   # stride 0x10 {+0 content id, +4 f32 level}
EDU_COUNTER_OFF = 0x52C  # plain i32 on the research system
HEAP_LO, HEAP_HI = 0x10000, (1 << 47)

# progressive_research_reward family -> unlock-map record type byte.
# (0 supplement, 2 breeding, 3 education, 1 enrichment incl. exhibit enrichment, 4 zoopedia.)
FAMILY_TYPE = {"supplement": 0, "breeding": 2, "education": 3, "exhibit_enrichment": 1}

# Barriers ("Progressive Barrier Level", 6 copies) are NOT rs+0x148 unlock-map content - they are
# HABITAT-BOUNDARY build content gated by MECHANIC research (rs+0xF8). LIVE-CONFIRMED 2026-06-20: a
# boundary becomes BUILDABLE at its research status >= 3, while the barrier1..6 research LOCATION fires
# only at status == 4. So the Progressive Barrier item makes grade<=N barriers buildable by status-
# writing their research item to 3 - WITHOUT falsely firing the location check. Driven by
# reconcile_barriers(N) from the received-level count each tick (client._reconcile_barriers), NOT
# grant_progressive (which is for rs+0x148 type-keyed families). Grade = c0habitatboundary.fdb
# RelativeResistance tier (0.02->1, 0.2->2, 0.333->3, 0.5->4, 1.0->5/6; Electric special->6). Map is
# the 6 RESEARCHABLE boundaries' research-item NAME -> grade; the 6 DEFAULT boundaries (Hedge/ChainLink/
# Corrugated/Glass/Wood_Logs/Brick_Red) have NO research item and need a c0habitatboundary.fdb tag edit
# to become gateable - NOT handled here yet (grades 1 & 3 are defaults-only).
BARRIER_RESEARCH_GRADE = {
    "barriersonewayglass": 2,                                # Glass_One_Way (+ default ChainLink, Corrugated)
    "barrierschainsteelposts": 4, "barriersthickglass": 4,   # Steel_Mesh, Thick_Glass
    "barriersrebarstonecages": 5,                            # Gabion (+ default Brick_Red)
    "barriersconcrete": 6, "barrierselectric": 6,            # Concrete, Electric
    # DEFAULT-only grades (1 & 3 have no researchable barrier): the 6 always-shown defaults get gated by
    # re-pointing their c0habitatboundary.fdb ResearchPack at an EXISTING loaded research item, so they
    # become buildable when the client status-writes that item. g2/g5 defaults share the researchable
    # items above (ChainLink/Corrugated -> Glass_One_Way 10132, Brick_Red -> Gabion 10131); g1/g3 reuse
    # NoneResearchable placeholder items (never rendered in the research UI -> no Collect/soft-couple):
    "scenarioblueprint01": 1,                                # Hedge (g1)  -> ResearchPack 50002
    "scenarioblueprint02": 3,                                # Glass, Wood_Logs (g3) -> ResearchPack 50003
}
BARRIER_BUILDABLE_STATUS = 3   # research status that makes a boundary buildable (location fires at 4)

# Facilities (Research Centre, Workshop) are gated by the SAME fdb-hide (c0modularscenery rooms RS_Room_*/
# WS_Room_4x4 + c0blueprints prebuilt blueprints), but the scenery/facility browser reveals at status 4 (NOT
# 3 like the boundary editor - live-confirmed 2026-06-20). They reuse EXISTING NoneResearchable placeholder
# items (creating NEW c0research items CRASHES on load - the placeholders' un-interned names were the crash);
# the client status-writes the placeholder to 4 on the facility_unlock AP item. The placeholders are NOT AP
# locations, so status-4 fires NO check. This REPLACES the PresenceGate for these two facilities.
FACILITY_GATE_RESEARCH = {
    "research_centre": "guestspawner",   # placeholder item 50000; gates RS_Room_4x4 + Research Centre blueprints
    "workshop": "parkgate",              # placeholder item 50001; gates WS_Room_4x4 + Workshop blueprints
}
FACILITY_BUILDABLE_STATUS = 4
PROGRESSIVE_ORDERED: Dict[str, list] = {}   # rs+0x148 ordered families (barriers use rs+0xF8 status instead)


def _norm(s: str) -> str:
    """lowercase, alnum-only - matches research._norm_token (the _mechanic_item_map key form)."""
    return "".join(c for c in s.lower() if c.isalnum())


class InternRegistry:
    """Read-only view of the global name<->id intern registry (DAT_14298AE00)."""

    MAX_IDS = 1 << 20
    SCAN_CHUNK = 4096

    def __init__(self, scanner):
        self.s = scanner
        base = scanner.module_base + REGISTRY_RVA
        self.base = base
        if not self._plausible():
            self.base = scanner.read_qword(base) or 0
            if not self._plausible():
                raise RuntimeError("intern registry not readable at module+0x298AE00 (or deref)")
        self.stride = struct.unpack("<q", self.s.read_bytes(self.base + 0x10, 8))[0]
        self.pool_top = struct.unpack("<Q", self.s.read_bytes(self.base + 0x30, 8))[0]
        self._index: Optional[dict] = None

    def _plausible(self) -> bool:
        try:
            stride = struct.unpack("<q", self.s.read_bytes(self.base + 0x10, 8))[0]
            top = struct.unpack("<Q", self.s.read_bytes(self.base + 0x30, 8))[0]
            count = struct.unpack("<I", self.s.read_bytes(self.base + 0x9C, 4))[0]
        except Exception:
            return False
        return 0 < stride <= 0x100 and HEAP_LO < top < HEAP_HI and 0 < count <= (1 << 24)

    def _name(self, cid: int) -> Optional[str]:
        try:
            slot = struct.unpack("<Q", self.s.read_bytes(self.pool_top - cid * self.stride, 8))[0]
        except Exception:
            return None
        rec = slot & ~1
        if not (slot and slot == rec and HEAP_LO < rec < HEAP_HI):
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

    def _scan_chunk(self, base: int, index: dict) -> bool:
        """Add one SCAN_CHUNK of pool slots' {lowercased name: id} into ``index`` (first id wins).
        Returns True to keep enumerating, False to stop: the slot region is unreadable (end of pool)
        or - past the first chunk - held no valid slot."""
        try:
            blob = self.s.read_bytes(self.pool_top - (base + self.SCAN_CHUNK) * self.stride,
                                     self.SCAN_CHUNK * self.stride)
        except Exception:
            return False
        any_valid = False
        for i in range(self.SCAN_CHUNK):
            slot = struct.unpack_from("<Q", blob, (self.SCAN_CHUNK - (i + 1)) * self.stride)[0]
            rec = slot & ~1
            if slot and slot == rec and HEAP_LO < rec < HEAP_HI:
                any_valid = True
                nm = self._name(base + i + 1)
                if nm:
                    index.setdefault(nm.lower(), base + i + 1)
        return any_valid or base == 0

    def build_index(self) -> dict:
        """Enumerate the id space -> {lowercased name: id}; cached. Read-only."""
        if self._index is not None:
            return self._index
        index: dict = {}
        for base in range(0, self.MAX_IDS, self.SCAN_CHUNK):
            if not self._scan_chunk(base, index):
                break
        self._index = index
        return index

    def lookup(self, name: str) -> Optional[int]:
        return self.build_index().get(name.lower())


class UnlockMap:
    """The unlockables map at research_system+0x148 (content id -> unlock record)."""

    def __init__(self, scanner, rs: int):
        self.s = scanner
        self.count = struct.unpack("<q", scanner.read_bytes(rs + UNLOCK_MAP_OFF + 0x08, 8))[0]
        self.cap = struct.unpack("<q", scanner.read_bytes(rs + UNLOCK_MAP_OFF + 0x10, 8))[0]
        self.buckets = struct.unpack("<Q", scanner.read_bytes(rs + UNLOCK_MAP_OFF + 0x18, 8))[0]
        if not (0 < self.count <= self.cap and self.cap and (self.cap & (self.cap - 1)) == 0
                and HEAP_LO < self.buckets < HEAP_HI):
            raise RuntimeError(f"rs+0x148 not a populated hashmap (count={self.count} cap={self.cap})")
        self.bitmap = scanner.read_bytes(self.buckets, ((self.cap >> 3) + 7) & ~7)
        self.records = self.buckets + len(self.bitmap)

    def _occupied(self) -> Iterator[int]:
        for i in range(self.cap):
            if (self.bitmap[i >> 3] >> (i & 7)) & 1:
                yield i

    def iter_records(self) -> Iterator[Tuple[int, int, int, int]]:
        """(record_addr, content_id, type, unlocked) for every live slot."""
        try:
            blob = self.s.read_bytes(self.records, self.cap * REC_STRIDE)
        except Exception:
            return
        for i in self._occupied():
            off = i * REC_STRIDE
            rec = self.records + off
            cid = struct.unpack_from("<I", blob, off + REC_KEY)[0]
            yield rec, cid, blob[off + REC_TYPE], blob[off + REC_UNLOCKED]

    def find(self, cid: int) -> Optional[Tuple[int, int, int]]:
        for rec, rcid, typ, flag in self.iter_records():
            if rcid == cid:
                return rec, typ, flag
        return None


class RewardGranter:
    """Grants research-data-layer rewards by flipping the content's unlocked byte."""

    def __init__(self, scanner, research):
        self.scanner = scanner
        self.research = research          # ResearchReader (resolves the research system)
        self._registry: Optional[InternRegistry] = None

    def _registry_index(self) -> Optional[InternRegistry]:
        if self._registry is None:
            try:
                self._registry = InternRegistry(self.scanner)
            except Exception as e:
                logger.warning("reward: intern registry unavailable (%s)", e)
                return None
        return self._registry

    def _unlock_map(self) -> Optional[UnlockMap]:
        rs = self.research._research_system()
        if not rs:
            return None
        try:
            return UnlockMap(self.scanner, rs)
        except Exception as e:
            logger.warning("reward: unlockables map unreadable (%s)", e)
            return None

    def _flip(self, rs: int, rec: int, cid: int, typ: int, flag: int) -> bool:
        if flag:
            return True  # already unlocked - idempotent success
        try:
            self.scanner.write_bytes(rec + REC_UNLOCKED, b"\x01")
        except Exception as e:
            logger.warning("reward: failed to write unlocked byte (%s)", e)
            return False
        self._bookkeep(rs, rec, cid, typ)
        return True

    def _bookkeep(self, rs: int, rec: int, cid: int, typ: int) -> None:
        """Mirror FUN_140E3FBF0's per-type side-effects for a grant. Types 1 (enrichment) and 4
        (zoopedia) need none for the build-menu appearance (verified). Best-effort: only mutates
        EXISTING bookkeeping records (the game's find-or-insert isn't replicated)."""
        try:
            if typ == 3:  # education: rs+0x52C counter++
                addr = rs + EDU_COUNTER_OFF
                cur = struct.unpack("<i", self.scanner.read_bytes(addr, 4))[0]
                self.scanner.write_bytes(addr, struct.pack("<i", cur + 1))
            elif typ in (0, 2):  # supplement / breeding: count map at rs+0x210 keyed by rec+0xC
                bk = struct.unpack("<I", self.scanner.read_bytes(rec + REC_BOOKKEEP, 4))[0]
                crec = self._intmap_find(rs + COUNT_MAP_OFF, 0xC, bk)
                if crec is None:
                    return  # game would insert one; UI counters may lag (non-fatal)
                if typ == 0:
                    cur = struct.unpack("<i", self.scanner.read_bytes(crec + 8, 4))[0]
                    self.scanner.write_bytes(crec + 8, struct.pack("<i", cur + 1))
                else:
                    self._bookkeep_breeding(rs, cid, crec)
        except Exception as e:
            logger.debug("reward: bookkeeping (type %d) skipped: %s", typ, e)

    def _intmap_find(self, base: int, stride: int, key: int) -> Optional[int]:
        """Find a record by u32 key in the engine int-map family {+8 count, +0x10 cap, +0x18 buckets}."""
        count = struct.unpack("<q", self.scanner.read_bytes(base + 0x08, 8))[0]
        cap = struct.unpack("<q", self.scanner.read_bytes(base + 0x10, 8))[0]
        buckets = struct.unpack("<Q", self.scanner.read_bytes(base + 0x18, 8))[0]
        if not (0 <= count <= cap and cap and (cap & (cap - 1)) == 0 and HEAP_LO < buckets < HEAP_HI):
            return None
        bitmap = self.scanner.read_bytes(buckets, ((cap >> 3) + 7) & ~7)
        records = buckets + len(bitmap)
        blob = self.scanner.read_bytes(records, cap * stride)
        for i in range(cap):
            if (bitmap[i >> 3] >> (i & 7)) & 1:
                if struct.unpack_from("<I", blob, i * stride)[0] == key:
                    return records + i * stride
        return None

    def _bookkeep_breeding(self, rs: int, cid: int, crec: int) -> None:
        lrec = self._intmap_find(rs + LEVEL_MAP_OFF, 0x10, cid)
        if lrec is None:
            return
        level = struct.unpack("<f", self.scanner.read_bytes(lrec + 4, 4))[0]
        cur = struct.unpack("<f", self.scanner.read_bytes(crec + 4, 4))[0]
        if level > cur:
            self.scanner.write_bytes(crec + 4, struct.pack("<f", level))

    # -- public API ------------------------------------------------------------

    def grant(self, content: str) -> bool:
        """Grant the named research-reward content. True on success (or already unlocked);
        False if unresolvable/unreadable (caller retries)."""
        reg = self._registry_index()
        if reg is None:
            return False
        cid = reg.lookup(content)
        if cid is None:
            logger.warning("reward: content %r not in intern registry (not a real content token?)", content)
            return False
        m = self._unlock_map()
        if m is None:
            return False
        rs = self.research._research_system()
        hit = m.find(cid)
        if hit is None:
            logger.warning("reward: content %r (id 0x%X) not in the unlockables map "
                           "(not research-reward-gated)", content, cid)
            return False
        rec, typ, flag = hit
        ok = self._flip(rs, rec, cid, typ, flag)
        if ok:
            logger.info("[apply] research_reward %s granted (type %d)", content, typ)
        return ok

    def _grant_next_locked(self, ordered_contents: list, family: str) -> bool:
        """Flip the FIRST still-locked content in an explicit order. For progressive families whose
        contents aren't a single unlock-map record type (barriers): each received level unlocks the
        next grade. True if one flipped or all already unlocked; False if maps unreadable (retry)."""
        reg = self._registry_index()
        if reg is None:
            return False
        m = self._unlock_map()
        if m is None:
            return False
        rs = self.research._research_system()
        for content in ordered_contents:
            cid = reg.lookup(content)
            if cid is None:
                continue
            hit = m.find(cid)
            if hit is None:
                continue
            rec, typ, flag = hit
            if not flag:  # locked -> this is the next grade to unlock
                ok = self._flip(rs, rec, cid, typ, 0)
                if ok:
                    logger.info("[apply] progressive_research_reward %s: unlocked %s (id 0x%X)",
                                family, content, cid)
                return ok
        logger.info("progressive reward (%s): all grades unlocked - acknowledging", family)
        return True

    def grant_progressive(self, family: str) -> bool:
        """Grant the next still-locked reward of a progressive family. Explicitly-ordered families
        (barrier) unlock the next content in grade order; type-keyed families (supplement/breeding/
        education/exhibit_enrichment) unlock the lowest content-id of the family's record type. True
        if one was flipped (or none left to grant); False if the maps aren't readable (retry)."""
        if family in PROGRESSIVE_ORDERED:
            return self._grant_next_locked(PROGRESSIVE_ORDERED[family], family)
        typ = FAMILY_TYPE.get(family)
        if typ is None:
            logger.warning("progressive reward: unknown family %r", family)
            return False
        m = self._unlock_map()
        if m is None:
            return False
        rs = self.research._research_system()
        locked = sorted((cid, rec, t) for rec, cid, t, flag in m.iter_records()
                        if t == typ and not flag)
        if not locked:
            logger.info("progressive reward (%s): nothing left to grant - acknowledging", family)
            return True
        cid, rec, t = locked[0]
        ok = self._flip(rs, rec, cid, t, 0)
        if ok:
            logger.info("[apply] progressive_research_reward %s: granted content id 0x%X", family, cid)
        return ok

    def reconcile_barriers(self, levels: int) -> bool:
        """Make every RESEARCHABLE barrier of grade <= `levels` buildable, by status-writing its mechanic
        research item (rs+0xF8) to BARRIER_BUILDABLE_STATUS (3). Buildability triggers at status >= 3 while
        the barrier_N LOCATION fires only at status == 4 - so this unlocks BUILDING without firing the check
        (live-confirmed 2026-06-20). Idempotent + restart-correct: the client drives this each tick from the
        received Progressive-Barrier-Level count (so N grades are buildable after N levels). Returns True if
        applied / nothing-to-do, False if the items map isn't readable yet (retry next tick).
        NB the 6 DEFAULT barriers have no research item (always-shown) and are NOT handled here - they need
        the c0habitatboundary.fdb research-tag edit first; grades 1 & 3 are defaults-only."""
        try:
            name_to_id = self.research._mechanic_item_map()   # {_norm(name) -> research-item id}, cached
        except Exception as e:
            logger.warning("barrier reconcile: mechanic item map unreadable (%s)", e)
            return False
        if not name_to_id:
            return False  # registry/items-map not ready - retry
        want = {}  # research-item id -> name, for grades <= levels
        for name, grade in BARRIER_RESEARCH_GRADE.items():
            if grade <= levels:
                iid = name_to_id.get(_norm(name))
                if iid is not None:
                    want[iid] = name
        if not want:
            return True
        try:
            for item_id, _lvl, status, _cat, status_addr in self.research.scan_records():
                if item_id in want and status < BARRIER_BUILDABLE_STATUS:
                    self.scanner.write_bytes(status_addr, bytes([BARRIER_BUILDABLE_STATUS]))
                    logger.info("[apply] progressive barrier (<= grade %d): %s buildable (item 0x%X, %d->%d)",
                                levels, want[item_id], item_id, status, BARRIER_BUILDABLE_STATUS)
        except Exception as e:
            logger.warning("barrier reconcile: scan/write failed (%s)", e)
            return False
        return True

    def reconcile_facilities(self, facility_keys) -> bool:
        """Reveal the Research Centre / Workshop build items for received facility_unlock keys, by status-
        writing their NoneResearchable placeholder research item (GuestSpawner/ParkGate) to
        FACILITY_BUILDABLE_STATUS (4 - the scenery/facility browser reveals at 4, not 3). The placeholders
        are NOT AP locations, so this fires no check. Idempotent + restart-correct: the client drives it each
        tick from the received facility_unlock set. Returns True if applied/nothing-to-do, False if the items
        map isn't readable yet (retry)."""
        try:
            name_to_id = self.research._mechanic_item_map()
        except Exception as e:
            logger.warning("facility reconcile: mechanic item map unreadable (%s)", e)
            return False
        if not name_to_id:
            return False
        want = {}  # placeholder item id -> facility key
        for key in facility_keys:
            name = FACILITY_GATE_RESEARCH.get(key)
            if name is None:
                continue
            iid = name_to_id.get(_norm(name))
            if iid is not None:
                want[iid] = key
        if not want:
            return True
        try:
            for item_id, _lvl, status, _cat, status_addr in self.research.scan_records():
                if item_id in want and status < FACILITY_BUILDABLE_STATUS:
                    self.scanner.write_bytes(status_addr, bytes([FACILITY_BUILDABLE_STATUS]))
                    logger.info("[apply] facility_unlock %s: revealed (placeholder item 0x%X, %d->%d)",
                                want[item_id], item_id, status, FACILITY_BUILDABLE_STATUS)
        except Exception as e:
            logger.warning("facility reconcile: scan/write failed (%s)", e)
            return False
        return True
