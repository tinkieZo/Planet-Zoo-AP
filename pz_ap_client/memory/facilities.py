"""FacilityGate - memory-enforced, per-facility building-PLACEMENT gate (A3 facilities).

``facility_unlock`` items (Research Centre, Workshop, Trade Centre, Veterinary Surgery)
must be physically enforced - no honor system. Chosen mechanism (user decision 2026-06-02):
block PLACEMENT, so a gated facility literally cannot be built until its item arrives.

This is the permit gate's twin (see permits.py): a conditional-abort detour on the
building-placement commit executor that compares the building's DEFINITION id (a content
id at ``[reg + FACILITY_DEFID_OFF]``) against a client-written blocked set; a match jumps
to the fn's fail-return so the placement aborts with nothing built. Difference from permits:
the gated id is a building DEF id, expected content-STABLE across restarts (a content-def
index, like the research-item id - not the per-session species handle), so the def-id map
is static (verify stability on capture); no research snapshot is needed.

Model mirrors PermitGate: ``set_gated(keys)`` declares the facilities that require an item;
``reconcile(unlocked)`` / ``unlock(key)`` record received items. Blocked set = gated -
unlocked, mapped through FACILITY_DEFID.

*** PENDING the placement executor: FACILITY_RVA is None until the build-placement commit
fn is located in Ghidra (the binding-name leads `AddBuilding` / `CreateBuildingPartSet` /
`CanPlace`; same registration-map technique that found release-to-wild). Until then
``ensure_installed`` is a logged no-op and ``unlock`` returns False, so facility items
STALL (surface + retry) rather than silently "applying" - consistent with the no-honor
contract. Capture each facility's def-id via tools/capture_facility.py once the site lands. ***

Safe: software detour; ``shutdown()`` restores the site; degrades to no-op if unavailable.
"""

from __future__ import annotations

import logging
import struct
from typing import Dict, Iterable, Optional, Set

from .hook import (HookManager, make_facility_gate, FACILITY_SCRATCH_COUNT,
                   FACILITY_SCRATCH_IDS, FACILITY_BLOCKED_MAX)

logger = logging.getLogger("PZClient")

# Placement-commit executor - TO FILL from Ghidra (see docs/A2_RE_HANDOFF.md "FACILITY GATE").
# RVA (module-relative), the >=5 original bytes at the site (relocatable), and the site->fail
# delta (site_addr - fail_return_addr) for the abort jump.
FACILITY_RVA: Optional[int] = None
FACILITY_ORIG: Optional[bytes] = None
FACILITY_FAIL_DELTA: Optional[int] = None

# data.json facility_key -> building DEFINITION id (content-def id). Captured via
# tools/capture_facility.py (place each gated facility once; read the logged def-id).
# Expected content-stable across restarts (verify on capture - if volatile, resolve at
# runtime like the species handle).
FACILITY_DEFID: Dict[str, int] = {
    # "research_centre": 0x....,
    # "workshop":        0x....,
    # "trade_centre":    0x....,
    # "vet_surgery":     0x....,
}


