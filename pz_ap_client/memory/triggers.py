"""MemoryTriggerSource — detects game events and emits location checks (A3).

The client's poll loop calls :meth:`poll` on a tick. It reads the relevant
memory anchors, maps any newly-satisfied trigger to its location ID via the
shared ``data.json``, and calls ``report_check`` (which debounces against
``effective_checked``). Detection is therefore naturally idempotent: a check
already sent is skipped.

Each ``trigger_type`` maps to a read:
  * ``research_complete`` — research_state_base + research[research_key] nonzero
  * ``first_breed``       — birth_event_counter increases (or per-species count)
  * ``milestone``         — metric anchor crosses threshold

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
        # chain (research record +0x10 = species handle, +0x49 = status; see research.py).
        self.research = ResearchReader(scanner)
        # A3 birth detection: software-detour on the add-animal insert + life-stage classification
        # (newborn vs bought); species attributed via entity+0x50 reverse-mapped through the
        # research handle table (shared reader). See births.py / the A2 spike.
        self.births = BirthDetector(scanner, research=self.research)
        self._bred_species: set = set()  # species_keys observed born (cumulative)
        # A3 conservation_release: software-detour on the release-to-wild executor that
        # counts releases (no stable game counter exists — see releases.py / the A2 spike).
        self.releases = ReleaseDetector(scanner)

    def poll(self, already_checked: set) -> List[int]:
        """One detection tick. Returns the list of newly-reported location ids."""
        if not self.scanner.attached and not self.scanner.attach():
            return []
        fired: List[int] = []
        fired += self._poll_research(already_checked)
        fired += self._poll_first_breed(already_checked)
        fired += self._poll_milestones(already_checked)
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
            if self.research.is_research_complete(key, snap):
                logger.info("Detected research complete: %s", key)
                out.append(loc.id)
        return out

    # -- first breed -----------------------------------------------------------

    def _poll_first_breed(self, already: set) -> List[int]:
        """Detect births via the add-animal insert detour + newborn classification
        (BirthDetector), then fire the first_breed location for each bred species.

        The detector resolves each inserted animal and reports the species_key only
        when it's a newborn (life-stage 0), so market purchases never trigger this.
        We accumulate bred species and match them to first_breed locations.
        """
        for key in self.births.poll():
            self._bred_species.add(key)
        if not self._bred_species:
            return []
        out = []
        for loc in self.game_data.locations_by_trigger("first_breed"):
            if loc.id in already:
                continue
            if loc.trigger_args.get("species_key") in self._bred_species:
                out.append(loc.id)
        return out

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
