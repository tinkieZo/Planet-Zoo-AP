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
import re
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
# The ZOO EDUCATION RATING = i32 counter@rs+0x52C / i32 total@rs+0x528, clamped (zoo-stats decomp
# fn_at_14049F49F, RE'd 2026-07-10). The counter = number of unlocked education contents zoo-wide;
# the game only increments it at research-completion transitions (fn_140E3FBF0 education case), so
# the gate keeps it equal to the number of SET type-3 bytes (_sync_education_counter).
EDU_COUNTER_OFF = 0x52C

# The PER-SPECIES education unlock-level STORE - what the education panel's per-species caps (and
# any other GetEducationUnlockLevel consumer) actually read. RE'd 2026-07-10 from the binding's
# executor chain (registrar block 0x140499xxx -> handler thunk 0x14049D930 -> FUN_146018830 ->
# reader FUN_1482f7500): STORE = *(*(park+0x10)+0x660); open-addressing map @store+0x88
# {u32 topic_id -> 0x60-stride record, u32 unlock level @rec+8, u32 max level @rec+0x18} with the
# engine's standard u32 hash + bitmap layout (find helper FUN_14836f0f0). Live-verified: 210
# records (the education-capable species), gpfrog topic 0x68 at level 2 the only nonzero; the
# record level is plain-writable. The gate writes level = min(received, 3) to EVERY record each
# education reconcile - the same authoritative barrier model as the byte gate; the game recomputes
# from research at load/completion and the next tick reasserts (accepted ~1s flicker class).
EDU_STORE_PARENT_OFF = 0x10    # park -> parent object
EDU_STORE_OFF = 0x660          # parent -> store object
EDU_STORE_MAP_OFF = 0x88       # store -> map header {count@+8, cap@+0x10 pow2, buckets@+0x18}
EDU_REC_STRIDE = 0x60
EDU_REC_LEVEL = 0x8
EDU_MAX_LEVEL = 3
HEAP_LO, HEAP_HI = 0x10000, (1 << 47)

# progressive_research_reward family -> unlock-map record type byte.
# (0 supplement, 2 breeding, 3 education, 1 enrichment incl. exhibit enrichment, 4 zoopedia.)
FAMILY_TYPE = {"supplement": 0, "breeding": 2, "education": 3, "exhibit_enrichment": 1}

# COUNT-BASED per-species LEVEL families: each is content named <Species><Family>L<k> (intern name,
# lowercased, '...<family>l<k>'; capture group = level k). The progressive item's quantity = the tier
# count, and the N-th received copy unlocks level <= N for EVERY species that has it (the barrier model:
# count-driven + authoritative, NOT grant_progressive's one-shot "lowest-locked of the type"). Pools:
#   exhibit_enrichment -> the 23 exhibit animals' enrichment levels (1..3). (Habitat enrichment is the
#     separate EN_* per-content pool gated by reconcile_rewards - the 'enrichmentl<k>' name never matches
#     EN_*, so the two don't collide.)
#   supplement / breeding / education -> all animals' welfare-research levels (1..2 / 1..3 / 1..3).
FAMILY_LEVEL_RE = {
    "exhibit_enrichment": re.compile(r"enrichmentl(\d+)"),
    "supplement": re.compile(r"supplementl(\d+)"),
    "breeding": re.compile(r"breedingl(\d+)"),
    "education": re.compile(r"educationl(\d+)"),
}
LEVEL_FAMILIES = frozenset(FAMILY_LEVEL_RE)

