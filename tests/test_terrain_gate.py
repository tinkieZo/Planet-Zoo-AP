"""Game-free tests for the native TerrainGate (Lua-bytecode-patch terrain-tool greying).

The water tool's real in-game availability is gated by patching TerrainEditUIMode main.2's loaded Lua
bytecode (live-validated 2026-06-04: water became disabled + unselectable). This tests the embedded
bytecode constants + the gate's patch/reconcile/restore logic with a fake scanner (no game, no server).
The byte-signature SEARCH (_find) is exercised live, not here.

Run:  python -m tests.test_terrain_gate
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import terrain as T  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


class FakeScanner:
    """Minimal scanner over a bytearray at a fixed base (read_bytes/write_bytes only)."""

    def __init__(self, base: int, blob: bytes):
        self.base = base
        self.mem = bytearray(blob)

    def read_bytes(self, addr: int, n: int) -> bytes:
        off = addr - self.base
        if off < 0 or off + n > len(self.mem):
            raise ValueError("oob read")
        return bytes(self.mem[off:off + n])

    def write_bytes(self, addr: int, b: bytes) -> None:
        off = addr - self.base
        if off < 0 or off + len(b) > len(self.mem):
            raise ValueError("oob write")
        self.mem[off:off + len(b)] = b


def test_constants() -> None:
    _check(len(T.MAIN2_CODE) == 312, f"main.2 code is 312 bytes (got {len(T.MAIN2_CODE)})")
    _check(T.MAIN2_CODE[0x08:0x0C].hex() == "07014000",
           "byteoff 0x08 = GETTABLE R4 bTerrainEditDisabled (sculpt+stamp)")
    _check(T.MAIN2_CODE[0x0C:0x10].hex() == "47414000",
           "byteoff 0x0C = GETTABLE R5 bLakeEditDisabled (water)")
    _check(T.TOOL_BYTEOFF.get("water_tools") == 0x0C, "water_tools maps to byteoff 0x0C")
    # LOADBOOL R5,1 = the exact live-validated water-gate patch
    _check(T._loadbool(5, 1).hex() == "43018000", "LOADBOOL R5,1 == 43018000 (live-validated water gate)")
    _check(T._loadbool(5, 0).hex() == "43010000", "LOADBOOL R5,0 == 43010000 (water force-enable)")


def _make_gate():
    base = 0x20000
    blob = b"\xCC" * 0x100 + T.MAIN2_CODE + b"\xCC" * 0x100
    code_at = base + 0x100
    sc = FakeScanner(base, blob)
    g = T.TerrainGate(sc)
    g._addrs = [code_at]            # simulate a successful _find (search is exercised live)
    g.set_gated({"water_tools"})
    return g, sc, code_at


def test_set_gated_filters() -> None:
    g, _, _ = _make_gate()
    _check(g.gated_tools == {"water_tools"}, "set_gated keeps only known tools")
    g.set_gated({"unknown_tool", "paint"})
    _check(g.gated_tools == set(), "set_gated drops tools with no byteoff mapping")


def test_gate_and_enable() -> None:
    g, sc, code_at = _make_gate()
    water = code_at + 0x0C

    # not received -> water gated (LOADBOOL R5,1)
    g.reconcile(set())
    _check(sc.read_bytes(water, 4).hex() == "43018000", "reconcile(no items): water byteoff patched to LOADBOOL R5,1 (greyed)")

    # received -> water force-enabled (LOADBOOL R5,0)
    g.reconcile({"water_tools"})
    _check(sc.read_bytes(water, 4).hex() == "43010000", "reconcile(water_tools): water byteoff patched to LOADBOOL R5,0 (enabled)")

    # idempotency: re-reconcile with same set does not change bytes
    before = sc.read_bytes(water, 4)
    g.reconcile({"water_tools"})
    _check(sc.read_bytes(water, 4) == before, "reconcile is idempotent on unchanged state")

    # shutdown restores the original GETTABLE
    g.shutdown()
    _check(sc.read_bytes(water, 4).hex() == "47414000", "shutdown restores original bytecode (47414000)")


def test_first_scan_deferred() -> None:
    """The _find() full writable-heap sweep (~20s live) never runs on the poll tick: the first reconcile
    STARTS it (on the BackgroundFinder thread - inline here) and returns not-located; the next reconcile
    adopts the result. A miss re-arms a wall-clock retry instead of re-scanning every few ticks."""
    base = 0x20000
    blob = b"\xCC" * 0x100 + T.MAIN2_CODE + b"\xCC" * 0x100
    code_at = base + 0x100
    sc = FakeScanner(base, blob)
    g = T.TerrainGate(sc)
    g.set_gated({"water_tools"})
    calls = {"n": 0}

    def _fake_find():
        calls["n"] += 1
        return [code_at]

    g._find = _fake_find  # the real byte-search is exercised live, not here
    g._finder.background = False   # run the search inline, same deferral

    located1 = g.reconcile(set())
    _check(located1 is False and calls["n"] == 1,
           "first reconcile only STARTS the search: not located yet (kept off the poll tick)")
    located2 = g.reconcile(set())
    _check(located2 is True and calls["n"] == 1,
           "second reconcile adopts the finished search and locates main.2 (no second sweep)")
    _check(sc.read_bytes(code_at + 0x0C, 4).hex() == "43018000",
           "after locating, the deferred reconcile applies the water gate (LOADBOOL R5,1)")


def test_failed_search_retries_on_wall_clock() -> None:
    """A miss (menu never opened -> main.2 not in the heap) must NOT re-scan every few ticks (live
    2026-11-14: ~70% CPU, 20-40 s ticks after a reload); the retry waits RETRY_S of wall-clock time."""
    sc = FakeScanner(0x20000, b"\xCC" * 0x200)
    g = T.TerrainGate(sc)
    g.set_gated({"water_tools"})
    calls = {"n": 0}

    def _miss():
        calls["n"] += 1
        return []

    g._find = _miss
    g._finder.background = False
    for _ in range(20):
        g.reconcile(set())
    _check(calls["n"] == 1, f"one sweep, then quiet until the retry is due (got {calls['n']} sweeps)")
    _check(g._finder.retry_armed, "miss armed the wall-clock retry")
    g._finder._next_ok = 0.0          # retry due
    g.reconcile(set())
    _check(calls["n"] == 2, "sweeps again once the retry is due")


def test_find_matches_already_patched_copy() -> None:
    """A copy a PREVIOUS client already patched (water gated) must still be found - live 2026-11-14 a
    restarted client never re-located main.2, so it could never ungate water when the item arrived - and
    shutdown must restore the GAME's instruction, not the stale gate."""
    base = 0x20000
    patched = bytearray(T.MAIN2_CODE)
    patched[0x0C:0x10] = bytes.fromhex("43018000")          # LOADBOOL R5,1 from an earlier session
    blob = b"\xCC" * 0x100 + bytes(patched) + b"\xCC" * 0x100
    sc = FakeScanner(base, blob)
    g = T.TerrainGate(sc)
    g.set_gated({"water_tools"})
    g._finder.background = False
    g._regions = lambda: [(base, len(blob))]               # the fake has no VirtualQueryEx
    _check(g._find() == [base + 0x100], "already-patched copy found by the masked search")
    g.reconcile({"water_tools"})                            # start search
    _check(g.reconcile({"water_tools"}) is True, "located")
    _check(sc.read_bytes(base + 0x100 + 0x0C, 4).hex() == "43010000", "item received -> ungated (LOADBOOL R5,0)")
    g.shutdown()
    _check(sc.read_bytes(base + 0x100 + 0x0C, 4) == T.MAIN2_CODE[0x0C:0x10],
           "shutdown restores the game's original GETTABLE, not the earlier session's patch")


