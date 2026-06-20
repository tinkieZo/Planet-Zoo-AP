"""Game-free unit test for the reward-grant primitive (pz_ap_client/memory/rewards.py).

Builds a synthetic address space (a fake scanner over a dict of byte ranges) laid out exactly
like the live structures rewards.py parses - the intern registry (name<->id pool) and the
research-system unlockables map - then asserts grant() resolves a content name and flips its
unlocked byte, and is idempotent. No game, no pymem.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from pz_ap_client.memory import rewards
except Exception as e:  # pragma: no cover - memory pkg import guard
    pytest.skip(f"rewards module not importable: {e}", allow_module_level=True)

MODULE_BASE = 0x140000000
REG = MODULE_BASE + rewards.REGISTRY_RVA
RS = 0x20000000          # fake research-system base
POOL_TOP = 0x30000000    # intern pool top
STRIDE = 0x8


class FakeScanner:
    """Sparse byte-addressed memory backing read_bytes/write_bytes/read_qword."""

    def __init__(self):
        self.module_base = MODULE_BASE
        self.attached = True
        self.mem: dict = {}

    def attach(self):
        return True

    def put(self, addr: int, data: bytes):
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    def read_bytes(self, addr: int, n: int) -> bytes:
        try:
            return bytes(self.mem[addr + i] for i in range(n))
        except KeyError:
            raise OSError(f"unmapped read at 0x{addr:X}+{n}")

    def write_bytes(self, addr: int, data: bytes) -> bool:
        self.put(addr, data)
        return True

    def read_qword(self, addr: int):
        try:
            return struct.unpack("<Q", self.read_bytes(addr, 8))[0]
        except OSError:
            return 0


def _build(scanner: FakeScanner, names: dict, unlock_records: list):
    """names: {content_id: name}; unlock_records: list of (content_id, type, unlocked)."""
    # --- intern registry header (rewards.InternRegistry reads +0x10 stride, +0x30 pool_top,
    #     +0x9C bucket_count for the plausibility gate) ---
    scanner.put(REG + 0x10, struct.pack("<q", STRIDE))
    scanner.put(REG + 0x30, struct.pack("<Q", POOL_TOP))
    scanner.put(REG + 0x9C, struct.pack("<I", 64))
    # The id-space enumerator reads a whole SCAN_CHUNK of slots in one contiguous read, so map the
    # entire first chunk [POOL_TOP - CHUNK*STRIDE, POOL_TOP) as zeros (empty slots), then set the
    # real ones. (Test ids are small, so they all fall in the first chunk; the next chunk is left
    # unmapped, which terminates enumeration via OSError.)
    chunk_bytes = rewards.InternRegistry.SCAN_CHUNK * STRIDE
    scanner.put(POOL_TOP - chunk_bytes, bytes(chunk_bytes))
    # name pool: slot for id n at POOL_TOP - n*STRIDE holds a pointer to a record whose name
    # cstring sits at rec+8. Lay records out in a scratch region.
    rec_base = 0x28000000
    for i, (cid, name) in enumerate(names.items()):
        rec = rec_base + i * 0x80
        scanner.put(rec, struct.pack("<I", 1))            # refcount (unused)
        # _name() reads a fixed 96-byte window at rec+8, so pad past the cstring with zeros.
        scanner.put(rec + 8, name.encode("ascii") + b"\x00" * (96 + 1))
        scanner.put(POOL_TOP - cid * STRIDE, struct.pack("<Q", rec))  # slot -> rec (even => valid)
    # terminate the enumeration: the slot just past the highest id must be unreadable, so leave a
    # gap (build_index stops when a whole SCAN_CHUNK is unmapped). Highest id is small here, and the
    # chunked reader catches OSError on the first unmapped chunk -> fine.

    # --- unlockables map at RS+0x148: {+8 count, +0x10 cap, +0x18 buckets}; bitmap then records ---
    cap = 8
    count = len(unlock_records)
    buckets = 0x21000000
    scanner.put(RS + rewards.UNLOCK_MAP_OFF + 0x08, struct.pack("<q", count))
    scanner.put(RS + rewards.UNLOCK_MAP_OFF + 0x10, struct.pack("<q", cap))
    scanner.put(RS + rewards.UNLOCK_MAP_OFF + 0x18, struct.pack("<Q", buckets))
    bm_len = ((cap >> 3) + 7) & ~7
    bitmap = bytearray(bm_len)
    recs = buckets + bm_len
    scanner.put(recs, bytes(cap * rewards.REC_STRIDE))  # zero-fill the whole record region first
    for i, (cid, typ, unlocked) in enumerate(unlock_records):
        bitmap[i >> 3] |= (1 << (i & 7))
        rec = recs + i * rewards.REC_STRIDE
        blob = bytearray(rewards.REC_STRIDE)
        struct.pack_into("<I", blob, rewards.REC_TYPE, typ)
        struct.pack_into("<I", blob, rewards.REC_KEY, cid)
        blob[rewards.REC_UNLOCKED] = unlocked
        scanner.put(rec, bytes(blob))
    scanner.put(buckets, bytes(bitmap))


class FakeResearch:
    def __init__(self, rs):
        self._rs = rs

    def _research_system(self):
        return self._rs


def _granter(scanner):
    return rewards.RewardGranter(scanner, FakeResearch(RS))


def test_grant_flips_unlocked_byte():
    s = FakeScanner()
    # type 1 (enrichment) needs no bookkeeping - the clean case verified live.
    _build(s, names={0x10: "en_grazing_ball", 0x11: "en_herbs"},
           unlock_records=[(0x10, 1, 0), (0x11, 1, 1)])
    g = _granter(s)
    # grazing ball starts locked -> grant flips it to 1
    assert g.grant("EN_Grazing_Ball") is True
    rec = None
    for r, cid, typ, flag in rewards.UnlockMap(s, RS).iter_records():
        if cid == 0x10:
            rec = (r, flag)
    assert rec is not None and rec[1] == 1, "grazing ball unlocked byte set to 1"


def test_grant_idempotent_when_already_unlocked():
    s = FakeScanner()
    _build(s, names={0x11: "en_herbs"}, unlock_records=[(0x11, 1, 1)])
    g = _granter(s)
    assert g.grant("EN_Herbs") is True  # already unlocked -> success, no-op


def test_grant_unknown_content_fails():
    s = FakeScanner()
    _build(s, names={0x10: "en_grazing_ball"}, unlock_records=[(0x10, 1, 0)])
    g = _granter(s)
    assert g.grant("EN_Does_Not_Exist") is False  # not in the registry


def test_grant_content_not_in_unlock_map_fails():
    s = FakeScanner()
    # name resolves but it's not a research-reward-gated content (absent from the unlock map).
    _build(s, names={0x10: "en_grazing_ball", 0x12: "some_other"},
           unlock_records=[(0x10, 1, 0)])
    g = _granter(s)
    assert g.grant("some_other") is False


def test_progressive_grants_lowest_locked_of_family():
    s = FakeScanner()
    # two supplement (type 0) contents, one already unlocked -> progressive grants the locked one.
    _build(s, names={0x20: "sup_a", 0x21: "sup_b"},
           unlock_records=[(0x20, 0, 1), (0x21, 0, 0)])
    g = _granter(s)
    assert g.grant_progressive("supplement") is True
    flags = {cid: flag for _, cid, _, flag in rewards.UnlockMap(s, RS).iter_records()}
    assert flags[0x21] == 1, "the locked supplement content got unlocked"


class FakeBarrierResearch:
    """Fake ResearchReader for reconcile_barriers: a mechanic items map (rs+0xF8) over the FakeScanner.
    Each researchable barrier's research item has a status byte at a fake address (start 0 = locked);
    scan_records reads it live so a status-write is observable. reconcile_barriers should set grade<=N
    items to BARRIER_BUILDABLE_STATUS (3) - NOT 4 (which is the location-fire status)."""

    def __init__(self, scanner, items):
        self.scanner = scanner
        self._items = items  # list of (research_name, item_id, status_addr)
        for _n, _iid, addr in items:
            scanner.put(addr, bytes([0]))

    def _research_system(self):
        return RS

    def _mechanic_item_map(self):
        return {rewards._norm(n): iid for n, iid, _a in self._items}

    def scan_records(self):
        for _n, iid, addr in self._items:
            yield (iid, 0, self.scanner.read_bytes(addr, 1)[0], 3, addr)  # (id, lvl, status, cat=3, addr)


def test_reconcile_barriers_unlocks_researchable_by_grade():
    # Level N makes every RESEARCHABLE barrier of grade <= N buildable via a status-write to 3 (NOT 4,
    # so the barrier_N location never fires). Grades 1 & 3 are defaults-only, so N=1 unlocks nothing and
    # N=3 unlocks the same as N=2 (only OneWayGlass, grade 2).
    s = FakeScanner()
    items = [  # (research-item name, item id, fake status_addr) - grades come from BARRIER_RESEARCH_GRADE
        ("barriersonewayglass",     0x2794, 0x40000000),  # g2
        ("barrierschainsteelposts", 0x32CA, 0x40000008),  # g4
        ("barriersthickglass",      0x32CB, 0x40000010),  # g4
        ("barriersrebarstonecages", 0x2793, 0x40000018),  # g5
        ("barriersconcrete",        0x278E, 0x40000020),  # g6
        ("barrierselectric",        0x32C9, 0x40000028),  # g6
    ]
    g = rewards.RewardGranter(s, FakeBarrierResearch(s, items))
    grade = rewards.BARRIER_RESEARCH_GRADE
    st = lambda addr: s.read_bytes(addr, 1)[0]
    for n in range(1, 7):  # received N Progressive Barrier Levels
        assert g.reconcile_barriers(n) is True
        for name, _iid, addr in items:
            exp = rewards.BARRIER_BUILDABLE_STATUS if grade[name] <= n else 0
            assert st(addr) == exp, f"after {n} levels, {name} (g{grade[name]}) = {st(addr)} (exp {exp})"
    # never writes the location-fire status (4)
    assert all(st(addr) == rewards.BARRIER_BUILDABLE_STATUS for _n, _i, addr in items)
    assert rewards.BARRIER_BUILDABLE_STATUS == 3
    assert g.reconcile_barriers(6) is True  # idempotent at full


def test_reconcile_facilities_reveals_on_facility_keys():
    # facility_unlock(research_centre/workshop) reveals the fdb-hidden build items by status-writing their
    # NoneResearchable placeholder (GuestSpawner/ParkGate) to 4 (scenery reveals at 4, not 3). Placeholders
    # are NOT AP locations, so no false check. Driven by the received facility_unlock keys each tick.
    s = FakeScanner()
    items = [
        ("guestspawner", 0xC350, 0x40000100),   # research_centre placeholder (50000)
        ("parkgate",     0xC351, 0x40000108),   # workshop placeholder (50001)
    ]
    g = rewards.RewardGranter(s, FakeBarrierResearch(s, items))
    st = lambda a: s.read_bytes(a, 1)[0]
    assert g.reconcile_facilities(set()) is True            # nothing received -> nothing written
    assert st(0x40000100) == 0 and st(0x40000108) == 0
    assert g.reconcile_facilities({"research_centre"}) is True   # RC only
    assert st(0x40000100) == rewards.FACILITY_BUILDABLE_STATUS and st(0x40000108) == 0
    assert g.reconcile_facilities({"research_centre", "workshop"}) is True  # both
    assert st(0x40000100) == 4 and st(0x40000108) == 4
    assert rewards.FACILITY_BUILDABLE_STATUS == 4