class FacilityGate:
    def __init__(self, scanner, facility_defids: Optional[Dict[str, int]] = None):
        self.scanner = scanner
        self.hm = HookManager(scanner)
        self.defids = dict(FACILITY_DEFID)
        if facility_defids:
            self.defids.update(facility_defids)
        self.gated: Set[str] = set()      # facility_keys that require an item
        self.unlocked: Set[str] = set()   # items received
        self.installed = False
        self.scratch: Optional[int] = None
        self._warned_unmapped: Set[str] = set()
        self._warned_pending = False
        self._warned_overflow = False

    # -- lifecycle -------------------------------------------------------------

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        if not self.gated:
            return False  # nothing routed to the placement gate (trade/vet are permits, research has
            # its own gate) -> silent no-op. The "pending" notice below only matters once a facility item
            # actually needs placement-blocking (the future build-menu / blueprint-unlock feature).
        if FACILITY_RVA is None or FACILITY_ORIG is None or FACILITY_FAIL_DELTA is None:
            if not self._warned_pending:
                self._warned_pending = True
                logger.info("facility gate: placement executor not located yet (FACILITY_RVA "
                            "unset) - facility items will stall until it's filled. See "
                            "docs/A2_RE_HANDOFF.md 'FACILITY GATE'.")
            return False
        base = getattr(self.scanner, "module_base", None)
        if not base:
            return False
        site = base + FACILITY_RVA
        try:
            if self.scanner.read_bytes(site, len(FACILITY_ORIG)) != FACILITY_ORIG:
                logger.warning("facility: site 0x%X bytes mismatch (game patch?); not installing", site)
                return False
            fail = site - FACILITY_FAIL_DELTA
            ok = self.hm.install(
                "facility", site, FACILITY_ORIG,
                lambda r, sc, res: make_facility_gate(r, sc, res, fail, FACILITY_ORIG))
        except Exception as e:
            logger.warning("facility: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("facility")
            logger.info("facility gate installed @0x%X", site)
            self._sync()
        return self.installed

    def shutdown(self) -> None:
        try:
            self.hm.restore("facility")
        except Exception:
            pass
        self.installed = False

    # -- gate state ------------------------------------------------------------

    def set_gated(self, facility_keys: Iterable[str]) -> None:
        """Declare the full set of placement-gated facilities (blocked until unlocked)."""
        self.gated = set(facility_keys)
        self._sync()

    def unlock(self, facility_key: str) -> bool:
        """Record a received facility item and unblock it. Returns True if installed +
        synced (caller treats the item as applied); False if not yet installable (retry)."""
        self.unlocked.add(facility_key)
        if not self.ensure_installed():
            return False
        self._sync()
        return True

    def reconcile(self, unlocked_keys: Iterable[str]) -> bool:
        """Set the held set to exactly ``unlocked_keys`` (full received facility set) and
        resync. Authoritative + idempotent (restart-correct, like PermitGate.reconcile)."""
        new = set(unlocked_keys)
        changed = new != self.unlocked
        self.unlocked = new
        if not self.ensure_installed():
            return False
        if changed:
            self._sync()
        return True

    # -- blocked-set sync ------------------------------------------------------

    def _blocked_ids(self) -> list:
        """Def-ids of the gated-but-unlocked facilities (static map; def-id is content-stable)."""
        out = []
        for key in sorted(self.gated - self.unlocked):
            did = self.defids.get(key)
            if did is None:
                if key not in self._warned_unmapped:
                    self._warned_unmapped.add(key)
                    logger.warning("facility: no def-id for gated facility %r yet - add it to "
                                   "facilities.FACILITY_DEFID (capture via tools/capture_facility.py)", key)
                continue
            out.append(did)
        return out

    def _sync(self) -> None:
        """Write the blocked def-id set into the hook scratch (ids first, count last, so the
        trampoline never reads a count larger than the ids present)."""
        if not self.installed or self.scratch is None:
            return
        ids = self._blocked_ids()
        if len(ids) > FACILITY_BLOCKED_MAX:
            if not self._warned_overflow:
                self._warned_overflow = True
                logger.error("facility: %d blocked exceeds capacity %d - extras unguarded; "
                             "raise FACILITY_BLOCKED_MAX", len(ids), FACILITY_BLOCKED_MAX)
            ids = ids[:FACILITY_BLOCKED_MAX]
        try:
            for i, did in enumerate(ids):
                self.scanner.write_bytes(self.scratch + FACILITY_SCRATCH_IDS + i * 4,
                                         struct.pack("<I", did & 0xFFFFFFFF))
            self.scanner.write_bytes(self.scratch + FACILITY_SCRATCH_COUNT,
                                     struct.pack("<I", len(ids)))
            logger.info("facility: blocked %d facilities %s", len(ids), [hex(d) for d in ids])
        except Exception as e:
            logger.warning("facility: failed to sync blocked set: %s", e)