def test_stale_hits_dropped() -> None:
    """The background sweep takes ~20 s; a copy that no longer holds main.2 by adoption time is dropped."""
    base = 0x20000
    blob = b"\xCC" * 0x100 + T.MAIN2_CODE + b"\xCC" * 0x100
    sc = FakeScanner(base, blob)
    g = T.TerrainGate(sc)
    g.set_gated({"water_tools"})
    g._find = lambda: [base + 0x100, base + 0x10]   # second hit is stale (no main.2 there)
    g._finder.background = False
    g.reconcile(set())
    _check(g.reconcile(set()) is True and g._addrs == [base + 0x100], "only the live copy is adopted")


def test_other_offsets_untouched() -> None:
    g, sc, code_at = _make_gate()
    g.reconcile(set())  # gate water only
    # the sculpt+stamp flag (0x08) must be untouched (no item gates it this seed)
    _check(sc.read_bytes(code_at + 0x08, 4).hex() == "07014000",
           "gating water leaves the sculpt+stamp flag (0x08) untouched")
    # the validity prefix is never written
    _check(sc.read_bytes(code_at, 8).hex() == "8b000000cb000000", "validity prefix never patched")


def main() -> int:
    test_constants()
    test_set_gated_filters()
    test_gate_and_enable()
    test_first_scan_deferred()
    test_failed_search_retries_on_wall_clock()
    test_stale_hits_dropped()
    test_find_matches_already_patched_copy()
    test_other_offsets_untouched()
    print("\nAll TerrainGate tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
