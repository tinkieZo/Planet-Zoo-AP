"""ReleaseDetector - counts animals released to the wild (A3 conservation_release).

There is no restart-stable cumulative "animals released" integer in the game (the A2
spike conclusively ruled out the master-root stats subtree, 2 levels deep - a release
only flips transient per-event flags + decrements population counts). The robust route,
matching births/permits, is a software detour on the RELEASE-SPECIFIC executor:

  ``ReleaseAnimalIntoWild`` native fn @ 0x145D84690 (found via the script-binding
  registration map: name @0x14265C788 -> handler thunk 0x14043EA30 -> 0x145D84690).

The detour (``make_release_gate``) does double duty on one hook:
  * COUNTS releases at scratch+0 (the conservation_release location), and
  * GATES the Conservation Program (program_unlock item): scratch+4 = lock flag; while
    LOCKED the trampoline aborts the release at entry (``xor eax,eax; ret`` - rsp is clean
    there), so the player physically cannot release until the AP item arrives (no honor
    system). A blocked release isn't counted (nothing happened).
Because it's the release-specific script action, no sell-vs-release disambiguation and no
species attribution are needed. Live-validated (count): idle 0; releasing bumped it 0->1.

The gate defaults LOCKED on install; the client calls ``set_locked(False)`` once the
Conservation Program item is received (reconciled from the full received set each tick).

``count()`` returns releases observed **this session** (resets on reinstall). For the
milestone threshold of 1, detecting any release while attached is sufficient (AP checks
are sticky). Safe: software detour; ``shutdown()`` restores the site; no-op if unavailable.
"""

from __future__ import annotations

import logging
import struct
from typing import Optional

from .hook import HookManager, make_release_gate

logger = logging.getLogger("PZClient")

RELEASE_RVA = 0x5D84690
RELEASE_ORIG = bytes.fromhex("48895c2410")  # mov [rsp+0x10],rbx (5B, rsp-relative entry)
RELEASE_LOCK_OFF = 4   # scratch+4 u32: 1 = conservation program LOCKED (releases aborted)


class ReleaseDetector:
    def __init__(self, scanner):
        self.scanner = scanner
        self.hm = HookManager(scanner)
        self.installed = False
        self.scratch: Optional[int] = None
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
            logger.info("release detector+gate installed @0x%X (locked=%s)", site, self._locked)
        return self.installed

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

    def shutdown(self) -> None:
        try:
            self.hm.restore("release")  # direct restore (one hook)
        except Exception:
            pass
        self.installed = False