# Families whose EFFECT is a research-driven value in the count-map @rs+0x210 (ONE record per
# species, keyed by the records' rec+0xC bookkeep id): breeding = fertility-rate f32 @crec+4,
# supplement = food-quality TIER count i32 @crec+8 (floor 1 = the default quality-1 food every
# species ships with; research L1/L2 raise it to 2/3 - so the AP gate is min(1+received, granted),
# NEVER 0: capping to 0 emptied the food-quality dropdown, live 2026-07-10). For these the
# unlocked BYTE is COSMETIC (live 2026-07-08/10):
# the engine applies the effect at research completion and RECOMPUTES it from research state on
# every park load (persistence test 2026-07-10, golden poison frog: written 0.30->0.15 reverted on
# save/reload; the UI read 15% live before the save). So the gate is a PER-TICK CAP: effect =
# level min(received progressive count, research-granted level), re-applied within a tick of any
# load / research completion. Breeding rates are FLAT across species (buffalo + golden poison frog
# live-confirmed L1=15% L2=30%); a value above L2's rate is treated as level 3 and only ever capped
# DOWN - restoring level 3 = restoring the observed granted value, so L3's exact rate is never
# needed. Education is NOT here: its per-species store is still unlocated (rs+0x52C is global).
EFFECT_CAP_FAMILIES = frozenset(("breeding", "supplement"))
BREEDING_RATE = {0: 0.0, 1: 0.15, 2: 0.30}
_RATE_EPS = 1e-4

# Barriers ("Progressive Barrier Level", 6 copies) are NOT rs+0x148 unlock-map content - they are
# HABITAT-BOUNDARY build content gated by MECHANIC research (rs+0xF8). LIVE-CONFIRMED 2026-06-20: a
# boundary becomes BUILDABLE at its research status >= 3, while the barrier1..6 research LOCATION fires
# only at status == 4. So the Progressive Barrier item makes grade<=N barriers buildable by status-
# writing their research item to 3 - WITHOUT falsely firing the location check. Driven by
# reconcile_barriers(N) from the received-level count each tick (client._reconcile_barriers), NOT
# grant_progressive (which is for rs+0x148 type-keyed families). A Progressive Barrier Level makes all
# barriers of grade <= N buildable. FULL DECOUPLE (2026-06-21): every barrier's c0habitatboundary.fdb
# ResearchPack is re-pointed onto a NON-location NoneResearchable GATE, and the client status-writes the gate
# to 4 (buildable). The 6 real barrier research items (10126/10132/13002/10131/13001/13003) are no longer
# boundary gates -> the client never touches them -> they stay as the barrier_N AP locations the player
# researches (build and check fully separate, no false fire). Grade = c0habitatboundary RelativeResistance
# tier. Gates: grades 1/3 reuse placeholders ScenarioBlueprint01/02 (50002/50003); grades 2/4/5/6 use minted
# gates ApBarrierGate2/4/5/6 (50004-50007 - the proven new-item recipe: c0research row + GameMain
# default_off_noneresearchable topology entry). The fdb re-point + gate minting are applied at /pz_install.
BARRIER_GRADE_GATE = {
    1: "scenarioblueprint01",   # 50002 -> Hedge
    2: "apbarriergate2",        # 50004 -> Glass_One_Way + ChainLink + Corrugated
    3: "scenarioblueprint02",   # 50003 -> Glass + Wood_Logs
    4: "apbarriergate4",        # 50005 -> Steel_Mesh + Thick_Glass
    5: "apbarriergate5",        # 50006 -> Gabion + Brick_Red
    6: "apbarriergate6",        # 50007 -> Concrete + Electric
}
BARRIER_MAX_GRADE = 6
BARRIER_BUILDABLE_STATUS = 4   # a boundary is buildable when its GATE research item is status 4 (live-confirmed).
                               # Gates are NoneResearchable (never rendered, NOT AP locations) -> status-4 fires
                               # NO check. That's the build/check decouple.

# Facilities (Research Centre, Workshop) are gated by the SAME fdb-hide (c0modularscenery rooms RS_Room_*/
# WS_Room_4x4 + c0blueprints prebuilt blueprints), revealed at status 4 (the boundary editor uses the
# SAME threshold - both 4, live-confirmed 2026-06-21). They reuse EXISTING NoneResearchable placeholder
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


# Mechanic-research CONTENT (shops/themes/blueprints/transport/staff facilities/power) unlocks like barriers:
# at /pz_install each content's research gate (c0modularscenery.ResearchItemID / c0trackedrides.ResearchPack /
# c0blueprints.ResearchItemIDs) is re-pointed onto a minted NoneResearchable gate named "ApGate<Content>"; the
# client status-writes that gate to 4. The real research item stays the player-researched location (no false
# check). Content is "mechanic" if its normalized name contains a keyword below; its gate resolves by name
# "apgate" + _norm(content). (ANIMAL research_reward content - EN_*/supplement/etc. - is NOT mechanic; it
# unlocks via the rs+0x148 flag flip in grant(), already decoupled.)
MECH_CONTENT_KEYWORDS = ("foodshops", "drinkshops", "souvenirshops", "themesets", "habitats", "shelters",
                         "stafffacilities", "transport", "power")


