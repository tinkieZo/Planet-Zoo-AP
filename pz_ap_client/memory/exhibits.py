"""ExhibitDetector - species-attributed exhibit-animal acquire/breed detection (A3, exhibit analog).

Habitat animals are detected by the BirthDetector (births.py): a detour on the habitat insert site
classifies newborn-vs-bought by a life-stage byte. EXHIBIT animals use a separate add path AND have NO
newborn life-stage (they spawn as young adults, identical to bought ones - user-confirmed), so that trick
is dead for them. The exhibit RE (memory exhibit-detection-re.md) found:

  * the runtime construct/commit fn FUN_140a31f20 (hook site 0xA3202C) - purchase AND birth both funnel
    through it (the "exhibit_insert" signature);
  * the species NAME token lives in the construct params (param_3 = r12): its first qword points at the
    species name string ("GoliathBeetle", "GiantDesertHairyScorpion", ...), which the fn itself interns on
    the common path - so it's populated for both purchase and birth and resolves namespace-free to a key.
    (The numeric species isn't usable in-detour: the committed record's +0x38 isn't written at the hook,
    the records relocate, and entity+0x38 is an index, not a registry id. Live-confirmed 2026-06-23.)
  * acquire-vs-breed is read straight off iVar8 = [param_3+0xc]: -1 for a fresh-construct BIRTH, a real
    preset animal id for an ACQUIRE (market buy / transfer). Live-confirmed 2026-06-23 (purchase 0x10230;
    births all -1). The hook is at the construct ENTRY (FUN_140a31f20) - common to both - because the later
    commit point lives inside the iVar8==-1 branch and so is birth-only.

So per add the detour (make_exhibit_instrument) records {iVar8, species name}; this detector drains them,
resolves the name -> species_key via ResearchReader.species_key_for_name (the normalized engine-token map),
classifies each by iVar8 as acquire or breed, and returns the two key lists. The MemoryTriggerSource feeds
them into the SAME
_bred_species / _acquired_species sets the habitat detector fills, so first_breed / first_acquire fire for
exhibit species through the existing per-species-key path - no firing-side change needed.

Safe: a software detour can't crash the game even if it leaked; shutdown() restores the site. Gated by
EXHIBIT_DETECT_ENABLED (PZAP_NO_EXHIBIT_DETECT=1 kills it) until live-validated, mirroring zoodate's flag.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from .hook import (HookManager, exhibit_event_is_acquire, make_exhibit_instrument,
                   read_exhibit_events)
from .research import ResearchReader

logger = logging.getLogger("PZClient")

# Master gate. The detour ASM + reader are unit-tested and mirror the proven habitat hook, but the live
# install (hook + species-namespace + classifier) is validated in-game. ON by default with an env kill
# switch; resolve_hook fails safe (returns None -> not installed) if the signature goes stale on a patch.
EXHIBIT_DETECT_ENABLED = os.environ.get("PZAP_NO_EXHIBIT_DETECT") != "1"


class ExhibitDetector:
    def __init__(self, scanner, research: Optional[ResearchReader] = None):
        self.scanner = scanner
        # reader holds the normalized engine-token map: species_key_for_name(name) resolves the captured
        # species name token -> data.json species_key. Reuse the shared reader (built once per session).
        self.research = research or ResearchReader(scanner)
        self.hm = HookManager(scanner)
        self.cursor = 0
        self.installed = False
        self.scratch: Optional[int] = None
        self._unknown_logged: set = set()

    def ensure_installed(self) -> bool:
        if not EXHIBIT_DETECT_ENABLED:
            return False
        if self.installed:
            return True
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "exhibit_insert")
        if resolved is None:
            logger.warning("exhibit: insert site unresolved (RVA stale + AOB miss - game patched?); "
                           "not installing")
            return False
        site, orig = resolved
        try:
            ok = self.hm.install("exhibit_insert", site, orig,
                                 lambda r, sc, res: make_exhibit_instrument(r, sc, res, orig))
        except Exception as e:
            logger.warning("exhibit: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("exhibit_insert")
            logger.info("exhibit detector installed @0x%X", site)
        return self.installed

    def poll_events(self) -> "tuple[List[str], List[str]]":
        """Drain new exhibit adds once; return (born_keys, acquired_keys). A purchase leaves the buy
        handler's return address on the captured stack (-> acquire); a birth doesn't (-> breed). The
        cursor advances once per tick so the two are read from one drain (like births.poll_events)."""
        if not self.ensure_installed() or self.scratch is None:
            return [], []
        try:
            self.cursor, events = read_exhibit_events(self.scanner, self.scratch, self.cursor)
        except Exception:
            return [], []
        if not events:
            return [], []
        born: List[str] = []
        acquired: List[str] = []
        for e in events:
            name = e.get("name", "")
            key = self.research.species_key_for_name(name) if name else None
            if key is None:
                self._log_unknown(name)
                continue
            is_acq = exhibit_event_is_acquire(e.get("ivar8", 0))
            logger.info("Detected exhibit %s: %s (token %r)",
                        "ACQUISITION" if is_acq else "BIRTH", key, name)
            (acquired if is_acq else born).append(key)
        return born, acquired

    def _log_unknown(self, name: str) -> None:
        """One-shot diagnostic when a captured exhibit species name won't resolve to a key (token not in
        the engine-token map - add it to data.json, or the params layout shifted on a patch)."""
        if name in self._unknown_logged:
            return
        self._unknown_logged.add(name)
        logger.info("exhibit add: species token %r did not resolve to a species_key (not in the "
                    "engine-token map). Add it to data.json species engine_token, or re-check the "
                    "exhibit params layout if this is garbage.", name)

    def shutdown(self) -> None:
        try:
            self.hm.restore("exhibit_insert")  # direct restore (one hook)
        except Exception:
            pass
        self.installed = False
