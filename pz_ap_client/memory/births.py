"""BirthDetector — robust, species-attributed birth detection (A3 first_breed).

Births can't be data-anchored (entities reallocate; counts are volatile). The
robust route, validated in the A2 spike, is a software detour on the stable
add-animal INSERT site that fires when any animal enters a habitat, plus a
per-event classifier:

  * the detour (`make_insert_instrument`) records the inserted entity HANDLE and
    the ZOO pointer (r13) into a scratch ring the client polls;
  * for each insert we resolve handle -> animal entity (`AnimalResolver`) and read
    the life-stage byte: **stage 0 = newborn => a BIRTH** (a market BUY is stage
    1+). This is path-independent (works whether the animal came from gestation or
    quarantine) and was validated live (the only stage-0 animals were births);
  * species comes from the entity's SPECIES HANDLE at `entity+0x50`, reverse-mapped
    through the research map (`ResearchReader.current_handle`, same handle namespace).
    This is reliable per-animal and covers every species automatically (restart-correct,
    no hardcoded id map). NOTE: the old `[container+8]` approach was WRONG — that's a
    HABITAT/holding-container id (many species share one container), not the species.

Safe: a software detour can't crash the game even if it leaked; `shutdown()`
restores the site. Degrades to no-op if pymem/the game/the site aren't available.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .animals import AnimalResolver, LIFE_STAGE_NEWBORN
from .hook import HookManager, make_insert_instrument, read_insert_events
from .research import ResearchReader

logger = logging.getLogger("PZClient")

INSERT_RVA = 0xC82168
INSERT_ORIG = bytes.fromhex("488bbdd8000000")  # mov rdi,[rbp+0xd8] (7B, relocatable)


class BirthDetector:
    def __init__(self, scanner, research: Optional[ResearchReader] = None):
        self.scanner = scanner
        self.resolver = AnimalResolver(scanner)
        # research map gives species HANDLE -> species_key (resolved live each session); used to
        # attribute a newborn's species from entity+0x50. Reuse the shared reader if provided.
        self.research = research or ResearchReader(scanner)
        self.hm = HookManager(scanner)
        self.cursor = 0
        self.installed = False
        self.scratch: Optional[int] = None
        self._unknown_logged: set = set()

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "birth_insert")
        if resolved is None:
            logger.warning("birth: insert site unresolved (RVA stale + AOB miss — game patched?); not installing")
            return False
        site, orig = resolved
        try:
            ok = self.hm.install("birth_insert", site, orig,
                                 lambda r, sc, res: make_insert_instrument(r, sc, res, orig))
        except Exception as e:
            logger.warning("birth: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("birth_insert")
            logger.info("birth detector installed @0x%X", site)
        return self.installed

    def _handle_to_key(self) -> dict:
        """Build {species_handle -> species_key} from the research map (one snapshot). Handles are
        per-session but resolved live, so this is restart-correct + covers every welfare species."""
        snap = self.research._snapshot()
        if snap is None:
            return {}
        out = {}
        for key in self.research.items:  # SPECIES_WELFARE_ITEM keys
            h = self.research.current_handle(key, snap)
            if h is not None:
                out[h] = key
        return out

    def poll(self) -> List[str]:
        """Drain new inserts; return species_keys of those that were BIRTHS (newborns)."""
        if not self.ensure_installed() or self.scratch is None:
            return []
        try:
            self.cursor, events = read_insert_events(self.scanner, self.scratch, self.cursor)
        except Exception:
            return []
        if not events:
            return []
        handle2key = self._handle_to_key()
        out: List[str] = []
        for e in events:
            entity = self.resolver.resolve_entity(e.get("r13", 0), e.get("handle", 0))
            if entity is None:
                continue
            if self.resolver.life_stage(entity) != LIFE_STAGE_NEWBORN:  # buy/grown -> not a birth
                continue
            sh = self.resolver.species_handle(entity)
            key = handle2key.get(sh) if sh is not None else None
            if key:
                logger.info("Detected BIRTH: %s (species handle 0x%X)", key, sh)
                out.append(key)
            elif sh is not None and sh not in self._unknown_logged:
                self._unknown_logged.add(sh)
                logger.info("BIRTH of species handle 0x%X not in the research map (non-welfare species?)", sh)
        return out

    def shutdown(self) -> None:
        try:
            self.hm.restore("birth_insert")  # direct restore (one hook)
        except Exception:
            pass
        self.installed = False
