"""Game-free tests for robust research-system resolution (research.ResearchReader._research_system).

The master-root pointer chains are layout-fragile and miss in some saves, so resolution must fall back
to a heap scan for the system's VTABLE (validating the items map at +0xF8), prefer a chain when one is
valid, and cache the result to avoid re-scanning every snapshot.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.research import (  # noqa: E402
    ResearchReader, RESEARCH_CHAIN, ITEMS_MAP_OFF,
)


class FakeScanner:
    """Sparse memory + a stub heap scan; read_bytes zero-fills gaps so unplanted reads look empty."""
    def __init__(self):
        self.module_base = 0x140000000
        self.attached = True
        self._b: dict = {}
        self.vtable_hits: list = []
        self.scan_calls = 0

    def _w(self, addr, data):
        for i, byte in enumerate(data):
            self._b[addr + i] = byte

    def wq(self, addr, val):
        self._w(addr, struct.pack("<Q", val))

    def read_bytes(self, addr, size):
        return bytes(self._b.get(addr + i, 0) for i in range(size))

    def read_qword(self, addr):
        return struct.unpack("<Q", self.read_bytes(addr, 8))[0]

    def scan_heap_for_qword(self, value, **_kw):
        self.scan_calls += 1
        return list(self.vtable_hits)


def _plant_map(s, obj, cap=0x100, buckets=0x800000):
    """Make obj+0xF8 look like a valid items map (cap=pow2, buckets in heap range)."""
    s.wq(obj + ITEMS_MAP_OFF + 0x10, cap)
    s.wq(obj + ITEMS_MAP_OFF + 0x18, buckets)


def test_vtable_scan_when_chains_miss():
    s = FakeScanner()              # chains unplanted -> read_qword 0 -> _walk_chain None
    obj = 0x900000000
    _plant_map(s, obj)
    s.vtable_hits = [0x111000, obj]   # a decoy (no map) + the real system
    r = ResearchReader(s)
    assert r._research_system() == obj   # skips the decoy, returns the object with a valid map
    assert s.scan_calls == 1


def test_cache_avoids_rescan():
    s = FakeScanner()
    obj = 0x900000000
    _plant_map(s, obj)
    s.vtable_hits = [obj]
    r = ResearchReader(s)
    assert r._research_system() == obj
    assert r._research_system() == obj
    assert s.scan_calls == 1        # second call reused the cache (cheap revalidation, no re-scan)


def test_chain_preferred_over_scan():
    s = FakeScanner()
    base = s.module_base
    a, b, rs = 0x500000, 0x600000, 0x700000
    s.wq(base + RESEARCH_CHAIN[0], a)   # primary chain resolves to a valid map
    s.wq(a + RESEARCH_CHAIN[1], b)
    s.wq(b + RESEARCH_CHAIN[2], rs)
    _plant_map(s, rs)
    s.vtable_hits = [0xDEAD]            # would be wrong; must NOT be consulted
    r = ResearchReader(s)
    assert r._research_system() == rs
    assert s.scan_calls == 0           # a valid chain wins; the heap scan never runs


def test_none_when_unreachable():
    s = FakeScanner()                  # no chain, no vtable hits
    r = ResearchReader(s)
    assert r._research_system() is None   # fail safe: None, never a garbage address
