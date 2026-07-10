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

EXHIBIT releases use a SEPARATE native action (``FUN_146048940`` @rva 0x6048940; no script binding -
it resolves the released exhibit animal and posts ``ExhibitAnimalReleasedMessage``), so the habitat
hook never sees them - which is why exhibit releases previously did nothing. ``_install_exhibit_gate``
adds the SAME gate+counter (``make_release_gate``) on that entry; its count folds into ``count()`` and
its lock tracks the Conservation Program, so exhibit releases register toward conservation_release and
are gated identically. Per-species ``cr_`` for exhibit splits by placement:

  * PLACED releases decrement the manager's +0x318 {species_handle -> count} census (deferred
    message) - the census diff attributes them (live-proven).
  * STORAGE releases touch neither the census nor any structure directly on the manager - a THIRD
    detour (``make_exhibit_release_capture`` @ FUN_146048940+0x152 = rva 0x6048A92, the storage
    branch's ``mov r15,[r14+0xf8]``) captures the released ANIMAL ID (ebx = msg+0x18) plus the
    manager (r14) and the def-map holder H = *(mgr+0xF8) (r15 after the relocated original). The
    client resolves id -> species via the {animal_id -> def} map at *(H+0x358)+0x108, cached per
    tick BEFORE the release (race-free, like the habitat roster sweep); the +0x2A0 owned-id-set
    diff on H is the hookless secondary. (The 2026-07-06 id-roster path read +0x2A0/+0x358 off the
    manager itself, missing the +0xF8 rebind the release fn performs - dead live 2026-07-08.)

``habitat_count`` / ``exhibit_count`` split the two executors so each uses its own attribution
(``triggers._poll_exhibit_release``). Found via Ghidra (FindStrRefs on ExhibitAnimalReleasedMessage +
the FUN_146048940 decomp/disasm - tools/_decomp/exhibit_release_species_notes.md); see
[[exhibit-release-RE]].

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

from .hook import (HookManager, make_exhibit_release_capture, make_release_gate,
                   make_release_species_capture, read_exhibit_release_events,
                   EXR_HOLDER, EXR_MGR, RELEASE_SP_HANDLE, RELEASE_SP_MGR)

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
        self.exhibit_scratch: Optional[int] = None  # EXHIBIT release gate scratch (count @+0, lock @+4)
        self.exr_scratch: Optional[int] = None    # EXHIBIT storage-release capture (id ring/mgr/holder)
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
            self._install_exhibit_gate()      # EXHIBIT releases use a separate native action
            logger.info("release detector+gate installed @0x%X (locked=%s, species_capture=%s, exhibit_gate=%s)",
                        site, self._locked, self.sp_scratch is not None, self.exhibit_scratch is not None)
        return self.installed

    def _install_exhibit_gate(self) -> None:
        """Install the gate+counter on the EXHIBIT release action (FUN_146048940), the exhibit analog of
        the habitat release executor. Exhibit animals release via a SEPARATE native path (no script binding;
        it posts ExhibitAnimalReleasedMessage), so the habitat hook never sees them - this is why exhibit
        releases previously did nothing. Counting here makes them register toward conservation_release and
        respects the Conservation Program lock. Best-effort: a miss only disables exhibit-release detection
        (habitat unaffected). Reuses make_release_gate (count@+0, lock@+4; the relocated push-prologue is
        position-independent, and the gate's `xor eax,eax; ret` abort is valid at this clean entry too)."""
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "exhibit_release")
        if resolved is None:
            logger.info("release: exhibit-release site unresolved - exhibit releases won't count "
                        "(habitat release detection unaffected)")
            return
        site, orig = resolved
        try:
            if self.hm.install("exhibit_release", site, orig,
                               lambda r, sc, res: make_release_gate(r, sc, res, orig)):
                self.exhibit_scratch = self.hm.scratch("exhibit_release")
                self._write_lock()   # apply the current lock to the exhibit gate too
                logger.info("exhibit-release gate installed @0x%X", site)
        except Exception as e:
            logger.info("release: exhibit-release install failed (%s) - exhibit releases won't count", e)
        self._install_exhibit_species_capture()   # storage-release id capture (best-effort)

    def _install_exhibit_species_capture(self) -> None:
        """Install the capture detour inside the exhibit release action's STORAGE branch
        (FUN_146048940+0x152): records the released animal id + the def-map holder so a storage
        release - which the placed census never reflects - attributes to a species. Best-effort:
        a miss only degrades storage releases to unattributed (count + gate + placed census keep
        working)."""
        from .signatures import resolve_hook
        resolved = resolve_hook(self.scanner, "exhibit_release_species")
        if resolved is None:
            logger.info("release: exhibit storage-release capture site unresolved - storage exhibit "
                        "releases won't attribute a species (count + placed-census attribution unaffected)")
            return
        site, orig = resolved
        try:
            if self.hm.install("exhibit_release_species", site, orig,
                               lambda r, sc, res: make_exhibit_release_capture(r, sc, res, orig)):
                self.exr_scratch = self.hm.scratch("exhibit_release_species")
                logger.info("exhibit storage-release capture installed @0x%X", site)
        except Exception as e:
            logger.info("release: exhibit storage-release capture install failed (%s) - storage "
                        "exhibit releases won't attribute a species", e)

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
        for sc in (self.scratch, self.exhibit_scratch):   # gate BOTH habitat + exhibit release paths
            if sc is None:
                continue
            try:
                self.scanner.write_bytes(sc + RELEASE_LOCK_OFF, struct.pack("<I", 1 if self._locked else 0))
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
        """Cumulative releases observed this session - habitat + exhibit (0 if not installable)."""
        if not self.ensure_installed():
            return 0
        total = 0
        for sc in (self.scratch, self.exhibit_scratch):
            if sc is None:
                continue
            try:
                total += struct.unpack("<I", self.scanner.read_bytes(sc, 4))[0]
            except Exception:
                pass
        return total

    def _scratch_count(self, sc: "Optional[int]") -> int:
        if sc is None:
            return 0
        try:
            return struct.unpack("<I", self.scanner.read_bytes(sc, 4))[0]
        except Exception:
            return 0

    def habitat_count(self) -> int:
        """Releases via the HABITAT executor (ReleaseAnimalIntoWild) this session. Attributed per-species
        by handle (release_species capture). Separated from exhibit releases so each path uses its own
        attribution (handle-based vs census-diff) without one mis-firing on the other's count."""
        return self._scratch_count(self.scratch) if self.ensure_installed() else 0

    def exhibit_count(self) -> int:
        """Releases via the EXHIBIT action (FUN_146048940) this session. Attributed per-species by
        the storage-branch id capture (stored animals) / the census diff (placed animals)."""
        return self._scratch_count(self.exhibit_scratch) if self.ensure_installed() else 0

    def exhibit_release_events(self, cursor: int) -> "tuple[int, list]":
        """Drain released exhibit-animal IDS captured at the storage branch since ``cursor``.
        Returns ``(new_cursor, ids)``; ``(cursor, [])`` if the capture isn't installed. Fires only
        for STORAGE releases (the placed path branches off before the capture site) - resolve each
        id to a species via the def-map cache (triggers)."""
        if self.exr_scratch is None:
            return cursor, []
        try:
            return read_exhibit_release_events(self.scanner, self.exr_scratch, cursor)
        except Exception:
            return cursor, []

    def _exr_qword(self, off: int) -> "Optional[int]":
        if self.exr_scratch is None:
            return None
        try:
            v = struct.unpack("<Q", self.scanner.read_bytes(self.exr_scratch + off, 8))[0]
            return v or None
        except Exception:
            return None

    def exhibit_capture_mgr(self) -> "Optional[int]":
        """r14 recorded at the last storage-release capture: the exhibit manager object (the
        pre-rebind base the release fn resolves everything through). None until a capture fires."""
        return self._exr_qword(EXR_MGR)

    def exhibit_capture_holder(self) -> "Optional[int]":
        """r15 recorded at the last storage-release capture: H = *(mgr+0xF8), the holder of the
        +0x2A0 owned-id set and the {animal_id -> def} map. Ground truth for the def-map base -
        preferred over the park-chain +0xF8 deref when available. None until a capture fires."""
        return self._exr_qword(EXR_HOLDER)

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
        for name in ("release", "release_species", "exhibit_release", "exhibit_release_species"):
            try:
                self.hm.restore(name)
            except Exception:
                pass
        self.installed = False
        self.sp_scratch = None
        self.exhibit_scratch = None
        self.exr_scratch = None
