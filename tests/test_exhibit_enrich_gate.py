"""Game-free tests for the native ExhibitEnrichmentGate (two-site Lua-bytecode exhibit tier gate).

Exhibit enrichment tiers are gated by patching TWO windows in ExhibitInfoPopUp's loaded bytecode
(main.54 shown-vs-researched + main.32 toggle-state population; live-validated 2026-07-10, Eastern
Brown Snake). This tests the embedded constants + patch/reconcile/restore logic with a fake scanner
(no game, no server). The byte-signature SEARCH (_find) is exercised live via the probe, not here.

Run:  python -m tests.test_exhibit_enrich_gate
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import exhibit_enrich as E  # noqa: E402


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


S54, S32 = E.SITES


def test_constants() -> None:
    _check(len(E.MAIN54_CODE) == 56 * 4, f"main.54 code is 224 bytes (got {len(E.MAIN54_CODE)})")
    _check(len(E.MAIN32_CODE) == 138 * 4, f"main.32 code is 552 bytes (got {len(E.MAIN32_CODE)})")
    _check(S54.orig.hex() == "07c201011f8043041e0000800342000003028000",
           "main.54 pc44-48 orig = GETTABLE/EQ window (bIsUnlocked computation)")
    _check(S32.orig.hex() == "c78303045f40c0071e800a80",
           "main.32 pc86-88 orig = GETTABLE/EQ-nil/JMP window (selectedIDs unlock gate)")
    # the exact live-validated set=1 patches (Eastern Brown Snake, 2026-07-10)
    _check(S54.variant(1).hex() == "5f00c2031e00008003420000030280001ec0ff7f",
           "main.54 set=1 patch matches the live-validated bytes")
    _check(S32.variant(1).hex() == "c103010021c003071e800a80",
           "main.32 set=1 patch matches the live-validated bytes")
    for site in E.SITES:
        _check(len(site.prefix) == site.off, f"{site.name} prefix runs exactly up to the window")
        for n in range(0, E.MAX_LEVEL + 1):
            _check(len(site.variant(n)) == site.len, f"{site.name} variant({n}) fills the window exactly")
        variants = {site.variant(n) for n in range(0, E.MAX_LEVEL + 1)}
        _check(len(variants) == E.MAX_LEVEL + 1 and site.orig not in variants,
               f"{site.name} variants are distinct from each other and from the original")


def _make_gate():
    base = 0x30000
    pad = b"\xCC" * 0x80
    blob = pad + E.MAIN54_CODE + pad + E.MAIN32_CODE + pad
    at54 = base + len(pad)
    at32 = at54 + len(E.MAIN54_CODE) + len(pad)
    sc = FakeScanner(base, blob)
    g = E.ExhibitEnrichmentGate(sc)
    g._addrs = {S54.name: [at54], S32.name: [at32]}  # simulate a successful _find (search runs live)
    g.set_gated(True)
    return g, sc, at54, at32


def test_reconcile_counts() -> None:
    g, sc, at54, at32 = _make_gate()
    for n in range(0, E.MAX_LEVEL + 1):
        g.reconcile(n)
        _check(sc.read_bytes(at54 + S54.off, S54.len) == S54.variant(n),
               f"reconcile({n}) patches main.54 to variant({n})")
        _check(sc.read_bytes(at32 + S32.off, S32.len) == S32.variant(n),
               f"reconcile({n}) patches main.32 to variant({n})")
    # counts beyond MAX_LEVEL clamp (a dup-heavy multiworld can send more copies)
    g.reconcile(7)
    _check(sc.read_bytes(at54 + S54.off, S54.len) == S54.variant(3), "count 7 clamps to variant(3)")


def test_idempotent_and_restore() -> None:
    g, sc, at54, at32 = _make_gate()
    g.reconcile(2)
    before = bytes(sc.mem)
    g.reconcile(2)
    _check(bytes(sc.mem) == before, "reconcile is idempotent on unchanged count")
    g.shutdown()
    _check(sc.read_bytes(at54 + S54.off, S54.len) == S54.orig, "shutdown restores main.54 original")
    _check(sc.read_bytes(at32 + S32.off, S32.len) == S32.orig, "shutdown restores main.32 original")
    _check(sc.read_bytes(at54, 8) == E.MAIN54_CODE[:8] and sc.read_bytes(at32, 8) == E.MAIN32_CODE[:8],
           "validity prefixes never patched")


def test_disabled_and_unknown_window() -> None:
    g, sc, at54, _ = _make_gate()
    g.set_gated(False)
    before = bytes(sc.mem)
    _check(g.reconcile(0) is True, "disabled gate reconciles as a no-op success")
    _check(bytes(sc.mem) == before, "disabled gate never writes")
    # an unexpected window (foreign patch / wrong build) must never be overwritten
    g.set_gated(True)
    sc.write_bytes(at54 + S54.off, b"\xDE\xAD\xBE\xEF" * 5)
    g.reconcile(1)
    _check(sc.read_bytes(at54 + S54.off, 4) == b"\xDE\xAD\xBE\xEF", "unknown window left untouched")


def test_first_scan_deferred() -> None:
    """The first _find() (a full writable-heap sweep) stays off the first poll tick, like TerrainGate."""
    g, sc, at54, at32 = _make_gate()
    g._addrs = {}
    calls = {"n": 0}

    def _fake_find():
        calls["n"] += 1
        return {S54.name: [at54], S32.name: [at32]}

    g._find = _fake_find  # the real byte-search is exercised live, not here
    _check(g._first_scan_pending is True, "fresh gate starts with the first scan pending")
    _check(g.reconcile(1) is False and calls["n"] == 0, "first reconcile defers without scanning")
    _check(g.reconcile(1) is True and calls["n"] == 1, "second reconcile scans once and locates")
    _check(sc.read_bytes(at54 + S54.off, S54.len) == S54.variant(1),
           "after locating, the deferred reconcile applies the patch")


def test_partial_find_rejected() -> None:
    """Both arrays live in the same chunk - a find that only sees one is stale/foreign: don't patch."""
    g, sc, at54, _ = _make_gate()
    g._addrs = {}
    g._first_scan_pending = False
    g._find = lambda: {S54.name: [at54], S32.name: []}
    _check(g.reconcile(1) is False, "partial find -> not located, nothing patched")
    _check(sc.read_bytes(at54 + S54.off, S54.len) == S54.orig, "main.54 untouched on partial find")
    _check(g._cooldown == E._FIND_COOLDOWN, "partial find arms the re-scan cooldown")


def main() -> int:
    test_constants()
    test_reconcile_counts()
    test_idempotent_and_restore()
    test_disabled_and_unknown_window()
    test_first_scan_deferred()
    test_partial_find_rejected()
    print("\nAll ExhibitEnrichmentGate tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
