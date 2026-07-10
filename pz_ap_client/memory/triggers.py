"""MemoryTriggerSource - detects game events and emits location checks (A3).

The client's poll loop calls :meth:`poll` on a tick. It reads the relevant
memory anchors, maps any newly-satisfied trigger to its location ID via the
shared ``data.json``, and calls ``report_check`` (which debounces against
``effective_checked``). Detection is therefore naturally idempotent: a check
already sent is skipped.

Each ``trigger_type`` maps to a read:
  * ``research_complete`` - research_state_base + research[research_key] nonzero
  * ``first_breed``       - birth_event_counter increases (or per-species count)
  * ``milestone``         - metric anchor crosses threshold

Anything whose anchor/offset isn't filled in yet is simply skipped, so the loop
runs harmlessly against an incomplete table during the spike.
"""

from __future__ import annotations

import logging
from typing import Callable, List

from .anchors import AnchorTable
from .births import BirthDetector
from .exhibits import ExhibitDetector
from .releases import ReleaseDetector
from .research import ResearchReader
from .scanner import MemoryScanner
from .signatures import ANCHOR_SANITY

logger = logging.getLogger("PZClient")

# Ticks (~1s each) to wait for a deferred exhibit-census decrement before giving up on attributing a
# detected exhibit release. "Release to wild" can animate for a few seconds before the census updates, so
# this is generous; if it elapses with no drop the +0x318 census likely isn't the right live structure
# (the give-up diag dumps state to confirm). The count + conservation gate already fired regardless.
EXHIBIT_GIVEUP_TICKS = 20

# metric name in data.json -> anchor name in anchors.json
_MILESTONE_ANCHOR = {
    "zoo_rating": "zoo_rating",
    "guest_count": "guest_count",
    "conservation_release": "conservation_release_count",
}


