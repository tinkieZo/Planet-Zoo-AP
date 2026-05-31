"""MemoryEffectApplier — applies received items by writing game memory (A3).

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
    def __init__(self, scanner: MemoryScanner, anchors: AnchorTable):
        self.scanner = scanner
        self.anchors = anchors

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
            logger.warning("[%s] anchor %r unresolved — cannot apply %s",
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
        if not self._ensure_attached():
            return False
        key = item.effect_args.get("species_key")
        if not key:
            logger.error("species_unlock item %s has no species_key in effect_args", item.id)
            return False
        # TODO(spike): confirm the "unlocked" sentinel value (1? bitmask?) and type.
        ok = self.anchors.write_entity(self.scanner, "species_roster_base", "species", key, 1)
        if not ok:
            logger.warning("species_unlock %r unresolved (fill species_roster_base + "
                           "entity_offsets.species[%r])", key, key)
        return ok

    def on_tool_unlock(self, item: "Item") -> bool:
        return self._unsupported(item, "tool_unlock")

    def on_facility_unlock(self, item: "Item") -> bool:
        return self._unsupported(item, "facility_unlock")

    def on_program_unlock(self, item: "Item") -> bool:
        return self._unsupported(item, "program_unlock")

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
                       "(item %s). Stalling — implement during/after the spike.",
                       item.name, effect, item.id)
        return False
