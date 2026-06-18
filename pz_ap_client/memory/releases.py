"""ReleaseDetector - detects animals released to the wild (A3 conservation_release).

There is no restart-stable cumulative "animals released" integer in the game (the A2
spike conclusively ruled out the master-root stats subtree, 2 levels deep - a release
only flips transient per-event flags + decrements population counts). The robust route,
matching births/permits, is a software detour on the RELEASE-SPECIFIC executor:

  ``ReleaseAnimalIntoWild`` native fn @ 0x145D84690 (found via the script-binding
  registration map: name @0x14265C788 -> handler thunk 0x14043EA30 -> 0x145D84690).

TWO detours on this one function:

  * ENTRY gate (``make_release_gate`` @ entry): COUNTS releases at scratch+0 (the
    conservation_release milestone) AND GATES the Conservation Program (program_unlock
    item) - while LOCKED the trampoline aborts the release at entry (``xor eax,eax; ret`` -
    rsp is clean there), so the player physically cannot release until the AP item
    arrives (no honor system). A blocked release isn't counted (nothing happened).

  * SPECIES capture (``make_release_species_capture`` @ entry+0xFF = the call-prep
    0x145D8478F, just before ``thunk_FUN_146f10ad0(*(lVar4+0x48), lVar5)``): records the
    released animal HANDLE (rsi = arg2 = nAnimalID) and ``*(rbp+0x48)`` (the manager/zoo
    the real release resolves the handle through). There is NO early return between the
    entry and this site, so the two fire 1:1 - when the entry count increments, a fresh
    handle is already captured. The client resolves handle -> entity -> species the same
    way births does (AnimalResolver: roster hashmap -> entity -> species handle @+0x50),
    enabling per-species ``cr_<species>`` checks. Removal is deferred (release posts
    AnimalReleasedIntoWildMessage), so the entity is still in the roster right after the
    capture for the next poll to resolve.

The gate defaults LOCKED on install; the client calls ``set_locked(False)`` once the
Conservation Program item is received (reconciled from the full received set each tick).

``count()`` returns releases observed **this session** (resets on reinstall). For the
milestone threshold of 1, detecting any release while attached is sufficient (AP checks
are sticky). Safe: software detours; ``shutdown()`` restores both sites; no-op if a site
isn't available (the species capture is best-effort - its absence only disables
per-species attribution, the count + gate still work).
"""

from __future__ import annotations

import logging
import struct
from typing import Optional

from .hook import (HookManager, make_release_gate, make_release_species_capture,
                   RELEASE_SP_HANDLE, RELEASE_SP_MGR)

logger = logging.getLogger("PZClient")

RELEASE_RVA = 0x5D84690
RELEASE_ORIG = bytes.fromhex("48895c2410")  # mov [rsp+0x10],rbx (5B, rsp-relative entry)
RELEASE_LOCK_OFF = 4   # scratch+4 u32: 1 = conservation program LOCKED (releases aborted)


class ReleaseDetector:
    def __init__(self, scanner):
        self.scanner = scanner
        self.hm = HookManager(scanner)
        self.installed = False
        self.scratch: Optional[int] = None        # entry-gate scratch (count @+0, lock @+4)
        self.sp_scratch: Optional[int] = None     # species-capture scratch (handle @+8, mgr @+0x10)
        self._locked = True   # conservation gated until the program_unlock item arrives

    def ensure_installed(self) -> bool:
        if self.installed:
            return True
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "release")
        if resolved is None:
            logger.warning("release: hook site unresolved (RVA stale + AOB miss - game patched?); not installing")
            return False
        site, orig = resolved
        try:
            ok = self.hm.install("release", site, orig,
                                 lambda r, sc, res: make_release_gate(r, sc, res, orig))
        except Exception as e:
            logger.warning("release: hook install failed: %s", e)
            return False
        self.installed = bool(ok)
        if ok:
            self.scratch = self.hm.scratch("release")
            self._write_lock()  # apply the current gate state
            self._install_species_capture()  # best-effort per-species attribution
            logger.info("release detector+gate installed @0x%X (locked=%s, species_capture=%s)",
                        site, self._locked, self.sp_scratch is not None)
        return self.installed

    def _install_species_capture(self) -> None:
        """Install the second detour that records the released animal's handle + manager candidate.
        Best-effort: a miss only disables per-species cr_ attribution (count + gate keep working)."""
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "release_species")
        if resolved is None:
            logger.info("release: species-capture site unresolved - cr_<species> attribution off "
                        "(release count + conservation gate still active)")
            return
        site, orig = resolved
        try:
            if self.hm.install("release_species", site, orig,
                               lambda r, sc, res: make_release_species_capture(r, sc, res, orig)):
                self.sp_scratch = self.hm.scratch("release_species")
        except Exception as e:
            logger.info("release: species-capture install failed (%s) - cr_ attribution off", e)

    def _write_lock(self) -> None:
        if self.scratch is not None:
            try:
                self.scanner.write_bytes(self.scratch + RELEASE_LOCK_OFF,
                                         struct.pack("<I", 1 if self._locked else 0))
            except Exception as e:
                logger.warning("release: failed to write gate lock: %s", e)

    def set_locked(self, locked: bool) -> None:
        """Gate (True) or open (False) release-to-wild. Client opens it when the
        Conservation Program (program_unlock) item is received."""
        changed = locked != self._locked
        self._locked = locked
        if self.ensure_installed() and changed:
            self._write_lock()
            logger.info("conservation release gate -> %s", "LOCKED" if locked else "OPEN")

    def count(self) -> int:
        """Cumulative releases observed this session (0 if not installable)."""
        if not self.ensure_installed() or self.scratch is None:
            return 0
        try:
            return struct.unpack("<I", self.scanner.read_bytes(self.scratch, 4))[0]
        except Exception:
            return 0

    def last_released_handle(self) -> "Optional[int]":
        """The ENTITY HANDLE (nAnimalID) of the most recently released animal, captured at the
        call-prep site. None if the species capture isn't installed or nothing was captured. The
        caller resolves handle -> species via AnimalResolver (see triggers._attribute_release)."""
        if self.sp_scratch is None:
            return None
        try:
            h = struct.unpack("<Q", self.scanner.read_bytes(self.sp_scratch + RELEASE_SP_HANDLE, 8))[0]
            return h or None
        except Exception:
            return None

    def last_release_manager(self) -> "Optional[int]":
        """``*(rbp+0x48)`` captured at the call-prep site: the object the real release fn resolves
        the handle through (the animal-roster manager or the zoo). A self-contained resolution
        source - the AnimalResolver's power-of-two cap guard rejects it if it's neither."""
        if self.sp_scratch is None:
            return None
        try:
            m = struct.unpack("<Q", self.scanner.read_bytes(self.sp_scratch + RELEASE_SP_MGR, 8))[0]
            return m or None
        except Exception:
            return None

    def shutdown(self) -> None:
        for name in ("release", "release_species"):
            try:
                self.hm.restore(name)
            except Exception:
                pass
        self.installed = False
        self.sp_scratch = None
