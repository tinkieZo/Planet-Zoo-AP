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
from pz_ap_client.memory import hook as hookmod  # noqa: E402

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


def test_shared_arena_packs_many_hooks_in_one_block():
    """The fix for species_capture=False (near-memory exhaustion): all trampolines are bump-allocated from
    ONE shared near-module block, so N hooks need ONE free hole, not N. The block is freed only once every
    slot is released (ref-counted across all HookManagers)."""
    hookmod._arenas.clear(); hookmod._arena_live = 0
    state = {"alloc_calls": 0, "freed": []}

    def fake_alloc(_handle, target, _size=0x1000):
        state["alloc_calls"] += 1
        return target & ~0xFFFF                                # a 64KB-aligned block AT the target (as _alloc_near does: near it)

    class FakeK32:
        def VirtualFreeEx(self, _h, addr, _sz, _fl):
            state["freed"].append(int(addr.value)); return 1

    orig_alloc, orig_k32 = hookmod._alloc_near, hookmod._k32
    hookmod._alloc_near, hookmod._k32 = fake_alloc, FakeK32()
    try:
        site = 0x140000000
        slots = [hookmod._arena_alloc(0, site + i * 0x1000) for i in range(4)]  # 4 nearby hook sites
        assert state["alloc_calls"] == 1                       # ONE block served all four
        base = slots[0]
        step = hookmod._ARENA_SLOT
        assert slots == [base, base + step, base + 2 * step, base + 3 * step]  # bump-allocated slots
        assert hookmod._arena_live == 4
        for _ in range(3):
            hookmod._arena_release(0)
        assert state["freed"] == []                            # not freed while slots remain
        hookmod._arena_release(0)
        assert state["freed"] == [base] and hookmod._arena_live == 0 and hookmod._arenas == []
    finally:
        hookmod._alloc_near, hookmod._k32 = orig_alloc, orig_k32


def test_shared_arena_new_block_when_out_of_reach():
    """A hook site too far (> rel32) for the current block forces a second block, so reachability holds."""
    hookmod._arenas.clear(); hookmod._arena_live = 0
    state = {"n": 0}

    def fake_alloc(_handle, target, _size=0x1000):
        state["n"] += 1
        return target & ~0xFFFF                                # a block at the (aligned) target

    orig_alloc = hookmod._alloc_near
    hookmod._alloc_near = fake_alloc
    try:
        a = hookmod._arena_alloc(0, 0x140000000)
        b = hookmod._arena_alloc(0, 0x140000000 + 0x100)       # near -> same block
        assert state["n"] == 1 and b == a + hookmod._ARENA_SLOT
        hookmod._arena_alloc(0, 0x140000000 + 0x100000000)     # +4GB -> out of rel32 -> new block
        assert state["n"] == 2
    finally:
        hookmod._alloc_near = orig_alloc
        hookmod._arenas.clear(); hookmod._arena_live = 0


def test_installed_hook_is_ok_not_leaked():
    """A site holding OUR detour installed THIS session must classify OK, not 'leaked' - it's byte-identical
    to a leaked prior-crash detour, so the preflight needs the installed-hooks hint to tell them apart (the
    false-positive the user saw: every freshly-installed hook reported 'leaked (prior unclean exit)')."""
    m = FakeMem()
    _install_our_leak(m)
    assert sig._classify_hook(m, REL).status == "leaked"                      # no hint -> looks like a leak
    r = sig._classify_hook(m, REL, installed_hooks={"release"})              # client installed it this session
    assert r.status == "ok" and "active" in r.detail
    # and the whole-inventory entry point threads the hint through:
    hooks = {c.name: c for c in sig.check_hooks(m, installed_hooks={"release"})}
    assert hooks["release"].status == "ok"


def test_active_hooks_registry_tracks_install_and_restore():
    """HookManager records installed names session-wide (removed on restore) so the preflight can query
    what's ours across every manager without enumerating them."""
    hookmod.HookManager._active_names.clear()
    try:
        hookmod.HookManager._active_names.add("release")   # simulate a successful install
        assert hookmod.HookManager.active_hooks() == {"release"}
        assert hookmod.HookManager.active_hooks() is not hookmod.HookManager._active_names  # a copy
        hookmod.HookManager._active_names.discard("release")  # simulate restore
        assert hookmod.HookManager.active_hooks() == set()
    finally:
        hookmod.HookManager._active_names.clear()


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
