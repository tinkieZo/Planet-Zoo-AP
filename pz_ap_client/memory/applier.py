"""MemoryEffectApplier - applies received items by writing game memory (A3).

Drop-in replacement for ConsoleEffectApplier: the client calls ``apply(item)``
and the base ``EffectApplier`` routes to ``on_<effect_type>``.

Apply-success contract (important for idempotency): a handler returns
``True`` only when the effect was actually applied. The client advances its
high-water mark on True and **stops/retries on False**. So:

  * cumulative effects (cash, cc) read-modify-write and return True on success;
  * unlocks flip a flag/byte and return True;
  * if an anchor isn't filled in yet (spike incomplete) the handler returns
    False, which intentionally stalls that item with a loud log rather than
    silently skipping a progression unlock the player needs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..effects import EffectApplier
from .anchors import AnchorTable
from .scanner import MemoryScanner

if TYPE_CHECKING:
    from ..data import Item

logger = logging.getLogger("PZClient")


class MemoryEffectApplier(EffectApplier):
    # facility items whose enforcement is the ResearchGate (research-start block by category),
    # not the placement FacilityGate. The client reconciles the ResearchGate each tick from the
    # full received set, so on_facility_unlock just acknowledges these (like on_program_unlock).
    RESEARCH_FACILITIES = frozenset({"research_centre", "workshop"})

    def __init__(self, scanner: MemoryScanner, anchors: AnchorTable, permit_gate=None,
                 release_gate=None, facility_gate=None, research_gate=None, reward_granter=None):
        self.scanner = scanner
        self.anchors = anchors
        self.permit_gate = permit_gate      # PermitGate; enforces species_unlock permits
        self.release_gate = release_gate    # ReleaseDetector; enforces the conservation program
        self.facility_gate = facility_gate  # FacilityGate; enforces facility_unlock placement
        self.research_gate = research_gate  # ResearchGate; enforces research_centre/workshop
        self.reward_granter = reward_granter  # RewardGranter; flips decoupled research rewards

    def _ensure_attached(self) -> bool:
        if self.scanner.attached:
            return True
        return self.scanner.attach()

    # -- cumulative scalars (read-modify-write) --------------------------------

    def _add_scalar(self, anchor_name: str, amount, item: "Item") -> bool:
        if not self._ensure_attached():
            return False
        current = self.anchors.read(self.scanner, anchor_name)
        if current is None:
            logger.warning("[%s] anchor %r unresolved - cannot apply %s",
                           item.effect_type, anchor_name, item.name)
            return False
        ok = self.anchors.write(self.scanner, anchor_name, current + amount)
        if ok:
            logger.info("[apply] %s: %s %s -> %s", item.name, anchor_name, current, current + amount)
        return ok

    def on_cash(self, item: "Item") -> bool:
        return self._add_scalar("cash", item.effect_args.get("amount", 0), item)

    def on_cc(self, item: "Item") -> bool:
        return self._add_scalar("conservation_credits", item.effect_args.get("amount", 0), item)

    # -- unlocks (flip a flag) -------------------------------------------------

    def on_species_unlock(self, item: "Item") -> bool:
        """Grant a species permit. Enforcement is the PermitGate purchase-block
        detour (memory-enforced; see permits.py): unlocking removes the species from
        the hook's blocked set so it becomes buyable. The client also reconciles the
        gate from the full received-permit set each tick (restart-correct)."""
        if not self._ensure_attached():
            return False
        key = item.effect_args.get("species_key")
        if not key:
            logger.error("species_unlock item %s has no species_key in effect_args", item.id)
            return False
        if self.permit_gate is None:
            logger.warning("species_unlock %r: no PermitGate wired - cannot enforce", key)
            return False
        ok = self.permit_gate.unlock(key)
        if ok:
            logger.info("[apply] %s: permit granted for %s (unblocked)", item.name, key)
        else:
            logger.warning("species_unlock %r: gate not installable yet - will retry", key)
        return ok

    def on_tool_unlock(self, item: "Item") -> bool:
        """Grant a tool. The water_tools item gates aquatic species (nile_hippo,
        saltwater_croc); its real in-game tool button lives in the data-driven Cobra UI
        we can't reach, so it's enforced as a species-PURCHASE block (the PermitGate),
        substituting for the unbuildable tool gate. The client reconciles the purchase
        gate from the full received set each tick + right after applying, so this just
        ensures the hook is installed and acknowledges (like a research facility). A
        non-water tool with no species gating it simply has no purchase effect."""
        if not self._ensure_attached():
            return False
        key = item.effect_args.get("tool_key")
        if not key:
            logger.error("tool_unlock item %s has no tool_key in effect_args", item.id)
            return False
        if self.permit_gate is None:
            logger.warning("tool_unlock %r: no PermitGate wired - cannot enforce (proxy)", key)
            return False
        if not self.permit_gate.ensure_installed():
            logger.warning("tool_unlock %r: purchase gate not installable yet - will retry", key)
            return False
        logger.info("[apply] %s: tool %s granted (water-gated species unblocked by reconcile)",
                    item.name, key)
        return True

    def on_facility_unlock(self, item: "Item") -> bool:
        """Grant a facility. Enforced by the FacilityGate placement-block detour
        (memory-enforced; see facilities.py): unlocking removes the facility's def-id
        from the hook's blocked set so it becomes placeable. The client also reconciles
        the gate from the full received-facility set each tick (restart-correct)."""
        if not self._ensure_attached():
            return False
        key = item.effect_args.get("facility_key")
        if not key:
            logger.error("facility_unlock item %s has no facility_key in effect_args", item.id)
            return False
        if key in self.RESEARCH_FACILITIES:
            # Enforced by the ResearchGate (research-start block); the client reconciles it from
            # the full received set each tick. Acknowledge so the high-water mark advances.
            if self.research_gate is None:
                logger.warning("facility_unlock %r: no ResearchGate wired - cannot enforce", key)
                return False
            logger.info("[apply] %s: research facility %s granted (research unblocked by reconcile)",
                        item.name, key)
            return True
        if self.facility_gate is None:
            logger.warning("facility_unlock %r: no FacilityGate wired - cannot enforce", key)
            return False
        ok = self.facility_gate.unlock(key)
        if ok:
            logger.info("[apply] %s: facility granted for %s (placement unblocked)", item.name, key)
        else:
            logger.warning("facility_unlock %r: gate not installable yet (placement executor "
                           "pending) - will retry", key)
        return ok

    def on_program_unlock(self, item: "Item") -> bool:
        """Unlock a program. The only program is Conservation (release-to-wild),
        enforced by the ReleaseDetector's release-gate detour (memory-enforced; see
        releases.py): while gated the release executor aborts at entry so the player
        physically cannot release. Granting opens the gate. The client also reconciles
        the gate from the full received set each tick (restart-correct)."""
        if not self._ensure_attached():
            return False
        key = item.effect_args.get("program_key")
        if key != "conservation":
            logger.warning("program_unlock %r: only 'conservation' is implemented", key)
            return False
        if self.release_gate is None:
            logger.warning("program_unlock %r: no ReleaseDetector wired - cannot enforce", key)
            return False
        self.release_gate.set_locked(False)
        logger.info("[apply] %s: conservation program unlocked (releases enabled)", item.name)
        return True

    def on_research_reward(self, item: "Item") -> bool:
        """Grant a decoupled research reward (enrichment item, shop, barrier, theme set...) by
        flipping its content's unlocked byte in the unlockables map (see rewards.py). The reward
        is content the AP item pool carries instead of the in-game research giving it directly."""
        if not self._ensure_attached():
            return False
        content = item.effect_args.get("content")
        if not content:
            logger.error("research_reward item %s has no content in effect_args", item.id)
            return False
        if self.reward_granter is None:
            logger.warning("research_reward %r: no RewardGranter wired - cannot apply", content)
            return False
        from .rewards import is_mechanic_content
        if is_mechanic_content(content):
            # MECHANIC content (shops/themes/blueprints/transport/staff/power) unlocks via its re-pointed gate,
            # reconciled each tick (client._reconcile_mechanic_content -> reward_granter.reconcile_mechanic),
            # like barriers/facilities. Acknowledge so the high-water mark advances; the tick does the write.
            return True
        return self.reward_granter.grant(content)  # ANIMAL content -> rs+0x148 flag flip

    def on_progressive_research_reward(self, item: "Item") -> bool:
        """Grant the next tier of a progressive reward family (supplement/education/breeding/
        exhibit enrichment) - flips the lowest still-locked content of that record type."""
        if not self._ensure_attached():
            return False
        family = item.effect_args.get("family")
        if not family:
            logger.error("progressive_research_reward item %s has no family in effect_args", item.id)
            return False
        if self.reward_granter is None:
            logger.warning("progressive_research_reward %r: no RewardGranter wired - cannot apply", family)
            return False
        if family == "barrier":
            # Barriers are habitat-boundary build content gated by mechanic research, reconciled each tick
            # from the received-level COUNT (client._reconcile_barriers -> reward_granter.reconcile_barriers):
            # level N makes grade<=N barriers buildable via status-write (buildable>=3, location==4, so no
            # false check). Acknowledge here so the high-water mark advances; the tick does the work
            # (restart-correct), like on_program_unlock.
            return True
        return self.reward_granter.grant_progressive(family)

    def on_staff_training(self, item: "Item") -> bool:
        return self._unsupported(item, "staff_training")

    def on_marketing(self, item: "Item") -> bool:
        return self._unsupported(item, "marketing")

    def on_enrichment_pack(self, item: "Item") -> bool:
        return self._unsupported(item, "enrichment_pack")

    def _unsupported(self, item: "Item", effect: str) -> bool:
        # Not yet wired to memory. Return False so it surfaces and retries rather
        # than silently advancing past a (possibly progression) item.
        logger.warning("[apply] %s effect %r not implemented in MemoryEffectApplier yet "
                       "(item %s). Stalling - implement during/after the spike.",
                       item.name, effect, item.id)
        return False