def is_mechanic_content(content: str) -> bool:
    """True for mechanic-research build content (gated via a re-pointed ApGate<Content>), False for animal
    rewards (rs+0x148)."""
    n = _norm(content)
    return any(k in n for k in MECH_CONTENT_KEYWORDS)


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
        self._level_maps: Dict[str, Dict[int, int]] = {}  # family -> {content id -> level k}, cached per family
        # Contents we've already warned aren't grantable here (bogus token / not research-gated in this
        # zoo, e.g. a non-researchable item that slipped into the pool). grant() acknowledges these as
        # no-ops - returning False forever would stall the item-apply queue at that item (everything
        # received after it would never apply) and re-log the warning every retry tick.
        self._not_gated_warned: set = set()
        self._last_unlock_count = -1     # unlock-map record count last tick (growth -> refresh snapshots)
        # Research-GRANTED effect high-water per (family, bookkeep id) - what the cap restores up to
        # when more progressive copies arrive. Bookkeep ids re-intern per park load, so this is
        # cleared with the other snapshots on unlock-map change (a stale id colliding with a fresh
        # load's could otherwise restore a value research never granted in that zoo).
        self._effect_granted: Dict[Tuple[str, int], float] = {}
        self._edu_store_hdr: Optional[int] = None   # cached education level-store map header

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

    def _maybe_refresh_names(self, m: UnlockMap) -> None:
        """Names intern LAZILY: a species' reward tokens (<Species>EnrichmentL<k>, ...) enter the
        intern registry only when its research tree first loads - which is AFTER our first-tick
        registry snapshot. The engine's grant find-or-inserts the content's record into the unlock
        map, so map GROWTH is the exact signal that content we may never have indexed just appeared.
        Rebuild the registry index + family level maps then, so the per-tick gate covers it (live
        bug 2026-07-06: freshly-researched exhibit animals' enrichment levels stayed unlocked - their
        tokens post-dated the cached snapshot, so the reconcile never matched their records)."""
        if m.count != self._last_unlock_count:
            if self._last_unlock_count >= 0:
                logger.info("reward: unlock map grew (%d -> %d records) - refreshing name snapshots",
                            self._last_unlock_count, m.count)
            self._last_unlock_count = m.count
            if self._registry is not None:
                self._registry._index = None
            self._level_maps.clear()
            self._effect_granted.clear()

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

    @staticmethod
    def _breeding_level_of(value: float) -> int:
        """Flat-rate level for an observed fertility f32 (above L2's rate = level 3, whose exact
        rate is never needed because level 3 is only ever capped DOWN or fully restored)."""
        if value > BREEDING_RATE[2] + _RATE_EPS:
            return 3
        if value >= BREEDING_RATE[2] - _RATE_EPS:
            return 2
        if value >= BREEDING_RATE[1] - _RATE_EPS:
            return 1
        return 0

    def _cap_effects(self, family: str, m: "UnlockMap", levels: Dict[int, int], received: int) -> int:
        """Cap each species' EFFECT value (count-map @rs+0x210) to the received progressive count -
        the real gate for breeding/supplement (the unlocked byte is cosmetic for them). Effect =
        level min(received, research-granted); the granted high-water is tracked per (family,
        bookkeep id) so later copies RESTORE it. The game rewrites the researched value on every
        park load and our next tick re-caps (~1s of full effect - the accepted gate flicker).
        Returns the number of effect writes."""
        if family not in EFFECT_CAP_FAMILIES:
            return 0
        rs = self.research._research_system()
        if not rs:
            return 0
        bks = set()
        for rec, cid, _typ, _flag in m.iter_records():
            if cid in levels:
                try:
                    bks.add(struct.unpack("<I", self.scanner.read_bytes(rec + REC_BOOKKEEP, 4))[0])
                except Exception:
                    continue
        bks.discard(0)
        writes = 0
        for bk in bks:
            try:
                crec = self._intmap_find(rs + COUNT_MAP_OFF, 0xC, bk)
                if crec is None:
                    continue    # record is inserted lazily on first grant - nothing granted, nothing to cap
                writes += self._cap_one_effect(family, bk, crec, received)
            except Exception as e:
                logger.debug("%s effect cap: bk=0x%X skipped (%s)", family, bk, e)
        return writes

    def _cap_one_effect(self, family: str, bk: int, crec: int, received: int) -> int:
        """Cap/restore one species' effect field; returns 1 if written."""
        if family == "breeding":
            cur = struct.unpack("<f", self.scanner.read_bytes(crec + 4, 4))[0]
            granted = max(self._effect_granted.get(("breeding", bk), 0.0), cur)
            self._effect_granted[("breeding", bk)] = granted
            glevel = self._breeding_level_of(granted)
            eff = min(received, glevel)
            desired = granted if eff == glevel else BREEDING_RATE[eff]
            if abs(cur - desired) <= _RATE_EPS:
                return 0
            self.scanner.write_bytes(crec + 4, struct.pack("<f", desired))
            logger.info("breeding effect cap: bk=0x%X %.2f -> %.2f (granted %.2f, received %d)",
                        bk, cur, desired, granted, received)
        else:   # supplement
            # The count @crec+8 is the number of AVAILABLE FOOD-QUALITY TIERS, floor 1: every species
            # ships with quality-1 food (vanilla-unresearched reads 1; live 2026-07-10 the pangolin's
            # food-quality dropdown went EMPTY when this capped to 0), research L1/L2 raise it to 2/3.
            # So the gate maps received copies to tiers ABOVE the default: desired = min(1+received,
            # granted), granted floored at 1 (also self-heals records an older build zeroed).
            cur = struct.unpack("<i", self.scanner.read_bytes(crec + 8, 4))[0]
            granted = int(max(self._effect_granted.get(("supplement", bk), 0), cur, 1))
            self._effect_granted[("supplement", bk)] = granted
            desired = min(1 + received, granted)
            if cur == desired:
                return 0
            self.scanner.write_bytes(crec + 8, struct.pack("<i", desired))
            logger.info("supplement effect cap: bk=0x%X %d -> %d (granted %d, received %d)",
                        bk, cur, desired, granted, received)
        return 1

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
        """Grant the named research-reward content. True on success (or already unlocked, or the content
        turns out not to be grantable here - acknowledged as a no-op so the item-apply queue advances);
        False only on a TRANSIENT failure (registry/map not readable yet - caller retries next tick).

        The distinction matters: a content that resolved against a loaded registry/map but isn't gated
        (e.g. a non-researchable item that slipped into the pool, like ParkGate) will NEVER become
        grantable - returning False stalled the whole apply queue at that item and re-warned every tick.
        Acknowledging is safe: the per-tick reconcile (_reconcile_rewards) re-syncs every gated animal
        content from the full received set, so a late-appearing record would still be unlocked there."""
        reg = self._registry_index()
        if reg is None:
            return False
        cid = reg.lookup(content)
        if cid is None:
            # names intern lazily - the token may have appeared after our snapshot; rebuild once
            reg._index = None
            cid = reg.lookup(content)
        if cid is None:
            if content not in self._not_gated_warned:
                self._not_gated_warned.add(content)
                logger.warning("reward: content %r not in intern registry (not a real content token?) - "
                               "acknowledging as a no-op so item application continues", content)
            return True
        m = self._unlock_map()
        if m is None:
            return False
        rs = self.research._research_system()
        hit = m.find(cid)
        if hit is None:
            if content not in self._not_gated_warned:
                self._not_gated_warned.add(content)
                logger.warning("reward: content %r (id 0x%X) not in the unlockables map (not "
                               "research-reward-gated in this zoo) - acknowledging as a no-op so item "
                               "application continues", content, cid)
            return True
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
        """Make every barrier of grade <= `levels` buildable by status-writing each grade's GATE (a
        NoneResearchable item from BARRIER_GRADE_GATE, NOT an AP location) to BARRIER_BUILDABLE_STATUS (4).
        The 6 real barrier research items stay untouched (they remain the barrier_N locations the player
        researches), so this NEVER fires a barrier check - build and check are decoupled (the c0habitatboundary
        re-point + gate minting are applied at /pz_install). Idempotent + restart-correct: driven each tick
        from the received Progressive-Barrier-Level count. Returns True if applied/nothing-to-do, False if the
        items map isn't readable yet (retry next tick)."""
        try:
            name_to_id = self.research._mechanic_item_map()   # {_norm(name) -> research-item id}, cached
        except Exception as e:
            logger.warning("barrier reconcile: mechanic item map unreadable (%s)", e)
            return False
        if not name_to_id:
            return False  # registry/items-map not ready - retry
        want = {}  # gate item id -> (grade, gate_name), for grades <= levels
        for grade in range(1, min(levels, BARRIER_MAX_GRADE) + 1):
            gate = BARRIER_GRADE_GATE.get(grade)
            iid = name_to_id.get(_norm(gate)) if gate else None
            if iid is not None:
                want[iid] = (grade, gate)
        if not want:
            return True
        try:
            for item_id, _lvl, status, _cat, status_addr in self.research.scan_records():
                if item_id in want and status < BARRIER_BUILDABLE_STATUS:
                    grade, gate = want[item_id]
                    self.scanner.write_bytes(status_addr, bytes([BARRIER_BUILDABLE_STATUS]))
                    logger.info("[apply] progressive barrier grade %d: gate %s buildable (item 0x%X, %d->%d)",
                                grade, gate, item_id, status, BARRIER_BUILDABLE_STATUS)
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

    def reconcile_rewards(self, received_contents, universe_contents) -> bool:
        """Authoritatively gate ANIMAL per-content research_reward unlocks: each gated content's
        unlocked byte (rs+0x148 record +0x12) = 1 if its AP item was received, else 0. This LOCKS
        content the scenario BASE BIN pre-unlocks (e.g. basic enrichment baked into Scenario_22_Empty)
        until its item arrives - grant() is ONE-WAY (only unlocks), so without this such content is
        free from the start. Mechanic content is excluded (gated at /pz_install via the fdb re-point).
        Idempotent + restart-correct (driven each tick from the full received set); only writes on
        change. Returns True if applied/nothing-to-do, False if the maps aren't readable yet (retry).

        Scope: the PER-CONTENT (named) universe only. The progressive 'exhibit_enrichment' family
        (grant_progressive) flips the lowest-locked type-1 content, which can overlap this named pool;
        if a progressive grant lands on a named content this re-locks it next tick (per-content wins)."""
        reg = self._registry_index()
        if reg is None:
            return False
        m = self._unlock_map()
        if m is None:
            return False
        self._maybe_refresh_names(m)
        gated = self._resolve_animal_cids(reg, universe_contents)
        if not gated:
            return True
        want_unlocked = set(self._resolve_animal_cids(reg, received_contents))
        try:
            locked, unlocked = self._apply_reward_gate(m, gated, want_unlocked)
        except Exception as e:
            logger.warning("reward gate: reconcile failed (%s)", e)
            return False
        if locked or unlocked:
            logger.info("reward gate: animal content re-synced (locked %d not-yet-received, unlocked %d received)",
                        locked, unlocked)
        return True

    def _resolve_animal_cids(self, reg, contents) -> Dict[int, str]:
        """{content id -> content} for ANIMAL (non-mechanic) contents resolvable in the intern registry.
        Mechanic content is gated at /pz_install (re-pointed gate), not via the rs+0x148 unlock byte."""
        out: Dict[int, str] = {}
        for content in contents:
            if is_mechanic_content(content):
                continue
            cid = reg.lookup(content)
            if cid is not None:
                out[cid] = content
        return out

    def _apply_reward_gate(self, m, gated, want_unlocked) -> Tuple[int, int]:
        """Set unlocked = (cid in want_unlocked) for every gated cid in the unlock map; only writes on
        change. Returns (locked, unlocked) write counts. Locking is a byte-only gate (no bookkeeping);
        unlocking goes through _flip so per-type bookkeeping (counts/levels) stays consistent."""
        rs = self.research._research_system()
        locked = unlocked = 0
        for rec, cid, typ, flag in m.iter_records():
            if cid not in gated:
                continue
            target = 1 if cid in want_unlocked else 0
            if flag == target:
                continue
            if target:
                self._flip(rs, rec, cid, typ, 0)                     # unlock (+ per-type bookkeeping)
                unlocked += 1
            else:
                self.scanner.write_bytes(rec + REC_UNLOCKED, b"\x00")  # lock: byte-only gate
                locked += 1
        return locked, unlocked

    def _family_level_map(self, reg, family: str) -> Dict[int, int]:
        """{content id -> level k} for every <Species><Family>L<k> of `family`, cached per family. Built
        from the (already-cached) intern index once, so the per-tick reconcile is just dict work."""
        cached = self._level_maps.get(family)
        if cached is None:
            rx = FAMILY_LEVEL_RE[family]
            cached = {cid: int(mo.group(1)) for name, cid in reg.build_index().items()
                      if (mo := rx.search(name))}
            self._level_maps[family] = cached
        return cached

    def reconcile_progressive_levels(self, family: str, received_count: int) -> bool:
        """Authoritatively gate a COUNT-BASED per-species LEVEL family (supplement/breeding/education/
        exhibit_enrichment): the progressive item's N-th received copy unlocks level <= N for EVERY
        species that has it - sets each <Species><Family>L<k> content's unlocked byte = (k <=
        received_count), and for the EFFECT_CAP_FAMILIES (breeding/supplement, whose byte is
        cosmetic) additionally caps the per-species effect value to the received count
        (_cap_effects). The barrier model: restart-correct, driven each tick from the received
        progressive count. Idempotent (writes on change). False if the maps aren't readable yet
        (retry)."""
        if family not in FAMILY_LEVEL_RE:
            logger.warning("progressive levels: unknown family %r", family)
            return False
        reg = self._registry_index()
        if reg is None:
            return False
        m = self._unlock_map()
        if m is None:
            return False
        self._maybe_refresh_names(m)
        levels = self._family_level_map(reg, family)
        if not levels:
            return True
        want_unlocked = {cid for cid, lvl in levels.items() if lvl <= received_count}
        try:
            locked, unlocked = self._apply_reward_gate(m, levels, want_unlocked)
            capped = self._cap_effects(family, m, levels, received_count)
            if family == "education":
                capped += self._sync_education_counter(m)
                capped += self._sync_education_levels(received_count)
        except Exception as e:
            logger.warning("%s level reconcile failed (%s)", family, e)
            return False
        if locked or unlocked or capped:
            logger.info("%s gate: level<=%d -> unlocked %d, locked %d, effect writes %d",
                        family, received_count, unlocked, locked, capped)
        return True

    def _sync_education_counter(self, m: "UnlockMap") -> int:
        """Keep the ZOO EDUCATION RATING numerator in step with the byte gate. The rating is
        counter@rs+0x52C / total@rs+0x528 (zoo-stats decomp fn_at_14049F49F: the fraction of
        education contents unlocked zoo-wide); the game only ever INCREMENTS the counter at a
        research-completion transition (fn_140E3FBF0), so a research grant beyond the received
        count leaks rating until re-synced. The byte gate has just made the type-3 unlocked bytes
        authoritative, so the correct numerator is simply the number of SET education bytes -
        recount and write it. Returns 1 if written."""
        rs = self.research._research_system()
        if not rs:
            return 0
        total = sum(1 for _rec, _cid, typ, flag in m.iter_records()
                    if typ == FAMILY_TYPE["education"] and flag)
        addr = rs + EDU_COUNTER_OFF
        cur = struct.unpack("<i", self.scanner.read_bytes(addr, 4))[0]
        if cur == total:
            return 0
        self.scanner.write_bytes(addr, struct.pack("<i", total))
        logger.info("education rating counter synced: %d -> %d (set education bytes)", cur, total)
        return 1

    def _resolve_edu_store_map(self) -> "Optional[tuple]":
        """(cap, buckets) of the per-species education level map, validated, or None. Cached;
        re-resolved when the cached header stops validating (park reload)."""
        for fresh in (False, True):
            hdr = self._edu_store_hdr
            if hdr is None or fresh:
                try:
                    from .animals import AnimalResolver
                    park = AnimalResolver(self.scanner).resolve_park()
                    if not park:
                        return None
                    parent = self.scanner.read_qword(park + EDU_STORE_PARENT_OFF)
                    store = self.scanner.read_qword(parent + EDU_STORE_OFF) if parent else 0
                    if not store:
                        return None
                    hdr = store + EDU_STORE_MAP_OFF
                except Exception:
                    return None
            try:
                count = self.scanner.read_qword(hdr + 0x08)
                cap = self.scanner.read_qword(hdr + 0x10)
                buckets = self.scanner.read_qword(hdr + 0x18)
            except Exception:
                count = cap = buckets = 0
            if buckets and 0 < count <= cap <= (1 << 12) and (cap & (cap - 1)) == 0:
                self._edu_store_hdr = hdr
                return cap, buckets
            self._edu_store_hdr = None
        return None

    def _sync_education_levels(self, received: int) -> int:
        """Write the per-species education unlock level (the panel's caps / GetEducationUnlockLevel)
        to min(received, 3) for EVERY species record - the same authoritative barrier model as the
        byte gate, so the display and the gate agree. Idempotent; sanity-guards the decoded records
        (small keys, levels <= 5) before writing anything. Returns the number of writes."""
        resolved = self._resolve_edu_store_map()
        if resolved is None:
            return 0
        cap, buckets = resolved
        bm = ((cap >> 3) + 7) & ~7
        try:
            data = self.scanner.read_bytes(buckets, bm + cap * EDU_REC_STRIDE)
        except Exception:
            return 0
        desired = min(max(received, 0), EDU_MAX_LEVEL)
        recs = []
        for i in range(cap):
            if not ((data[i >> 3] >> (i & 7)) & 1):
                continue
            base = bm + i * EDU_REC_STRIDE
            key = struct.unpack_from("<I", data, base)[0]
            cur = struct.unpack_from("<I", data, base + EDU_REC_LEVEL)[0]
            if key >= (1 << 20) or cur > 5:
                return 0    # not the store we think it is - touch nothing
            recs.append((buckets + base + EDU_REC_LEVEL, cur))
        writes = 0
        for addr, cur in recs:
            if cur != desired:
                self.scanner.write_bytes(addr, struct.pack("<I", desired))
                writes += 1
        if writes:
            logger.info("education panel levels synced: %d record(s) -> level %d (received %d)",
                        writes, desired, received)
        return writes

    def reconcile_mechanic(self, contents) -> bool:
        """Make each received MECHANIC research_reward content (shops/themes/blueprints/transport/staff/power)
        buildable by status-writing its gate ("ApGate<Content>", a NoneResearchable item re-pointed at
        /pz_install) to FACILITY_BUILDABLE_STATUS (4). The real research item stays the player-research
        location -> no false check (same decouple as barriers). Animal content is skipped (handled by
        grant()/rs+0x148). Driven each tick from the received research_reward set. Returns True if applied/
        nothing-to-do, False if the items map isn't readable yet (retry)."""
        try:
            name_to_id = self.research._mechanic_item_map()
        except Exception as e:
            logger.warning("mechanic reconcile: mechanic item map unreadable (%s)", e)
            return False
        if not name_to_id:
            return False
        want = {}  # gate item id -> content
        for content in contents:
            if not is_mechanic_content(content):
                continue
            iid = name_to_id.get("apgate" + _norm(content))
            if iid is not None:
                want[iid] = content
        if not want:
            return True
        try:
            for item_id, _lvl, status, _cat, status_addr in self.research.scan_records():
                if item_id in want and status < FACILITY_BUILDABLE_STATUS:
                    self.scanner.write_bytes(status_addr, bytes([FACILITY_BUILDABLE_STATUS]))
                    logger.info("[apply] mechanic content %s: gate buildable (item 0x%X, %d->%d)",
                                want[item_id], item_id, status, FACILITY_BUILDABLE_STATUS)
        except Exception as e:
            logger.warning("mechanic reconcile: scan/write failed (%s)", e)
            return False
        return True
