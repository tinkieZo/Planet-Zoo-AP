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
from .releases import ReleaseDetector
from .research import ResearchReader
from .scanner import MemoryScanner

logger = logging.getLogger("PZClient")

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
        self._bred_species: set = set()      # species_keys observed born (cumulative)
        self._acquired_species: set = set()  # species_keys observed acquired (cumulative)
        self._warned_release_attr = False    # one-shot notice that per-species release is unmapped
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
        """Drain the add-animal insert ring once per tick and update the cumulative bred/acquired
        sets. Fires nothing itself - _poll_first_breed / _poll_first_acquire read the sets."""
        born, acquired = self.births.poll_events()
        self._bred_species.update(born)
        self._acquired_species.update(acquired)
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
        """Per-species conservation_release (cr_<species>) locations. The ReleaseDetector counts
        releases but does NOT attribute species (the release hook doesn't capture the animal handle
        yet), so we cannot tell WHICH species was released - these can't fire correctly and are left
        un-detected (no false checks). Unlock: extend ReleaseDetector to resolve the released
        animal's species like births (capture/RE-gated). Logged once when a release is seen."""
        locs = self.game_data.locations_by_trigger("conservation_release")
        if not locs or self._warned_release_attr:
            return []
        if self.releases.count() > 0:
            self._warned_release_attr = True
            logger.info("conservation_release: %d per-species locations present and a release was "
                        "observed, but the release detector has no species attribution yet - these "
                        "checks won't fire until that RE lands (see releases.py).", len(locs))
        return []

    def close(self) -> None:
        """Restore the code-patch detours (call on disconnect / shutdown)."""
        self.births.shutdown()
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
