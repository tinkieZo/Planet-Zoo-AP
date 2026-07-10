"""Chunk-scan correctness for MemoryScanner._find_qword.

Regression for the Proton park-info bug (2026-07-08): scan_heap_for_qword used to SKIP regions >= 64 MB,
so an object in Wine's single big heap region was never found. It now chunk-scans large regions. The
subtle risk is chunk boundaries - this proves an 8-aligned qword straddling a boundary is still found and
unaligned occurrences are skipped.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import scanner as scan_mod          # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner        # noqa: E402

BASE = 0x140000000  # 8-aligned, page-aligned like a real region base


class _FakePm:
    def __init__(self, blob: bytes):
        self.blob = blob

    def read_bytes(self, addr: int, n: int) -> bytes:
        off = addr - BASE
        if off < 0 or off + n > len(self.blob):
            raise OSError("out of range")
        return self.blob[off:off + n]


def _scanner(blob: bytes) -> MemoryScanner:
    s = MemoryScanner("PlanetZoo.exe")
    s.pm = _FakePm(blob)
    return s


def test_find_qword_across_chunk_boundary(monkeypatch):
    # tiny chunk so the region spans several reads; value planted at an 8-aligned offset that is exactly
    # a chunk boundary (would be the seam between read 1 and read 2).
    monkeypatch.setattr(scan_mod, "_SCAN_CHUNK", 0x40)
    value = 0xCAFEF00DDEADBEEF
    needle = struct.pack("<Q", value)
    region = bytearray(0x100)
    struct.pack_into("<Q", region, 0x40, value)   # 8-aligned, on the chunk seam
    struct.pack_into("<Q", region, 0xA0, value)   # another aligned hit in a later chunk
    out: list = []
    _scanner(bytes(region))._find_qword(BASE, len(region), needle, out, max_hits=16)
    assert out == [BASE + 0x40, BASE + 0xA0], "both 8-aligned hits found across chunk boundaries"


def test_find_qword_skips_unaligned(monkeypatch):
    monkeypatch.setattr(scan_mod, "_SCAN_CHUNK", 0x40)
    value = 0x1122334455667788
    needle = struct.pack("<Q", value)
    region = bytearray(0x80)
    struct.pack_into("<Q", region, 0x13, value)   # NOT 8-aligned -> must be ignored
    struct.pack_into("<Q", region, 0x28, value)   # 8-aligned -> found
    out: list = []
    _scanner(bytes(region))._find_qword(BASE, len(region), needle, out, max_hits=16)
    assert out == [BASE + 0x28], "only the 8-aligned occurrence is reported"


def test_find_qword_respects_max_hits(monkeypatch):
    monkeypatch.setattr(scan_mod, "_SCAN_CHUNK", 0x40)
    value = 0x2222222222222222
    needle = struct.pack("<Q", value)
    region = bytearray(0x100)
    for off in (0x08, 0x48, 0x88):
        struct.pack_into("<Q", region, off, value)
    out: list = []
    _scanner(bytes(region))._find_qword(BASE, len(region), needle, out, max_hits=2)
    assert out == [BASE + 0x08, BASE + 0x48], "stops at max_hits"
