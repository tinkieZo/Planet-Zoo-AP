"""Game-free tests for ParkAgeReader - vtable-scans the park-info class and reads completed-years-open
(+0x1c8) to detect a FRESH (Year 1) save for cumulative-item re-award. Verifies it takes the max over
instances (live park vs static template), the fresh threshold, sane-range rejection, and fail-safe None
(never a spurious fresh-reset)."""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import signatures as sig  # noqa: E402
from pz_ap_client.memory.zoodate import ParkAgeReader, FRESH_YEARS  # noqa: E402

BASE = 0x140000000
VTABLE = BASE + sig.PARKINFO_VTABLE_RVA
POFF = sig.PARKINFO_PERIODS_OFF


class FakeScanner:
    """Heap of park-info instances: {addr: years}. scan_heap_for_qword returns those whose +0x00==vtable."""
    def __init__(self, instances: dict):
        self.module_base = BASE
        self._inst = instances            # addr -> years_open
        self._extra: dict = {}            # addr -> qword (non-park objects to ignore)

    def scan_heap_for_qword(self, value, max_hits=64, max_region=0):
        if value != VTABLE:
            return []
        return list(self._inst.keys())[:max_hits]

    def read_qword(self, addr):
        return VTABLE if addr in self._inst else self._extra.get(addr, 0)

    def read_bytes(self, addr, size):
        for base, years in self._inst.items():
            if addr == base + POFF and size == 8:
                return struct.pack("<q", years)
        return b"\x00" * size


def test_live_park_max_over_template():
    # template always 0, live park at Year 30 -> 29 completed years; max picks the live park
    r = ParkAgeReader(FakeScanner({0x900000: 0, 0xA00000: 29}))
    assert r.read() == 29
    assert r.is_fresh() is False


def test_fresh_year_one_is_zero():
    r = ParkAgeReader(FakeScanner({0x900000: 0, 0xA00000: 0}))   # Year 1: both 0
    assert r.read() == 0
    assert r.is_fresh() is True


def test_year_two_not_fresh():
    r = ParkAgeReader(FakeScanner({0x900000: 0, 0xA00000: 1}))   # Year 2 -> 1 >= FRESH_YEARS
    assert r.read() == 1
    assert r.is_fresh() is False
    assert FRESH_YEARS == 1


def test_none_when_no_instances():
    r = ParkAgeReader(FakeScanner({}))   # no loaded zoo -> no instances
    assert r.read() is None
    assert r.is_fresh() is False


def test_rejects_out_of_range_years():
    r = ParkAgeReader(FakeScanner({0x900000: 2_000_000}))   # garbage -> rejected -> None
    assert r.read() is None
