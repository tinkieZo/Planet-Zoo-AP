"""Game-free tests for the exhibit add-animal detection (the EXHIBIT analog of births.py).

Covers the pure logic that the live install can't exercise cheaply:
  * hook.read_exhibit_events - drain of the {iVar8, species name} ring (cursor advance, incremental
    drain, wraparound cap);
  * hook.exhibit_event_is_acquire - the acquire-vs-breed classifier (iVar8 == -1 => birth, else acquire);
  * ExhibitDetector.poll_events - end-to-end attribution: species name token -> species_key (via a fake
    research reader) + classify each by iVar8 as born/acquired, with unknown tokens skipped.

Validated live 2026-06-23: a scorpion purchase captured iVar8=0x10230 + "GiantDesertHairyScorpion"; four
beetle births captured iVar8=-1 + "GoliathBeetle". The detour ASM + live hook are validated in-game. Run:
    python -m tests.test_exhibit_hook
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import hook as H  # noqa: E402
from pz_ap_client.memory.exhibits import ExhibitDetector  # noqa: E402

BIRTH = -1            # iVar8 for a fresh-construct birth
ACQUIRE = 0x10230     # iVar8 for a market buy (a real preset animal id), as seen live


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


class FakeScanner:
    """Minimal scanner: a flat byte buffer addressed from ``base``; exposes read_bytes."""

    def __init__(self, base: int = 0x500000):
        self.base = base
        self.module_base = 0x140000000
        self.mem = bytearray(0x4000)

    def write(self, off: int, data: bytes) -> None:
        self.mem[off:off + len(data)] = data

    def read_bytes(self, addr: int, n: int) -> bytes:
        off = addr - self.base
        return bytes(self.mem[off:off + n])


class FakeResearch:
    """Resolves a captured species name token -> key from a fixed dict (stands in for the token map)."""

    def __init__(self, name_to_key: dict):
        self._map = name_to_key

    def species_key_for_name(self, name: str):
        return self._map.get(name)


def _set_count(s: FakeScanner, count: int) -> None:
    s.write(0, struct.pack("<I", count))


def _set_ring(s: FakeScanner, events) -> None:
    """Place (ivar8, name) events into the ring exactly where the trampoline's (n-1)&(RING-1) lands."""
    for n, (ivar8, name) in enumerate(events, start=1):
        idx = (n - 1) & (H.EXHIBIT_RING - 1)
        slot = H.EXHIBIT_RING_OFF + idx * H.EXHIBIT_REC
        s.write(slot, struct.pack("<I", ivar8 & 0xFFFFFFFF))
        nb = name.encode("ascii")[:H.EXHIBIT_NAME_BYTES - 1] + b"\x00"
        s.write(slot + 0x8, nb.ljust(H.EXHIBIT_NAME_BYTES, b"\x00"))


# -- ring drain ---------------------------------------------------------------

def test_drain_basic() -> None:
    s = FakeScanner()
    events = [(ACQUIRE, "GiantDesertHairyScorpion"), (BIRTH, "GoliathBeetle")]
    _set_count(s, 2)
    _set_ring(s, events)
    cur, got = H.read_exhibit_events(s, s.base, 0)
    _check(cur == 2, "cursor advances to count (2)")
    _check([e["name"] for e in got] == ["GiantDesertHairyScorpion", "GoliathBeetle"],
           "drained both names in order (incl. a 24-char token)")
    _check([e["ivar8"] & 0xFFFFFFFF for e in got] == [ACQUIRE, 0xFFFFFFFF], "drained iVar8 per event")


def test_nothing_new() -> None:
    s = FakeScanner()
    _set_count(s, 5)
    cur, got = H.read_exhibit_events(s, s.base, 5)
    _check(cur == 5 and got == [], "cursor==count -> no new events")


def test_incremental() -> None:
    s = FakeScanner()
    events = [(BIRTH, n) for n in ("A", "B", "C", "D")]
    _set_count(s, 4)
    _set_ring(s, events)
    cur, got = H.read_exhibit_events(s, s.base, 2)  # already saw the first 2
    _check(cur == 4 and [e["name"] for e in got] == ["C", "D"], "only the 2 new events since cursor 2")


def test_wraparound_caps_at_ring() -> None:
    s = FakeScanner()
    total = H.EXHIBIT_RING + 3
    events = [(BIRTH, f"sp{i}") for i in range(total)]
    _set_count(s, total)
    _set_ring(s, events)
    cur, got = H.read_exhibit_events(s, s.base, 0)
    _check(cur == total, "cursor advances to total")
    _check(len(got) == H.EXHIBIT_RING, f"drain caps at ring size {H.EXHIBIT_RING} (got {len(got)})")
    _check(got[-1]["name"] == f"sp{total - 1}", "newest event survives (ring tail)")


# -- classifier ---------------------------------------------------------------

def test_classifier() -> None:
    _check(H.exhibit_event_is_acquire(0x10230) is True, "preset animal id (0x10230) -> ACQUIRE")
    _check(H.exhibit_event_is_acquire(-1) is False, "iVar8 -1 (signed) -> BREED")
    _check(H.exhibit_event_is_acquire(0xFFFFFFFF) is False, "iVar8 0xFFFFFFFF (unsigned -1) -> BREED")
    _check(H.exhibit_event_is_acquire(0) is True, "iVar8 0 (a valid id) -> ACQUIRE")


# -- detector attribution -----------------------------------------------------

def _detector(scanner, name_to_key):
    det = ExhibitDetector(scanner, research=FakeResearch(name_to_key))
    det.installed = True            # bypass the live hook install; drain the prepared ring
    det.scratch = scanner.base
    return det


def test_detector_classifies_and_attributes() -> None:
    s = FakeScanner()
    events = [
        (ACQUIRE, "GiantDesertHairyScorpion"),   # bought -> acquired
        (BIRTH, "GoliathBeetle"),                # born   -> born
        (BIRTH, "MysteryBug"),                   # unmapped token -> skipped
    ]
    _set_count(s, len(events))
    _set_ring(s, events)
    det = _detector(s, {"GiantDesertHairyScorpion": "gdscorpian", "GoliathBeetle": "gbeetle"})
    born, acquired = det.poll_events()
    _check(acquired == ["gdscorpian"], f"scorpion purchase -> first_acquire (got {acquired})")
    _check(born == ["gbeetle"], f"beetle birth -> first_breed (got {born})")
    _check("MysteryBug" in det._unknown_logged, "unmapped token is logged + skipped (not attributed)")


def test_detector_cursor_no_redrain() -> None:
    s = FakeScanner()
    _set_count(s, 1)
    _set_ring(s, [(ACQUIRE, "GiantDesertHairyScorpion")])
    det = _detector(s, {"GiantDesertHairyScorpion": "gdscorpian"})
    born, acquired = det.poll_events()
    _check(acquired == ["gdscorpian"] and born == [], "first drain attributes the acquire")
    born2, acquired2 = det.poll_events()
    _check(born2 == [] and acquired2 == [], "second drain sees nothing new (cursor held)")


def main() -> int:
    test_drain_basic()
    test_nothing_new()
    test_incremental()
    test_wraparound_caps_at_ring()
    test_classifier()
    test_detector_classifies_and_attributes()
    test_detector_cursor_no_redrain()
    print("\nAll exhibit-hook + detector tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
