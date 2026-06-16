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
