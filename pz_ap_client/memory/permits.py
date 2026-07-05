"""PermitGate - memory-enforced, per-species animal-purchase gate (A3 permits).

Planet Zoo has no mod API and the player must not be trusted to honor a soft lock,
so a ``species_unlock`` permit is enforced physically: until the permit arrives, the
species literally cannot be bought. This installs a conditional-abort detour on the
Animal-Exchange purchase fn (``FUN_14A089410`` @ the listing-found site 0x14A0894E5,
see ``make_permit_trampoline``). The trampoline compares each purchased listing's
SPECIES handle ``[rbx+0x10]`` against a client-written blocked set; a match jumps to
the fn's fail-return (0x14A0894B7) so the purchase aborts with no animal spawned.

Validated live (2026-06-01): blocking the common-warthog handle made every warthog
buy-click a no-op while a zebra bought normally - per-species, memory-enforced.

Model: ``set_gated(keys)`` declares the species that REQUIRE a permit (the AP world's
randomized set); ``unlock(key)`` records a received permit. The blocked set written to
the hook = gated - unlocked, intersected with the species we have a listing handle for.
Species whose handle isn't mapped yet are skipped with a one-time warning (the hook is
proven; only the handle map is incomplete - see SPECIES_LISTING_HANDLES).

Safe: a software detour can't crash the game even if leaked; ``shutdown()`` restores
the site. Degrades to a no-op if pymem / the game / the site aren't available.
"""

from __future__ import annotations

import logging
import struct
from typing import Iterable, Optional, Set

from .hook import (HookManager, make_permit_trampoline, PERMIT_SCRATCH_COUNT,
                   PERMIT_SCRATCH_IDS, PERMIT_BLOCKED_MAX)
from .research import ResearchReader

logger = logging.getLogger("PZClient")

PERMIT_RVA = 0xA0894E5
PERMIT_ORIG = bytes.fromhex("0fb68310020000")   # movzx eax,byte[rbx+0x210] (7B, relocatable)
PERMIT_FAIL_DELTA = 0x2E                          # site - fail-return (0x14A0894E5 - 0x14A0894B7)

# NOTE: the species HANDLE used by both the Animal-Exchange listing ([rec+0x10]) and the
# research record ([rec+0x10]) is a per-SESSION runtime index - restart-validation (2026-06-01)
# proved it changes across a game restart (zebra 0x30A2->0x309D). So we do NOT hardcode handles;
# the gate resolves each gated species' CURRENT-session handle from the research map via its
# stable welfare research-item id (ResearchReader.current_handle -> see research.SPECIES_WELFARE_ITEM).


class PermitGate:
    def __init__(self, scanner, research: Optional[ResearchReader] = None):
        self.scanner = scanner
        self.hm = HookManager(scanner)
        # resolve volatile species handles each session from the stable research map
        self.research = research or ResearchReader(scanner)
        self.gated: Set[str] = set()      # species_keys that require a permit
        self.unlocked: Set[str] = set()   # permits received
        self.installed = False
        self.scratch: Optional[int] = None
        self._warned_unmapped: Set[str] = set()
        self._warned_overflow = False

    # -- lifecycle -------------------------------------------------------------

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        if not getattr(self.scanner, "attached", False):
            return False   # not attached yet (e.g. Connected-handler reconcile before the game is up) - quiet
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "permit")
        if resolved is None:
            logger.warning("permit: hook site unresolved (RVA stale + AOB miss - game patched?); not installing")
            return False
        site, orig = resolved
        try:
            fail = site - PERMIT_FAIL_DELTA
            ok = self.hm.install(
                "permit", site, orig,
                lambda r, sc, res: make_permit_trampoline(r, sc, res, fail, orig))
        except Exception as e:
            logger.warning("permit: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("permit")
            logger.info("permit gate installed @0x%X", site)
            self._sync()
        return self.installed

    def shutdown(self) -> None:
        try:
            self.hm.restore("permit")  # direct restore (one hook)
        except Exception:
            pass
        self.installed = False

    # -- permit state ----------------------------------------------------------

    def set_gated(self, species_keys: Iterable[str]) -> None:
        """Declare the full set of permit-gated species (blocked until unlocked)."""
        self.gated = set(species_keys)
        self._sync()

    def unlock(self, species_key: str) -> bool:
        """Record a received permit and unblock the species. Returns True if the
        gate is installed and the blocked set was synced (so the caller can treat
        the item as applied); False if not yet installable (caller should retry)."""
        self.unlocked.add(species_key)
        if not self.ensure_installed():
            return False
        self._sync()
        return True

    def reconcile(self, unlocked_keys: Iterable[str]) -> bool:
        """Set the held-permit set to exactly ``unlocked_keys`` (the full set of
        received species_unlock permits) and resync. Idempotent and authoritative -
        the client calls this each tick from its complete received-items list, so the
        gate is correct across restarts without depending on the cumulative item
        high-water mark. Returns True if installed + synced."""
        new = set(unlocked_keys)
        changed = new != self.unlocked
        self.unlocked = new
        if not self.ensure_installed():
            return False
        if changed:
            self._sync()
        return True

    # -- blocked-set sync ------------------------------------------------------

    def _blocked_handles(self) -> list:
        """Current-session handles of the gated-but-unlocked species, resolved from their
        stable welfare research-item ids via the research map (handles are volatile)."""
        keys = sorted(self.gated - self.unlocked)
        if not keys:
            return []
        snap = self.research._snapshot()  # read the research map once for all gated species
        out = []
        unresolved = []
        for key in keys:
            h = None if snap is None else self.research.current_handle(key, snap)
            if h is None:
                unresolved.append(key)
                continue
            out.append(h)
        # One SUMMARY line when the unresolved set changes, not 70 per-species warnings per connect:
        # a species absent from this zoo's research map is NORMAL for a fresh/empty AP zoo (it appears
        # once the species is in the zoo), and the market gate already keeps un-received species out of
        # the market, so the purchase hook not covering them is defense-in-depth, not a hole.
        if unresolved and set(unresolved) != self._warned_unmapped:
            self._warned_unmapped = set(unresolved)
            logger.info("permit: %d/%d gated species have no research-map handle in this zoo yet - "
                        "purchase-hook blocking covers the other %d; the market gate covers offering "
                        "for all. (Normal for a fresh zoo; resolves as species enter the zoo.)",
                        len(unresolved), len(keys), len(out))
        return out

    def _sync(self) -> None:
        """Write the current blocked handle set into the hook's scratch (ids first,
        count last, so the trampoline never reads a count larger than the ids)."""
        if not self.installed or self.scratch is None:
            return
        handles = self._blocked_handles()
        if len(handles) > PERMIT_BLOCKED_MAX:
            if not self._warned_overflow:
                self._warned_overflow = True
                logger.error("permit: %d blocked species exceeds capacity %d - extras unguarded; "
                             "raise PERMIT_BLOCKED_MAX", len(handles), PERMIT_BLOCKED_MAX)
            handles = handles[:PERMIT_BLOCKED_MAX]
        try:
            for i, h in enumerate(handles):
                self.scanner.write_bytes(self.scratch + PERMIT_SCRATCH_IDS + i * 4,
                                         struct.pack("<I", h & 0xFFFFFFFF))
            self.scanner.write_bytes(self.scratch + PERMIT_SCRATCH_COUNT,
                                     struct.pack("<I", len(handles)))
            logger.info("permit: blocked %d species %s", len(handles), [hex(h) for h in handles])
        except Exception as e:
            logger.warning("permit: failed to sync blocked set: %s", e)
