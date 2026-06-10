"""Game-free tests for AP-session detection (memory/session.py): ParkNameReader reads the native park
name off the vtable-scanned park-info class (+0x1E8 refcounted string), and ApSessionDetector requires
the script-planted "ARCHIPELAGO ZOO" marker AND scenario market mode. Covers the marker match, foreign/
unnamed parks, the spoof guard (marker name but non-scenario mode), the nameless-cache rescan (live
park-info allocated after the first scan), and fail-safe None on unresolved reads."""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import signatures as sig  # noqa: E402
from pz_ap_client.memory.session import (  # noqa: E402
    AP_PARK_NAME,
    ApSessionDetector,
    ParkNameReader,
)

BASE = 0x140000000
VTABLE = BASE + sig.PARKINFO_VTABLE_RVA
NOFF = sig.PARKINFO_NAME_OFF
STR_AT = 0x5000000  # where fake refcounted name strings live


class FakeScanner:
    """Park-info instances {addr: name_or_None}; name strings are laid out in the engine's refcounted
    format {len i64 @+0x00, refcount @+0x10, chars @+0x14} at STR_AT + index."""

    def __init__(self, instances: dict):
        self.module_base = BASE
        self._inst = dict(instances)              # addr -> Optional[str]
        self._strs: dict = {}                     # str_ptr -> bytes(name)
        self._name_ptrs: dict = {}                # instance addr -> str_ptr (0 if unnamed)
        for i, (addr, name) in enumerate(self._inst.items()):
            if name is None:
                self._name_ptrs[addr] = 0
            else:
                p = STR_AT + i * 0x100
                self._strs[p] = name.encode()
                self._name_ptrs[addr] = p

    def add_instance(self, addr, name):
        self._inst[addr] = name
        p = STR_AT + (len(self._strs) + 8) * 0x100
        self._strs[p] = name.encode()
        self._name_ptrs[addr] = p

    def scan_heap_for_qword(self, value, max_hits=64, max_region=0):
        return list(self._inst.keys())[:max_hits] if value == VTABLE else []

    def read_qword(self, addr):
        if addr in self._inst:
            return VTABLE
        if addr - NOFF in self._inst:
            return self._name_ptrs[addr - NOFF]
        if addr in self._strs:
            return len(self._strs[addr])
        return 0

    def read_bytes(self, addr, size):
        for p, raw in self._strs.items():
            if addr == p + 0x14:
                return raw[:size]
        return b"\x00" * size


def test_reads_marker_name_over_template():
    r = ParkNameReader(FakeScanner({0x900000: None, 0xA00000: AP_PARK_NAME}))
    assert r.read() == AP_PARK_NAME


def test_unnamed_park_reads_none():
    r = ParkNameReader(FakeScanner({0x900000: None, 0xA00000: None}))
    assert r.read() is None


def test_no_instances_reads_none():
    assert ParkNameReader(FakeScanner({})).read() is None


def test_nameless_cache_picks_up_late_instance(monkeypatch):
    """A cache built while only the template existed must still find the live park-info allocated at
    world load (rescan on the NONE_RESCAN_S throttle, not never)."""
    fake = FakeScanner({0x900000: None})
    r = ParkNameReader(fake)
    assert r.read() is None                       # caches the nameless template
    fake.add_instance(0xB00000, AP_PARK_NAME)     # world load creates the live instance
    monkeypatch.setattr(r, "NONE_RESCAN_S", 0.0)  # collapse the throttle for the test
    assert r.read() == AP_PARK_NAME


def test_detector_requires_marker_and_mode():
    fake = FakeScanner({0xA00000: AP_PARK_NAME})
    assert ApSessionDetector(fake, mode_check=lambda: True).is_ap_session() is True
    # spoof guard: marker name but the market is NOT in scenario mode (renamed sandbox park)
    assert ApSessionDetector(fake, mode_check=lambda: False).is_ap_session() is False


def test_detector_false_for_foreign_park():
    fake = FakeScanner({0xA00000: "Goodwin House"})
    assert ApSessionDetector(fake, mode_check=lambda: True).is_ap_session() is False


def test_detector_false_when_nothing_loaded():
    assert ApSessionDetector(FakeScanner({}), mode_check=lambda: True).is_ap_session() is False
