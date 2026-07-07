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
        # Exhibit conservation_release is attributed by census-diff (no handle is captured at the exhibit
        # action). The release HOOK count rises synchronously, but the census decrement is DEFERRED (the
        # release posts ExhibitAnimalReleasedMessage, processed later), so we hold a baseline census across
        # the lag: each detected exhibit release is "pending" until a species' census count drops below the
        # baseline, which attributes it. Baseline only advances when nothing is pending (so buys/births don't
        # erase a not-yet-resolved drop). A pending release is given up after a few ticks (count + milestone
        # still fire) to avoid a stuck baseline if the released species' handle isn't in the research map.
        self._last_exhibit_count = 0
        self._exhibit_baseline: "dict | None" = None   # {species_handle -> count} accounted-for PLACED census
        self._pending_exhibit = 0                      # detected exhibit releases awaiting attribution
        self._pending_exhibit_ticks = 0                # ticks a pending release has waited (give-up guard)
        self._exhibit_storage_hint_logged = False      # once-only unattributed-release notice
        # PRIMARY exhibit attribution = ID-ROSTER DIFF (covers STORAGE releases, which the placed-only
        # census can't): the manager's +0x2A0 id set holds every owned exhibit animal and the release
        # removes the id SYNCHRONOUSLY (FUN_146048940 decomp); the def map (*(mgr+0x358)+0x108) names
        # each id's species. We cache {animal_id -> species_key} as ids appear, then a removed id under
        # a pending release names the released species. The census diff stays as the fallback.
        self._exhibit_prev_ids: "set | None" = None    # last tick's owned exhibit animal ids
        self._exhibit_id_species: dict = {}            # animal_id -> species_key (from the def map)
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
        synchronously). PRIMARY: diff the manager's +0x2A0 owned-animal ID SET - the release removes the
        id synchronously and the id-species cache (def map) names it; covers PLACED and STORED animals
        alike. FALLBACK: the placed-only census diff (deferred decrement, so pending releases wait for
        it). Pairing either diff with the hook count distinguishes a RELEASE from a death/transfer."""
        # DETECT the release FIRST, independent of any roster read. The exhibit hook count is session-
        # scoped (starts at 0), so counting from 0 captures every release since attach - even when both
        # roster structures are unreadable this tick.
        ecount = self.releases.exhibit_count()
        new = ecount - self._last_exhibit_count
        if new > 0:
            self._pending_exhibit += new
            logger.info("conservation_release: exhibit release detected (hook count %d, +%d) - resolving "
                        "species via id-roster diff (census fallback)", ecount, new)
        self._last_exhibit_count = ecount
        try:
            self._poll_exhibit_roster_diff()   # PRIMARY: id-set diff (handles storage releases)
        except Exception:
            logger.exception("conservation_release: exhibit id-roster diff failed (census fallback only)")
        raw = self._read_exhibit_census()   # PLACED census (may be None: no placed animals / mid-load)
        if raw is not None:
            if self._exhibit_baseline is None:
                self._exhibit_baseline = raw            # prime the placed-census baseline (no fire)
            elif self._pending_exhibit > 0:
                self._attribute_exhibit_drops(raw)      # a PLACED release drops a baseline count
        # Retire pending releases after the budget even if neither diff explained them (e.g. the released
        # animal's species token wasn't mappable). The count + milestone already fired regardless.
        if self._pending_exhibit > 0:
            self._pending_exhibit_ticks += 1
            if self._pending_exhibit_ticks >= EXHIBIT_GIVEUP_TICKS:
                if not self._exhibit_storage_hint_logged:
                    self._exhibit_storage_hint_logged = True
                    logger.info("conservation_release: an exhibit release wasn't attributed to a species "
                                "(no id-roster removal or census drop matched a known species). cr_ won't "
                                "fire for it; the release count + milestone are still credited.")
                self._pending_exhibit = 0
        if self._pending_exhibit == 0:
            self._pending_exhibit_ticks = 0
            if raw is not None:
                self._exhibit_baseline = raw   # advance baseline (accounts buys/births + resolved drops)

    def _poll_exhibit_roster_diff(self) -> None:
        """Maintain the {animal_id -> species_key} cache from the exhibit manager's id set + def map, and
        attribute pending releases to the species of ids that VANISHED from the set. Ids also vanish on
        death/sale, so removals only attribute while a release is pending (same pairing as the census
        path); every removal evicts its cache entry either way."""
        resolver = self.births.resolver
        mgr = resolver.resolve_exhibit_manager()
        ids = resolver.read_exhibit_ids(mgr) if mgr else None
        if ids is None:
            return   # not resolvable this tick - keep the previous snapshot (don't fake an empty set)
        fresh = ids - self._exhibit_id_species.keys()
        if fresh:
            self._cache_exhibit_species(resolver, mgr, fresh)
        if self._exhibit_prev_ids is not None:
            self._attribute_exhibit_removals(self._exhibit_prev_ids - ids)
        self._exhibit_prev_ids = ids

    def _cache_exhibit_species(self, resolver, mgr: int, fresh: set) -> None:
        """Resolve + cache species_keys for newly-seen exhibit animal ids via the def map. The def
        object's string fields aren't positionally guaranteed - match ANY against the species token map
        (self-validating: a non-species string simply doesn't map)."""
        names = resolver.read_exhibit_def_names(mgr) or {}
        for aid in fresh:
            key = next((k for nm in names.get(aid, ())
                        if (k := self.research.species_key_for_name(nm))), None)
            if key:
                self._exhibit_id_species[aid] = key
            else:
                logger.info("conservation_release: exhibit animal id 0x%X cached WITHOUT a species "
                            "(def names %s unmapped) - its release would fall back to the census",
                            aid, names.get(aid))

    def _attribute_exhibit_removals(self, removed: set) -> None:
        """Consume pending releases for ids that left the owned-id set; evict their cache entries."""
        for aid in removed:
            key = self._exhibit_id_species.pop(aid, None)
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

    def _poll_milestones(self, already: set) -> List[int]:
        out = []
        for loc in self.game_data.locations_by_trigger("milestone"):
            if loc.id in already:
                continue
            metric = loc.trigger_args.get("metric")
            threshold = loc.trigger_args.get("threshold")
            if metric == "conservation_release":
                # No stable game counter exists (A2 spike); the release-detour counts
                # releases observed this session. Threshold is 1, and AP checks are
                # sticky, so a single observed release is sufficient. (For a higher
                # threshold, persist a cross-session running total in client state.)
                val = self.releases.count()
            else:
                anchor_name = _MILESTONE_ANCHOR.get(metric)
                if anchor_name is None:
                    continue
                val = self.anchors.read(self.scanner, anchor_name)
            if val is not None and val >= threshold:
                logger.info("Detected milestone %s >= %s (=%s)", metric, threshold, val)
                out.append(loc.id)
        return out
