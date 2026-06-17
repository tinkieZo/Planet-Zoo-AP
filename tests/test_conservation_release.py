"""Game-free tests for the conservation_release species-attribution scaffold.

The live offset (RELEASE_SPECIES_PATH) is discovered by tools/release_probe.py; these tests cover the
plumbing that consumes it: the gate trampoline now captures the manager, ReleaseDetector follows the
path to a species handle, and the trigger fires cr_<species> for resolved releases - degrading safely
(no false checks) while the path is unset.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import hook, releases  # noqa: E402


def test_release_gate_captures_manager():
    # The live gate trampoline must store rcx (manager) into scratch+RELEASE_GATE_MGR for attribution.
    code = hook.make_release_gate(0x1000, 0x1000, 0x2000, releases.RELEASE_ORIG)
    assert b"\x48\x89\x48\x10" in code, "gate stores rcx -> [rax+0x10] (manager capture)"
    assert hook.RELEASE_GATE_MGR == 0x10


def test_release_capture_probe_trampoline():
    code = hook.make_release_capture(0x1000, 0x1000, 0x2000, releases.RELEASE_ORIG)
    assert b"\xFF\x00" in code                 # inc dword [rax] (count)
    assert b"\x48\x89\x48\x10" in code         # mov [rax+0x10], rcx (manager)
    assert releases.RELEASE_ORIG in code       # relocated original instruction is preserved


class FakeScanner:
    """Sparse memory for the species-path read."""
    def __init__(self, mem):
        self.mem = mem

    def read_bytes(self, addr, n):
        if addr in self.mem and len(self.mem[addr]) >= n:
            return self.mem[addr][:n]
        raise OSError("unmapped")


def _detector_with(path, mem, scratch=0x5000, mgr=0x9000):
    rd = releases.ReleaseDetector(FakeScanner(mem))
    rd.installed = True
    rd.scratch = scratch
    # patch the module-level path for this test
    releases.RELEASE_SPECIES_PATH = path
    return rd


def test_species_handle_unset_path_returns_none():
    rd = _detector_with(None, {})
    try:
        assert rd.last_release_species_handle() is None
    finally:
        releases.RELEASE_SPECIES_PATH = None


def test_species_handle_followed_from_manager():
    scratch, mgr, listing = 0x5000000, 0x9000000, 0xA000000  # plausible heap addrs (> 0x10000)
    mem = {
        scratch + hook.RELEASE_GATE_MGR: struct.pack("<Q", mgr),  # captured manager ptr
        mgr + 0x10: struct.pack("<Q", listing),                   # manager->[+0x10] = listing
        listing + 0x08: struct.pack("<I", 0x30A2),                # listing->[+0x08] = species handle
    }
    rd = _detector_with((0x10, 0x08), mem, scratch, mgr)
    try:
        assert rd.last_release_species_handle() == 0x30A2
    finally:
        releases.RELEASE_SPECIES_PATH = None


def test_trigger_fires_cr_for_resolved_release():
    """MemoryTriggerSource._poll_conservation_release: a new release whose handle resolves to a
    species_key fires that species' cr_ location."""
    import types
    from pz_ap_client.memory.triggers import MemoryTriggerSource

    # minimal stand-ins
    cr_loc = types.SimpleNamespace(id=2500, trigger_args={"species_key": "aardvark"})
    game_data = types.SimpleNamespace(
        locations_by_trigger=lambda t: [cr_loc] if t == "conservation_release" else [])
    src = types.SimpleNamespace(
        game_data=game_data,
        releases=types.SimpleNamespace(count=lambda: 1, last_release_species_handle=lambda: 0x30A2),
        research=types.SimpleNamespace(species_key_for_handle=lambda h: "aardvark" if h == 0x30A2 else None),
        _released_species=set(), _last_release_count=0, _warned_release_attr=False)
    fired = MemoryTriggerSource._poll_conservation_release(src, already=set())
    assert fired == [2500], "cr_aardvark fires once its release is attributed"
    # idempotent: already-checked not re-fired
    assert MemoryTriggerSource._poll_conservation_release(src, already={2500}) == []