class MemoryTriggerSource:
    def __init__(self, scanner: MemoryScanner, anchors: AnchorTable, game_data,
                 report_check: Callable[[int], None]):
        self.scanner = scanner
        self.anchors = anchors
        self.game_data = game_data
        self.report_check = report_check
        # A3 research_complete: reads the research-system items map via a stable master-root
        # chain (research record +0x10 = species handle, +0x49 = status; see research.py). The
        # RegistryResolver + engine_token map let the reader attribute/locate EVERY species via the
        # live symbol registry (not just the captured ones) - the namespace bridge across Track B's
        # abbreviated stringids and the engine's full tokens.
        from .registry import RegistryResolver
        token_to_key = {s.engine_token: s.key for s in game_data.species if s.engine_token}
        self.research = ResearchReader(scanner, registry=RegistryResolver(scanner),
                                       token_to_key=token_to_key)
        # A3 birth detection: software-detour on the add-animal insert + life-stage classification
        # (newborn vs bought); species attributed via entity+0x50 reverse-mapped through the
        # research handle table (shared reader). See births.py / the A2 spike.
        self.births = BirthDetector(scanner, research=self.research)
        # A3 exhibit detection: EXHIBIT animals use a separate add path (FUN_140a31f20) and have no newborn
        # life-stage, so the habitat detector misses them. The exhibit detour captures species id + a caller-
        # stack window; acquire-vs-breed is classified by a buy-handler return address on the stack. Its
        # detected keys feed the SAME bred/acquired sets, so the existing first_breed/first_acquire pollers
        # fire for exhibit species too. Gated by EXHIBIT_DETECT_ENABLED; shares the registry-backed reader.
        self.exhibits = ExhibitDetector(scanner, research=self.research)
        self._bred_species: set = set()      # species_keys observed born (cumulative)
        self._acquired_species: set = set()  # species_keys observed acquired (cumulative)
        self._released_species: set = set()  # species_keys observed released (cumulative)
        self._last_habitat_count = 0         # habitat-release high-water (attribute only NEW releases)
        self._warned_release_attr = False    # one-shot notice that per-species release is unmapped
        # Exhibit conservation_release: PLACED releases are attributed by census-diff. The release HOOK
        # count rises synchronously, but the census decrement is DEFERRED (the release posts
        # ExhibitAnimalReleasedMessage, processed later), so we hold a baseline census across the lag:
        # each detected exhibit release is "pending" until a species' census count drops below the
        # baseline, which attributes it. Baseline only advances when nothing is pending (so buys/births
        # don't erase a not-yet-resolved drop). A pending release is given up after a few ticks (count +
        # milestone still fire) to avoid a stuck baseline if the released species' handle isn't mappable.
        self._last_exhibit_count = 0
        self._exhibit_baseline: "dict | None" = None   # {species_handle -> count} accounted-for PLACED census
        self._pending_exhibit = 0                      # detected exhibit releases awaiting attribution
        self._pending_exhibit_ticks = 0                # ticks a pending release has waited (give-up guard)
        self._exhibit_storage_hint_logged = False      # once-only unattributed-release notice
        # STORAGE exhibit attribution (the placed-only census can't see storage): PRIMARY = the
        # storage-branch CAPTURE detour records the released animal id (+ the def-map holder H =
        # *(mgr+0xF8)); we resolve id -> species via the {animal_id -> def} map at *(H+0x358)+0x108,
        # cached per tick BEFORE the release (race-free). SECONDARY = diffing H's +0x2A0 owned-id set
        # (the release removes the id synchronously) - hookless, same cache. The census diff stays as
        # the PLACED fallback. (The structures hang off H, not the manager - the +0xF8 rebind the
        # 2026-07-06 id-roster path missed, which is why it read None live on 2026-07-08.)
        self._exhibit_prev_ids: "set | None" = None    # last tick's owned exhibit animal ids
        self._exhibit_id_species: dict = {}            # animal_id -> species_key (from the def map)
        self._exhibit_cap_cursor = 0                   # storage-capture ring drain cursor
        self._exhibit_captured_done: set = set()       # ids attributed via capture (id-set diff: evict only)
        self._exhibit_capture_unresolved: set = set()  # captured ids awaiting species (def entries survive
                                                       # the release, so retry each tick while pending)
        self._exhibit_roster_warned = False            # one-shot: holder/id-set unresolvable diagnostic
        # A3 conservation_release: software-detour on the release-to-wild executor that
        # counts releases (no stable game counter exists - see releases.py / the A2 spike).
        self.releases = ReleaseDetector(scanner)

    def poll(self, already_checked: set) -> List[int]:
        """One detection tick. Returns the list of newly-reported location ids. Each detector is
        isolated so one failing read (e.g. an anchor whose chain isn't live yet because the zoo
        isn't loaded) can't abort the others or the rest of the poll-loop tick (item application,
        gate reconciliation, the preflight self-check)."""
        if not self.scanner.attached and not self.scanner.attach():
            return []
        fired: List[int] = []
        # _poll_inserts drains births+acquisitions once and updates the cumulative sets that
        # _poll_first_breed / _poll_first_acquire read (the insert ring must be drained once/tick).
        for sub in (self._poll_research, self._poll_inserts, self._poll_first_breed,
                    self._poll_first_acquire, self._poll_conservation_release, self._poll_milestones):
            try:
                fired += sub(already_checked)
            except Exception:
                logger.exception("detection sub-step %s failed (skipping this tick)", sub.__name__)
        for loc_id in fired:
            self.report_check(loc_id)
        return fired

    # -- research --------------------------------------------------------------

    def _poll_research(self, already: set) -> List[int]:
        """Detect completed research via ResearchReader (research-system items map).
        Each data.json research_key is dispatched through is_research_complete:
        welfare_<species> -> leveled animal-research rule; mapped non-welfare keys (e.g.
        mechanic research) -> their item's status == 4. Unmapped keys never fire."""
        out = []
        snap = self.research._snapshot()  # read the research map once per tick
        if snap is None:
            return out
        for loc in self.game_data.locations_by_trigger("research_complete"):
            if loc.id in already:
                continue
            key = loc.trigger_args.get("research_key") or ""
            level = loc.trigger_args.get("level")  # per-level welfare locations carry a 1-based level
            if self.research.is_research_complete(key, snap, level=level):
                logger.info("Detected research complete: %s%s", key, f" L{level}" if level else "")
                out.append(loc.id)
        return out

    # -- inserts (births + acquisitions share one drain) -----------------------

    def _poll_inserts(self, already: set) -> List[int]:
        """Drain the add-animal rings once per tick and update the cumulative bred/acquired sets. Fires
        nothing itself - _poll_first_breed / _poll_first_acquire read the sets. Both the habitat detector
        (BirthDetector) and the exhibit detector (ExhibitDetector) feed the same sets; each must be drained
        once per tick (its ring cursor advances), and an exhibit-detector failure can't abort the habitat
        drain (or vice-versa)."""
        born, acquired = self.births.poll_events()
        self._bred_species.update(born)
        self._acquired_species.update(acquired)
        try:
            exh_born, exh_acquired = self.exhibits.poll_events()
            self._bred_species.update(exh_born)
            self._acquired_species.update(exh_acquired)
        except Exception:
            logger.exception("exhibit insert drain failed (skipping this tick)")
        return []

    def _poll_first_breed(self, already: set) -> List[int]:
        """Fire first_breed for each species observed born (newborn insert, life-stage 0), so
        market purchases never trigger it. Reads the set _poll_inserts maintains."""
        if not self._bred_species:
            return []
        return [loc.id for loc in self.game_data.locations_by_trigger("first_breed")
                if loc.id not in already and loc.trigger_args.get("species_key") in self._bred_species]

    def _poll_first_acquire(self, already: set) -> List[int]:
        """Fire first_acquire for each species observed acquired (a non-newborn insert: market
        buy/trade/transfer). Reads the set _poll_inserts maintains."""
        if not self._acquired_species:
            return []
        return [loc.id for loc in self.game_data.locations_by_trigger("first_acquire")
                if loc.id not in already and loc.trigger_args.get("species_key") in self._acquired_species]

    def _poll_conservation_release(self, already: set) -> List[int]:
        """Per-species conservation_release (cr_<species>), two attribution paths that share the cr_ set:
        HABITAT - on each NEW habitat release the species-capture detour recorded the released animal's
        handle (resolved to a species_key via the insert/roster cache, race-free); EXHIBIT - exhibit
        animals capture no handle, so _poll_exhibit_release diffs the exhibit {species->count} census
        across a detected exhibit release. Both degrade to nothing (no false checks) if attribution fails."""
        locs = self.game_data.locations_by_trigger("conservation_release")
        if not locs:
            return []
        # Keep handle->species fresh from the live roster (habitat + storage) so a release resolves from
        # cache. A released animal leaves the roster within ms - too fast to resolve live on the next poll -
        # but this sweep cached it on a prior tick. Also covers loaded saves + release-straight-from-storage,
        # which the insert hook never sees. Cheap for normal zoos; best-effort (no-op if no zoo / not resolvable).
        try:
            self.births.sweep_roster()
        except Exception:
            pass
        # HABITAT releases: handle-based attribution (release_species capture + insert/roster cache).
        hcount = self.releases.habitat_count()
        if hcount > self._last_habitat_count:
            self._last_habitat_count = hcount
            key = self._attribute_release()
            if key:
                self._released_species.add(key)
            elif not self._warned_release_attr:
                self._warned_release_attr = True
                logger.info("conservation_release: habitat release #%d observed but species attribution "
                            "failed (handle unresolved against any animal manager - entity already "
                            "freed, or species-capture hook not installed). cr_ checks won't fire "
                            "for it; the milestone (count) still works.", hcount)
        # EXHIBIT releases: census-diff attribution (no handle is captured for the exhibit action).
        try:
            self._poll_exhibit_release()
        except Exception:
            logger.exception("conservation_release: exhibit census-diff failed (skipping this tick)")
        if not self._released_species:
            return []
        return [loc.id for loc in locs
                if loc.id not in already and loc.trigger_args.get("species_key") in self._released_species]

    def _read_exhibit_census(self) -> "dict | None":
        """RAW {species_handle -> count} for exhibit animals in the zoo (placed + stored), or None if the
        exhibit manager isn't resolvable (no zoo / mid-load). We keep RAW handles (not species_keys) so the
        diff sees a drop even for a handle the research map doesn't cover - mapping happens at attribution
        time, and an unmapped drop is logged (not silently lost). Empty dict (zoo, no exhibit animals) is
        distinct from None (not resolvable)."""
        resolver = self.births.resolver
        mgr = resolver.resolve_exhibit_manager()
        if not mgr:
            return None
        return resolver.read_exhibit_census(mgr)

    def _poll_exhibit_release(self) -> None:
        """Attribute exhibit conservation_release across a detected release (the hook count rises
        synchronously). STORAGE releases: PRIMARY = the storage-branch capture (released animal id ->
        species via the def-map cache); SECONDARY = diffing the holder's +0x2A0 owned-id set (the
        release removes the id synchronously). PLACED releases: the census diff (deferred decrement,
        so pending releases wait for it). Pairing any of them with the hook count distinguishes a
        RELEASE from a death/transfer."""
        # DETECT the release FIRST, independent of any roster read. The exhibit hook count is session-
        # scoped (starts at 0), so counting from 0 captures every release since attach - even when both
        # roster structures are unreadable this tick.
        ecount = self.releases.exhibit_count()
        new = ecount - self._last_exhibit_count
        if new > 0:
            self._pending_exhibit += new
            logger.info("conservation_release: exhibit release detected (hook count %d, +%d) - resolving "
                        "species via storage capture / id-set diff / placed census", ecount, new)
        self._last_exhibit_count = ecount
        try:
            self._drain_exhibit_captures()     # PRIMARY (storage): captured released id -> species
        except Exception:
            logger.exception("conservation_release: exhibit capture drain failed (diff paths only)")
        try:
            self._poll_exhibit_roster_diff()   # SECONDARY (storage): id-set diff on the holder
        except Exception:
            logger.exception("conservation_release: exhibit id-roster diff failed (census fallback only)")
        raw = self._read_exhibit_census()   # PLACED census (may be None: no placed animals / mid-load)
        if raw is not None:
            if self._exhibit_baseline is None:
                self._exhibit_baseline = raw            # prime the placed-census baseline (no fire)
            elif self._pending_exhibit > 0:
                self._attribute_exhibit_drops(raw)      # a PLACED release drops a baseline count
        self._retire_pending_exhibit(raw)

    def _retire_pending_exhibit(self, raw: "dict | None") -> None:
        """Retire pending releases after the tick budget even if no path explained them (e.g. the
        released animal's species token wasn't mappable). The count + milestone already fired
        regardless. When nothing is pending, advance the placed-census baseline (accounts
        buys/births + resolved drops)."""
        if self._pending_exhibit > 0:
            self._pending_exhibit_ticks += 1
            if self._pending_exhibit_ticks >= EXHIBIT_GIVEUP_TICKS:
                if not self._exhibit_storage_hint_logged:
                    self._exhibit_storage_hint_logged = True
                    logger.info("conservation_release: an exhibit release wasn't attributed to a species "
                                "(no capture, id-set removal, or census drop matched a known species). "
                                "cr_ won't fire for it; the release count + milestone are still credited.")
                self._pending_exhibit = 0
        if self._pending_exhibit == 0:
            self._pending_exhibit_ticks = 0
            self._exhibit_capture_unresolved.clear()   # nothing pending left for a retry to consume
            if raw is not None:
                self._exhibit_baseline = raw

    def _exhibit_holder(self) -> "int | None":
        """The exhibit def-map holder H (owner of the +0x2A0 owned-id set and the {animal_id -> def}
        map): the storage-capture's recorded r15 when a capture has fired (ground truth), else the
        park-chain deref *(park+0x1D0 + 0xF8). Either way the consumers' pow2 guards validate it -
        a wrong pointer reads as None, never as a false roster."""
        holder = self.releases.exhibit_capture_holder()
        if holder:
            return holder
        resolver = self.births.resolver
        mgr = resolver.resolve_exhibit_manager()
        return resolver.resolve_exhibit_defmap_holder(mgr) if mgr else None

    def _drain_exhibit_captures(self) -> None:
        """PRIMARY storage attribution: resolve each captured released exhibit-animal id -> species
        via the def-map cache (filled on a PRIOR tick, race-free), falling back to a fresh def-map
        read. Def entries SURVIVE the release (live-observed), so an id that doesn't resolve this
        tick (mid-load, research map not ready) is RETRIED every tick while its release is pending.
        A resolved capture consumes one pending release; its id is remembered so the id-set diff
        only evicts (never double-consumes) the same removal."""
        count, ids = self.releases.exhibit_release_events(self._exhibit_cap_cursor)
        self._exhibit_cap_cursor = count
        retry, self._exhibit_capture_unresolved = self._exhibit_capture_unresolved, set()
        fresh_defs: "dict | None" = None
        for aid, is_new in [(a, False) for a in sorted(retry)] + [(a, True) for a in ids]:
            key = self._exhibit_id_species.get(aid)
            if key is None:
                if fresh_defs is None:
                    fresh_defs = self._fresh_defs()
                key = self._species_from_def(fresh_defs.get(aid))
            self._consume_capture(aid, key, is_new)

    def _fresh_defs(self) -> dict:
        """One live def-map read for ids not yet in the cache ({} if the holder is unresolvable)."""
        holder = self._exhibit_holder()
        return (self.births.resolver.read_exhibit_defs(holder) or {}) if holder else {}

    def _consume_capture(self, aid: int, key: "str | None", is_new: bool = True) -> None:
        """Fire cr_ for one captured released id (consuming a pending release), or park it for a
        retry while the release stays pending (the miss is logged once, on the capture tick)."""
        if key and self._pending_exhibit > 0:
            self._released_species.add(key)
            self._pending_exhibit -= 1
            self._exhibit_captured_done.add(aid)
            self._exhibit_id_species.pop(aid, None)
            self._exhibit_capture_unresolved.discard(aid)
            logger.info("conservation_release: exhibit STORAGE release attributed -> %s "
                        "(captured animal id 0x%X)", key, aid)
        elif self._pending_exhibit > 0:
            self._exhibit_capture_unresolved.add(aid)
            if is_new:
                logger.info("conservation_release: storage release captured (animal id 0x%X) but no "
                            "species mapped for it yet - retrying via the def map while pending "
                            "(the id-set/census diffs may also attribute it)", aid)

    def _species_from_def(self, entry) -> "str | None":
        """Species for one (species_handle, [name strings]) def-map entry. The handle through the
        research map is authoritative; the token match over the strings is the fallback (live zoos
        exist where the strings hold ONLY given names - see animals.DEF_ENTRY_SPECIES)."""
        if not entry:
            return None
        handle, names = entry
        key = (self.research.handle_key_map() or {}).get(handle) if handle else None
        return key or self._match_species_name(names)

    def _match_species_name(self, names) -> "str | None":
        """First def-string candidate that maps to a species_key (self-validating - a non-species
        string like an animal's given name simply doesn't map)."""
        return next((k for nm in names if (k := self.research.species_key_for_name(nm))), None)

    def _poll_exhibit_roster_diff(self) -> None:
        """Maintain the {animal_id -> species_key} cache from the holder's id set + def map, and
        attribute pending releases to the species of ids that VANISHED from the set. Ids also vanish
        on death/sale, so removals only attribute while a release is pending (same pairing as the
        census path); every removal evicts its cache entry either way."""
        resolver = self.births.resolver
        holder = self._exhibit_holder()
        ids = resolver.read_exhibit_ids(holder) if holder else None
        if ids is None:
            # Not resolvable this tick - keep the previous snapshot (don't fake an empty set). Say so
            # ONCE: a session-long holder failure silently killed both storage paths (live 2026-07-10,
            # *(mgr+0xF8) NULL) and only this breadcrumb distinguishes that from "no releases yet".
            if not self._exhibit_roster_warned and self.births.resolver.resolve_exhibit_manager():
                self._exhibit_roster_warned = True
                logger.info("conservation_release[diag]: exhibit id set unresolvable (holder %s) - "
                            "storage releases will rely on the capture + def-map path only",
                            hex(holder) if holder else None)
            return
        self._exhibit_roster_warned = False   # resolvable again - re-arm the breadcrumb
        fresh = ids - self._exhibit_id_species.keys()
        if fresh:
            self._cache_exhibit_species(resolver, holder, fresh)
        if self._exhibit_prev_ids is not None:
            self._attribute_exhibit_removals(self._exhibit_prev_ids - ids)
        self._exhibit_prev_ids = ids

    def _cache_exhibit_species(self, resolver, holder: int, fresh: set) -> None:
        """Resolve + cache species_keys for newly-seen exhibit animal ids via the def map: the
        species HANDLE @entry+0x30 through the research map first, the string-token match as
        fallback (the strings can be all given names - see animals.DEF_ENTRY_SPECIES)."""
        defs = resolver.read_exhibit_defs(holder) or {}
        for aid in fresh:
            key = self._species_from_def(defs.get(aid))
            if key:
                self._exhibit_id_species[aid] = key
            else:
                logger.info("conservation_release: exhibit animal id 0x%X cached WITHOUT a species "
                            "(def entry %s unmapped) - its release would fall back to the census",
                            aid, defs.get(aid))

    def _attribute_exhibit_removals(self, removed: set) -> None:
        """Consume pending releases for ids that left the owned-id set; evict their cache entries.
        An id the capture drain already attributed is only evicted (no second pending consumed for
        the same release)."""
        for aid in removed:
            key = self._exhibit_id_species.pop(aid, None)
            if aid in self._exhibit_captured_done:
                self._exhibit_captured_done.discard(aid)
                continue
            if self._pending_exhibit > 0 and key:
                self._released_species.add(key)
                self._pending_exhibit -= 1
                logger.info("conservation_release: exhibit release attributed -> %s (animal id 0x%X "
                            "left the owned-id set; works for storage releases)", key, aid)

    def _attribute_exhibit_drops(self, raw: dict) -> None:
        """For each baseline handle whose census count dropped, consume one pending release and (if the
        handle maps to a species) fire its cr_. An unmapped drop is logged, not silently lost."""
        h2k = self.research.handle_key_map() or {}
        for handle, base_c in self._exhibit_baseline.items():
            now = raw.get(handle, 0)
            while now < base_c and self._pending_exhibit > 0:
                key = h2k.get(handle)
                if key:
                    self._released_species.add(key)
                    logger.info("conservation_release: exhibit release attributed -> %s "
                                "(species handle 0x%X census drop)", key, handle)
                else:
                    logger.info("conservation_release: exhibit census dropped for handle 0x%X but it's NOT "
                                "in the research map (cr_ can't fire). Research-map handles: %s",
                                handle, [hex(k) for k in list(h2k)[:16]])
                base_c -= 1
                self._exhibit_baseline[handle] = base_c   # account this drop (don't re-attribute)
                self._pending_exhibit -= 1

    def _attribute_release(self) -> "str | None":
        """Resolve the last released animal handle -> species_key. None if it can't be attributed.

        PRIMARY (race-free): the births insert-cache, which recorded handle->species_key when the
        animal ENTERED the zoo. A release removes the animal within tens of ms (deferred message),
        but the client polls ~1s, so resolving the roster live at release time almost always finds
        the entity already freed - whereas the cache was filled long before. The AP scenario starts
        empty, so every releasable animal was inserted (bought/traded/born) while attached.

        FALLBACK (best-effort): live resolution via the release site's captured ``*(rbp+0x48)`` (as
        zoo, then manager) then the births-captured zoo - covers an animal already present at attach
        (never inserted), though it may miss if the entity was freed before this tick."""
        handle = self.releases.last_released_handle()
        if not handle:
            logger.info("conservation_release[diag]: species-capture hook fired but recorded NO handle "
                        "(sp_scratch=%s) - the capture isn't writing rsi",
                        bool(getattr(self.releases, "sp_scratch", None)))
            return None
        cached = self.births.handle_species.get(handle)
        if cached:
            logger.info("conservation_release: attributed released handle 0x%X -> %s (insert cache)",
                        handle, cached)
            return cached
        resolver = self.births.resolver
        handle2key = self.research.handle_key_map()
        mgr_cand = self.releases.last_release_manager()
        candidates = []
        if mgr_cand:
            candidates.append(resolver.resolve_entity(mgr_cand, handle))             # *(rbp+0x48) as zoo
            candidates.append(resolver.resolve_entity_via_manager(mgr_cand, handle))  # ... as manager
        if self.births.last_zoo:
            candidates.append(resolver.resolve_entity(self.births.last_zoo, handle))  # births zoo
        for entity in candidates:
            if entity is None:
                continue
            sh = resolver.species_handle(entity)
            key = handle2key.get(sh) if sh is not None else None
            if key:
                logger.info("conservation_release: attributed released handle 0x%X -> %s "
                            "(live resolve, species handle 0x%X)", handle, key, sh)
                return key
        # Attribution failed: dump what we had, so one release tells us WHY. The decisive clue is whether
        # the released handle (rsi = nAnimalID) is the same id-space as the births-cache keys (the insert
        # handle); if not, the cache can never hit and we need GetSpecies(nAnimalID)-style resolution.
        sample = [hex(k) for k in list(self.births.handle_species)[:6]]
        live_hits = sum(1 for e in candidates if e is not None)
        logger.info("conservation_release[diag]: FAILED. released handle=0x%X; births cache=%d entries "
                    "(sample keys: %s); mgr_cand=0x%X; live-resolve found %d/%d candidate entities. If the "
                    "released handle is nowhere near the cache keys, it's a different id-space (nAnimalID).",
                    handle, len(self.births.handle_species), sample, mgr_cand or 0, live_hits, len(candidates))
        return None

    def close(self) -> None:
        """Restore the code-patch detours (call on disconnect / shutdown)."""
        self.births.shutdown()
        self.exhibits.shutdown()
        self.releases.shutdown()

    # -- milestones ------------------------------------------------------------

    def _metric_value(self, metric: str) -> "float | int | None":
        """Current value of a milestone metric (already display-adjusted), or None if unreadable."""
        if metric == "conservation_release":
            # No stable game counter exists (A2 spike); the release-detour counts releases observed
            # this session. Threshold is 1 and AP checks are sticky, so one observed release suffices.
            # (For a higher threshold, persist a cross-session running total in client state.)
            return self.releases.count()
        anchor_name = _MILESTONE_ANCHOR.get(metric)
        if anchor_name is None:
            return None
        val = self.anchors.read(self.scanner, anchor_name)
        if val is None:
            return None
        # Reject an out-of-range read as garbage (no zoo loaded / stale chain) so it can't fire checks.
        # Live 2026-07-08: a client connected before the zoo loaded read guest_count=1953393007 (ASCII
        # "port"), which is >= every threshold, and fired ALL six Guests-* checks at once. The preflight
        # flags this range but ran AFTER the poll; the same sanity gate must be in the poll itself.
        lohi = ANCHOR_SANITY.get(metric)
        if lohi is not None and not (lohi[0] <= val <= lohi[1]):
            logger.debug("milestone %s: value %s outside sane %s - treating as unresolved (no zoo / "
                         "stale chain); not firing", metric, val, lohi)
            return None
        if metric == "zoo_rating":
            # zoo_rating is the clamp01 reputation float x5 (continuous stars). Its clamped max is 1.0,
            # so a 5-star zoo reads ~4.98 stars, not exactly 5.0, and `>= 5` never fires (live 2026-07-08:
            # raw 0.9962 -> 4.98). Compare the DISPLAYED star value - the UI rounds to half-stars - so
            # threshold N fires when the player sees N stars.
            val = round(val * 2) / 2
        return val

    def _poll_milestones(self, already: set) -> List[int]:
        out = []
        for loc in self.game_data.locations_by_trigger("milestone"):
            if loc.id in already:
                continue
            metric = loc.trigger_args.get("metric")
            threshold = loc.trigger_args.get("threshold")
            val = self._metric_value(metric)
            if val is not None and val >= threshold:
                logger.info("Detected milestone %s >= %s (=%s)", metric, threshold, val)
                out.append(loc.id)
        return out
