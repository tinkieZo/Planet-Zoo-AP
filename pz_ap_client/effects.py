"""Item effect application (item ID -> "do this in the game").

The client maps each received item's ``effect_type`` to an action. The *logic*
of whether an item should arrive lives in the APWorld; here we only execute.

The backend is pluggable so A1 can run with no game attached:

  * ``ConsoleEffectApplier``  — logs what it *would* do (used by A1 + tests).
  * ``MemoryEffectApplier``   — writes to game memory (added in A3, subclass below).

Dispatch is by ``effect_type``: a backend overrides ``on_<effect_type>`` for the
effects it supports. Unknown / unsupported effects are logged, not fatal.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .data import Item

logger = logging.getLogger("PZClient")


class EffectApplier:
    """Routes an item to its ``on_<effect_type>`` handler."""

    def apply(self, item: "Item") -> bool:
        """Apply one item's effect. Returns True on success.

        Returning a bool (rather than raising) lets the caller decide whether to
        advance the idempotency high-water mark: only advance on success.
        """
        handler = getattr(self, f"on_{item.effect_type}", None)
        if handler is None:
            logger.warning(
                "No handler for effect_type %r (item %s %r) — skipping",
                item.effect_type, item.id, item.name,
            )
            return False
        try:
            return bool(handler(item))
        except Exception:  # never let one bad apply kill the poll loop
            logger.exception("Failed applying item %s (%s)", item.id, item.name)
            return False


class ConsoleEffectApplier(EffectApplier):
    """Dry-run backend: prints intended effects. Used by A1 and unit tests.

    Every handler returns True so the high-water mark advances exactly as it
    would with a real game attached — this lets us exercise the full A3
    idempotency path without the game.
    """

    def _log(self, item: "Item", detail: str) -> bool:
        logger.info("[apply] %-28s -> %s", item.name, detail)
        return True

    def on_tool_unlock(self, item: "Item") -> bool:
        return self._log(item, f"unlock tool {item.effect_args.get('tool_key')!r}")

    def on_facility_unlock(self, item: "Item") -> bool:
        return self._log(item, f"unlock facility {item.effect_args.get('facility_key')!r}")

    def on_species_unlock(self, item: "Item") -> bool:
        return self._log(item, f"grant permit for {item.effect_args.get('species_key')!r}")

    def on_program_unlock(self, item: "Item") -> bool:
        return self._log(item, f"unlock program {item.effect_args.get('program_key')!r}")

    def on_cash(self, item: "Item") -> bool:
        return self._log(item, f"add cash +{item.effect_args.get('amount')}")

    def on_cc(self, item: "Item") -> bool:
        return self._log(item, f"add conservation credits +{item.effect_args.get('amount')}")

    def on_staff_training(self, item: "Item") -> bool:
        return self._log(item, f"staff training +{item.effect_args.get('levels')} level(s)")

    def on_marketing(self, item: "Item") -> bool:
        return self._log(item, f"run marketing campaign {item.effect_args.get('campaign')!r}")

    def on_enrichment_pack(self, item: "Item") -> bool:
        return self._log(item, "grant enrichment item pack")
