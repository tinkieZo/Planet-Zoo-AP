"""BirthDetector - robust, species-attributed birth detection (A3 first_breed).

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
    no hardcoded id map). NOTE: the old `[container+8]` approach was WRONG - that's a
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
        # The most recently captured ZOO (insert fn param_1 / r13). It's a session-stable singleton,
        # so the last value seen at any insert is the live zoo - reused by the release detector to
        # resolve a released animal handle when the release site's own manager candidate doesn't.
        self.last_zoo: Optional[int] = None
        # {animal handle -> species_key} for every animal seen ENTERING the zoo (buy/trade/birth).
        # This is how conservation_release attributes a released species RACE-FREE: a release removes
        # the animal within tens of ms (deferred message), but the client polls every ~1s, so a live
        # roster lookup at release time almost always finds the entity gone. The animal's species was
        # already recorded here when it entered, so the release handler just looks it up. Handle reuse
        # is safe - a reused handle gets a fresh insert that overwrites the entry before any release.
        self.handle_species: dict = {}

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "birth_insert")
        if resolved is None:
            logger.warning("birth: insert site unresolved (RVA stale + AOB miss - game patched?); not installing")
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
        """Build {species_handle -> species_key} for this session. Uses the research reader's
        registry-backed map (covers ALL species via the symbol registry, falling back to the
        captured welfare ids), so birth/acquisition attribution isn't limited to the 11 captured
        species. Handles are per-session but resolved live, so this is restart-correct."""
        return self.research.handle_key_map()

    def sweep_roster(self) -> int:
        """Enumerate the owned-animal roster (habitat + storage) and cache handle->species_key for EVERY
        animal. This is the race-free source for conservation_release attribution: a released animal is
        removed from the roster within ms - too fast for the ~1s poll to resolve it live - but a prior
        sweep already cached it. Covers cases the insert hook misses: loaded/continued saves (pre-existing
        animals) and animals bought + released straight from storage (never entered a habitat). Returns the
        number cached this sweep (0 if no zoo is loaded / the manager isn't resolvable). Cheap for normal
        zoos (one bucket-array read + one species read per animal); called on a throttle from the poll."""
        mgr = self.resolver.resolve_animal_manager()
        if not mgr:
            return 0
        h2k = self._handle_to_key() or {}
        n = 0
        for handle, entity in self.resolver.iter_roster(mgr):
            sh = self.resolver.species_handle(entity)
            key = h2k.get(sh) if sh is not None else None
            if key and self.handle_species.get(handle) != key:
                self.handle_species[handle] = key
                n += 1
        return n

    def poll(self) -> List[str]:
        """Drain new inserts; return species_keys of those that were BIRTHS (newborns).
        (Back-compat: first_breed detection. Use poll_events for births + acquisitions.)"""
        return self.poll_events()[0]

    def poll_events(self) -> "tuple[List[str], List[str]]":
        """Drain new inserts once; return (born_keys, acquired_keys). A newborn (life-stage 0)
        insert is a BIRTH; any other insert of a mapped species is an ACQUISITION (market buy,
        trade, transfer). Same hook, classified per event so first_breed and first_acquire share
        one drain (the cursor must only advance once per tick)."""
        if not self.ensure_installed() or self.scratch is None:
            return [], []
        try:
            self.cursor, events = read_insert_events(self.scanner, self.scratch, self.cursor)
        except Exception:
            return [], []
        if not events:
            return [], []
        handle2key = self._handle_to_key()
        born: List[str] = []
        acquired: List[str] = []
        for e in events:
            z = e.get("r13", 0)
            if z:
                self.last_zoo = z   # remember the live zoo for cross-detector handle resolution
            key, newborn = self._attribute(e, handle2key)
            if key is None:
                continue
            self.handle_species[e.get("handle", 0)] = key  # race-free source for release attribution
            (born if newborn else acquired).append(key)
        return born, acquired

    def _attribute(self, e: dict, handle2key: dict) -> "tuple[Optional[str], bool]":
        """Classify one insert: returns (species_key, is_newborn) for a mapped species, else
        (None, False). Unknown handles / unresolved entities are skipped (logged once)."""
        entity = self.resolver.resolve_entity(e.get("r13", 0), e.get("handle", 0))
        if entity is None:
            return None, False
        newborn = self.resolver.life_stage(entity) == LIFE_STAGE_NEWBORN
        sh = self.resolver.species_handle(entity)
        key = handle2key.get(sh) if sh is not None else None
        if key:
            logger.info("Detected %s: %s (species handle 0x%X)",
                        "BIRTH" if newborn else "ACQUISITION", key, sh)
            return key, newborn
        if sh is not None and sh not in self._unknown_logged:
            self._unknown_logged.add(sh)
            known = {k: "0x%X" % h for h, k in handle2key.items()}
            logger.info("insert of species handle 0x%X not in the research map (unmapped species - "
                        "needs a welfare-id capture - or handle namespaces diverged this session). "
                        "Research-map handles: %s", sh,
                        known or "<EMPTY - research chain didn't resolve / not in a loaded zoo>")
        return None, False

    def shutdown(self) -> None:
        try:
            self.hm.restore("birth_insert")  # direct restore (one hook)
        except Exception:
            pass
        self.installed = False
        self.handle_species.clear()   # handles are per-session; drop the cache on disconnect
