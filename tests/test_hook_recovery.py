"""Game-free tests for leaked-detour self-recovery (signatures.recover_leaked_hook / resolve_hook).

Simulates a hook site left holding OUR jmp-detour after an unclean exit (the console X button / a
crash), and asserts the client restores the original bytes in place so it can re-hook - WITHOUT a
game restart - while refusing to touch a foreign patch it didn't install.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import signatures as sig  # noqa: E402

# "release" has no AOB, so resolve_hook reaches the recovery path without a (slow) full-module scan.
REL = next(h for h in sig.HOOKS if h.name == "release")


class FakeMem:
    """Sparse byte-addressable memory with a module_base; read_bytes zero-fills gaps. Addresses inside
    any (start, end) range in ``unmapped`` raise on read - models a freed VirtualAlloc trampoline page."""
    def __init__(self, module_base: int = 0x140000000):
        self.module_base = module_base
        self.module_size = 0x10000000
        self.attached = True
        self._b: dict = {}
        self.unmapped: list = []   # list of (start, end) byte ranges that raise on read

    def write_bytes(self, addr: int, data: bytes) -> None:
        for i, byte in enumerate(data):
            self._b[addr + i] = byte

    def read_bytes(self, addr: int, size: int) -> bytes:
        for lo, hi in self.unmapped:
            if addr < hi and addr + size > lo:
                raise OSError("unmapped read @0x%X" % addr)
        return bytes(self._b.get(addr + i, 0) for i in range(size))

    def read_qword(self, addr: int) -> int:
        return struct.unpack("<Q", self.read_bytes(addr, 8))[0]


def _install_our_leak(m: FakeMem) -> int:
    """Write our E9 jmp-detour at the release site, pointing at a trampoline that embeds the original
    bytes (kept within rel32 range, like HookManager._alloc_near). Returns the site address."""
    site = m.module_base + REL.rva
    tramp = site + 0x1000
    m.write_bytes(site, b"\xE9" + struct.pack("<i", tramp - (site + 5)))
    m.write_bytes(tramp, b"\x90" * 8 + REL.orig + b"\x90" * 8)  # filler + relocated original + filler
    return site


def test_recovers_our_leaked_detour():
    m = FakeMem()
    site = _install_our_leak(m)
    assert sig._is_leaked_detour(m, site, REL.orig)
    assert sig.recover_leaked_hook(m, "release") is True
    assert m.read_bytes(site, len(REL.orig)) == REL.orig          # restored in place
    assert sig.resolve_hook(m, "release") == (site, REL.orig)     # now resolvable again


def test_intact_site_needs_no_recovery():
    m = FakeMem()
    site = m.module_base + REL.rva
    m.write_bytes(site, REL.orig)
    assert sig._is_leaked_detour(m, site, REL.orig) is False
    assert sig.recover_leaked_hook(m, "release") is False
    assert sig.resolve_hook(m, "release") == (site, REL.orig)


def _install_dead_detour(m: FakeMem) -> int:
    """Write our E9 jmp at the release site pointing at an out-of-module trampoline whose page is now
    UNMAPPED (a prior client PROCESS died and its VirtualAlloc was freed). Returns the site address."""
    site = m.module_base + REL.rva
    tramp = m.module_base - 0x1000000          # below the module -> a freed VirtualAlloc page
    m.write_bytes(site, b"\xE9" + struct.pack("<i", tramp - (site + 5)))
    m.unmapped.append((tramp - 0x100, tramp + 0x400))  # reads there raise
    return site


def test_recovers_dead_detour_from_prior_process():
    """The release_species-on-Auriana case: a leaked jmp whose trampoline was freed by a dead process.
    The trampoline is unreadable, so the leaked-detour check can't verify it - but the jmp targets
    unmapped memory (can't be a live foreign hook), so we restore the authoritative original bytes."""
    m = FakeMem()
    site = _install_dead_detour(m)
    assert sig._is_leaked_detour(m, site, REL.orig) is False  # trampoline unreadable -> not verifiable
    assert sig._is_dead_detour(m, site) is True
    assert sig.recover_leaked_hook(m, "release") is True
    assert m.read_bytes(site, len(REL.orig)) == REL.orig       # restored from the signature
    assert sig.resolve_hook(m, "release") == (site, REL.orig)


def test_jmp_within_module_is_not_a_dead_detour():
    """A jmp that stays inside the module is not the freed-trampoline pattern - never auto-restore it."""
    m = FakeMem()
    site = m.module_base + REL.rva
    target = m.module_base + 0x500000          # within the module
    m.write_bytes(site, b"\xE9" + struct.pack("<i", target - (site + 5)))
    m.unmapped.append((target - 0x10, target + 0x10))  # even if unreadable
    assert sig._is_dead_detour(m, site) is False


def test_foreign_patch_is_not_touched():
    """A jmp whose trampoline does NOT contain our original bytes is someone else's hook - leave it."""
    m = FakeMem()
    site = m.module_base + REL.rva
    tramp = site + 0x1000
    m.write_bytes(site, b"\xE9" + struct.pack("<i", tramp - (site + 5)))
    m.write_bytes(tramp, b"\xCC" * 32)  # not our trampoline (no original bytes embedded)
    assert sig._is_leaked_detour(m, site, REL.orig) is False
    assert sig.recover_leaked_hook(m, "release") is False
    assert m.read_bytes(site, 1) == b"\xE9"            # untouched
    assert sig.resolve_hook(m, "release") is None      # no AOB + not ours -> unresolved
