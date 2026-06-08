"""PresenceGate - clean NATIVE facility-presence gate (research_centre, workshop).

The game keeps a per-facility "is one present" cache (a flag array at manager+0x390) that its build
menu / research UI read to enable or grey the facility's controls. A single shared routine rebuilds
that cache (clear-then-fill); the fill write `mov byte [rcx+rax],1` @ 0x149E94863 marks each present
facility, with rbp = the component manager being filled. We detour that fill (see
``make_presence_gate``): for a manager in the client-written gated set the flag is filled with 0, so
the facility reads as ABSENT and the game greys its controls NATIVELY ("you need a research centre" /
mechanic research disabled) even with the building placed - exactly the game's own not-built UX.

Why this beats poking the data directly: the gate rides the game's OWN cache-fill, so it re-applies on
every rebuild and never desyncs the facility (poll-zeroing the flag did, and could not be cleanly
reversed). LIVE-VALIDATED both ways for research_centre and workshop, and per-manager selective
(gating one never touches the others). See memory/clean-gate-investigations.md.

This is the player-facing UX layer; the ResearchGate (research-START hook) remains the hard
enforcement (no progress / completion / in-progress leak). Both are driven from the same received
research-facility set each tick, so they stay consistent and restart-correct.

Lock/unlock semantics: the flag only changes when the game refills the cache (a facility build/
demolish). So a gated facility built while the gate is active is greyed at build time; a facility
already present when the gate first applies is force-zeroed once for immediate enforcement (the hook
then holds it). On unlock the manager drops from the set and the next refill writes 1 - i.e. the
player (re)builds the facility to activate it, the natural AP "you unlocked it, now build it" moment
(a direct flag write to 1 does NOT cleanly re-enable - the button needs a fresh fill)."""

from __future__ import annotations

import logging
import struct
from typing import List, Optional

from .hook import (HookManager, make_presence_gate, PRESENCE_GATED_COUNT,
                   PRESENCE_GATED_MGRS, PRESENCE_GATED_MAX)

logger = logging.getLogger("PZClient")

# The fill write to detour, and the exact bytes there (store + the following lea, relocated).
PRESENCE_RVA = 0x9E94863
PRESENCE_ORIG = bytes.fromhex("c6040101488d842480000000")

# zoo = *( *(base+0x29446A0) + 0x38 ); each facility's component manager hangs off it at:
ZOO_ROOT = 0x29446A0
ZOO_OFF = 0x38
FACILITY_PRESENCE_MGR_OFF = {
    "research_centre": 0x150,   # mgr e.g. 0xF792910 - greys the research button
    "workshop": 0x168,          # mgr e.g. 0xF793600 - disables mechanic research
}
MGR_COUNT_OFF = 0x2D4           # u32 slot count within a presence manager
MGR_FLAGS_OFF = 0x390           # pointer to the per-slot presence flag bytes


class PresenceGate:
    """Native greyed-button gate for the research-enabling facilities. Drop-in alongside the
    ResearchGate: same ``set_gated`` / ``reconcile`` / ``shutdown`` shape and a ``gated_facilities``
    attribute, driven from the received-facility set. Degrades to a no-op if the game/site aren't
    available; ``shutdown()`` restores the detour."""

    def __init__(self, scanner):
        self.scanner = scanner
        self.hm = HookManager(scanner)
        self.installed = False
        self.scratch: Optional[int] = None
        self.gated_facilities: set = set(FACILITY_PRESENCE_MGR_OFF)
        self._last_gated: Optional[list] = None
        self._warned_pending = False

    def set_gated(self, facility_keys) -> None:
        """Declare which research facilities have an AP item (only these are ever gated)."""
        self.gated_facilities = set(facility_keys) & set(FACILITY_PRESENCE_MGR_OFF)
        self._last_gated = None  # force a resync

    # -- structure resolution (heap pointers; re-resolved each session) --------

    def _manager(self, facility: str) -> Optional[int]:
        base = getattr(self.scanner, "module_base", None)
        off = FACILITY_PRESENCE_MGR_OFF.get(facility)
        if not base or off is None:
            return None
        root = self.scanner.read_qword(base + ZOO_ROOT)
        zoo = self.scanner.read_qword(root + ZOO_OFF) if root else None
        return self.scanner.read_qword(zoo + off) if zoo else None

    # -- lifecycle -------------------------------------------------------------

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        base = getattr(self.scanner, "module_base", None)
        if not base:
            return False
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "presence")
        if resolved is None:
            logger.warning("presence gate: fill site unresolved (RVA stale + AOB miss - game patched?); not installing")
            return False
        site, orig = resolved
        try:
            ok = self.hm.install("presence", site, orig,
                                 lambda r, sc, res: make_presence_gate(r, sc, res, orig))
        except Exception as e:
            logger.warning("presence gate: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("presence")
            logger.info("presence gate installed @0x%X", site)
        return self.installed

    def shutdown(self) -> None:
        try:
            self.hm.restore("presence")
        except Exception:
            pass
        self.installed = False

    # -- gated-set sync --------------------------------------------------------

    def _force_zero(self, mgr: int) -> None:
        """Zero a gated facility's CURRENT presence flags so an already-built facility is locked
        immediately (the hook then holds it 0 across rebuilds). Lock-side only - validated to grey
        the control without the desync that plagued continuous poll-zeroing."""
        try:
            arr = self.scanner.read_qword(mgr + MGR_FLAGS_OFF)
            cnt = struct.unpack("<I", self.scanner.read_bytes(mgr + MGR_COUNT_OFF, 4))[0]
            if arr:
                self.scanner.write_bytes(arr, b"\x00" * min(max(cnt, 1), 8))
        except Exception:
            pass

    def _write_gated(self, mgrs: List[int]) -> None:
        if self.scratch is None:
            return
        mgrs = mgrs[:PRESENCE_GATED_MAX]
        try:
            for i, p in enumerate(mgrs):
                self.scanner.write_bytes(self.scratch + PRESENCE_GATED_MGRS + i * 8, struct.pack("<Q", p))
            self.scanner.write_bytes(self.scratch + PRESENCE_GATED_COUNT, struct.pack("<I", len(mgrs)))
        except Exception as e:
            logger.warning("presence gate: failed to write gated set: %s", e)

    def reconcile(self, unlocked_facilities) -> bool:
        """Authoritative gate sync: grey the controls of research facilities whose item has not been
        received. Idempotent; only re-syncs on change. Returns True if installed."""
        if not self.ensure_installed():
            if not self._warned_pending:
                self._warned_pending = True
                logger.info("presence gate: fill hook not installable yet - will retry")
            return False
        unlocked = set(unlocked_facilities)
        gated = sorted(f for f in self.gated_facilities if f not in unlocked)
        if gated == self._last_gated:
            return True
        mgrs = [m for f in gated if (m := self._manager(f))]
        self._write_gated(mgrs)
        for m in mgrs:
            self._force_zero(m)   # immediate lock for an already-built facility
        logger.info("presence gate: gating %s (managers %s)", gated or "none",
                    [hex(m) for m in mgrs])
        self._last_gated = list(gated)
        return True
