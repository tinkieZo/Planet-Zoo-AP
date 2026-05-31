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
from typing import Callable, Dict, List, Optional

from .anchors import AnchorTable
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
        # Baseline for first_breed counter diffing, captured on first good read.
        self._birth_baseline: Optional[int] = None
        # Per-species baseline if births are tracked per species.
        self._species_birth_baseline: Dict[str, int] = {}

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
        out = []
        for loc in self.game_data.locations_by_trigger("research_complete"):
            if loc.id in already:
                continue
            key = loc.trigger_args.get("research_key")
            val = self.anchors.read_entity(self.scanner, "research_state_base", "research", key)
            if val:  # nonzero = complete (TODO spike: confirm sentinel)
                logger.info("Detected research complete: %s", key)
                out.append(loc.id)
        return out

    # -- first breed -----------------------------------------------------------

    def _poll_first_breed(self, already: set) -> List[int]:
        # Strategy depends on what the spike finds for birth_event_counter:
        # a global counter (we can't attribute species without more work) vs a
        # per-species count under species_roster_base. Prefer per-species.
        out = []
        per_species_available = bool(self.anchors.entity_offsets.get("species_birth"))
        for loc in self.game_data.locations_by_trigger("first_breed"):
            if loc.id in already:
                continue
            key = loc.trigger_args.get("species_key")
            if per_species_available:
                count = self.anchors.read_entity(self.scanner, "species_roster_base",
                                                 "species_birth", key)
                if count is None:
                    continue
                baseline = self._species_birth_baseline.setdefault(key, count)
                if count > baseline:
                    logger.info("Detected first breed: %s (count %s)", key, count)
                    out.append(loc.id)
        return out

    # -- milestones ------------------------------------------------------------

    def _poll_milestones(self, already: set) -> List[int]:
        out = []
        for loc in self.game_data.locations_by_trigger("milestone"):
            if loc.id in already:
                continue
            metric = loc.trigger_args.get("metric")
            threshold = loc.trigger_args.get("threshold")
            anchor_name = _MILESTONE_ANCHOR.get(metric)
            if anchor_name is None:
                continue
            val = self.anchors.read(self.scanner, anchor_name)
            if val is not None and val >= threshold:
                logger.info("Detected milestone %s >= %s (=%s)", metric, threshold, val)
                out.append(loc.id)
        return out
