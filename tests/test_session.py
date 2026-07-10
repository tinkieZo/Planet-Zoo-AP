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


def test_invalidate_drops_ghost_cache_and_rescans():
    """Park-unload hygiene (the save+quit+resume redetection fix, live 2026-07-10): invalidate() must
    drop the cached instances AND the last value AND the scan throttle, so the next read() rescans
    immediately instead of serving the unloaded park's ghost name or sitting out a cooldown."""
    fake = FakeScanner({0xA00000: AP_PARK_NAME})
    r = ParkNameReader(fake)
    assert r.read() == AP_PARK_NAME               # warm cache on the loaded park
    del fake._inst[0xA00000]                      # park unloads; instance gone
    r.invalidate()
    assert r._cached is None and r._last_val is None and r._last_scan is None
    assert r.read() is None                       # fresh scan, no cooldown wait, no stale _last_val
    fake.add_instance(0xB00000, AP_PARK_NAME)     # resume: new park-info at a NEW address
    r._last_scan = None                           # collapse the rescan throttle for the test
    assert r.read() == AP_PARK_NAME               # redetected


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


# -- preflight reuse: the client passes its already-scanned reader/gate so run_selfcheck doesn't
#    repeat the ~20s park-info / main.2 heap sweeps (the biggest first-tick cost). ------------------

def test_check_session_reuses_warm_reader():
    """check_session(reader=...) reuses the client's reader: a warm cache means NO extra heap scan,
    while the no-reader path constructs a fresh reader that DOES scan."""
    fake = FakeScanner({0xA00000: AP_PARK_NAME})
    scans = {"n": 0}
    orig = fake.scan_heap_for_qword

    def _counting(value, max_hits=64, max_region=0):
        scans["n"] += 1
        return orig(value, max_hits, max_region)

    fake.scan_heap_for_qword = _counting

    reader = ParkNameReader(fake)
    assert reader.read() == AP_PARK_NAME          # warms the cache (1 scan)
    assert scans["n"] == 1
    res = sig.check_session(fake, reader=reader)   # reuse: cache hit, no new scan
    assert len(res) == 1 and res[0].status == "ok" and scans["n"] == 1

    sig.check_session(fake)                        # fresh reader -> scans again
    assert scans["n"] == 2


class _StubGate:
    def __init__(self, located):
        self._addrs = [0x1000, 0x2000] if located else []
        self._located = located

    def _cache_valid(self):
        return self._located


def test_check_terrain_reuses_gate_no_scan():
    """check_terrain(gate=...) reports from the client's gate state without a fresh _find(): located
    -> ok with the copy count; not-yet-located (first scan deferred) -> ok 'deferred', never 'broken'."""
    located = sig.check_terrain(None, gate=_StubGate(located=True))
    assert located[0].status == "ok" and "2 copy" in located[0].detail

    deferred = sig.check_terrain(None, gate=_StubGate(located=False))
    assert deferred[0].status == "ok" and "deferred" in deferred[0].detail
