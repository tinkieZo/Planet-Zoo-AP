"""TerrainGate - native per-tool terrain-menu gate via Lua bytecode patch.

The terrain edit menu's per-tool availability is decided in the Cobra Lua function TerrainEditUIMode
``main.2`` (BuildCategories): each tool's ``enabled = not b<X>Disabled``, the ``b<X>Disabled`` flags read
from the IScenarioManager (deeply reflection-dispatched - see memory/water-tools-gate.md). Rather than
chase that reflection, we patch ``main.2``'s LOADED bytecode directly: it is loaded verbatim into a
writable VM heap region, so overwriting one GETTABLE (the source-flag load) with a constant ``LOADBOOL``
forces that flag - greying the tool WITH the "Disabled by scenario" tooltip (gate) or force-enabling it.

LIVE-VALIDATED 2026-06-04 (Goodwin House): patching the water flag made the water tool disabled +
unselectable; restoring re-enabled it. Memory-enforced (not honor-based), reversible, semi-live - the
change takes effect the next time the player enters terrain-edit mode (``main.2`` re-runs on entry).

main.2's 78-instruction code array is found by its byte signature (the VM reloads it fresh per scenario
load, so its address moves - we re-find on cache miss). Source-flag instructions (byte offsets into the
code array):
  0x08  GETTABLE R4 = bTerrainEditDisabled  -> gates SCULPT + STAMP (they share this flag)
  0x0C  GETTABLE R5 = bLakeEditDisabled      -> gates WATER
``painting`` has no enabled gate in main.2 (always available) so it cannot be greyed this way.

Patch = ``LOADBOOL R<A>, gate, 0`` where gate=1 forces disabled=TRUE (tool greys) when the tool's AP item
is NOT received, gate=0 forces FALSE (force-enabled) once it is - so the AP item state fully drives
availability, overriding the scenario default. Same ``set_gated`` / ``reconcile`` / ``shutdown`` shape as
the other gates; degrades to a no-op if the bytecode can't be located.
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
import time
from ctypes import wintypes
from typing import Dict, List, Optional

logger = logging.getLogger("PZClient")

# main.2 (TerrainEditUIMode BuildCategories) code array - 78 instr * 4 = 312 bytes, loaded verbatim.
MAIN2_CODE = bytes.fromhex(
    "8b000000cb00000007014000474140009c0180018d814003cb810100ca01c181ca81c182ca01c283"
    "220100001e80008001820200224200001e00008001c20200ca018284ca4143861b020002ca010287"
    "cac0010385010000a20100001e0004809c0180018d814003cb810100cac1c381ca01c482ca41c483"
    "220100001e80008001820200224200001e00008001c20200ca018284ca4143861b020002ca010287"
    "cac001039c0180018d814003cb010100ca81c481cac1c482ca01c583ca414386cac001039c018001"
    "8d814003cb810100ca41c581ca81c582cac1c583620100001e80008001820200224200001e000080"
    "01c20200ca018284ca4143861b028002ca010287cac001038ac0008c8b018000cb810000ca41438d"
    "ca41808dab4180008a80818c870147008c41470300020001a441800126008000"
)
# Stable validity prefix (instr 0+1) - never patched, so it marks a live, unpatched-or-patched copy.
_SIG_PREFIX = MAIN2_CODE[:8]
# _find must match a copy we ALREADY patched (a client restarted while the game kept running, or a copy that
# outlives park reloads - live 2026-11-14 the 0x7xxxxxxx copy did): the instructions at +0x08/+0x0C are the
# patch windows, so the search anchors on the code AFTER them and checks the prefix BEFORE them.
_PATCH_END = 0x10
_ANCHOR = MAIN2_CODE[_PATCH_END:]

# tool_key -> byte offset into the code array of the GETTABLE that loads its disabled flag.
TOOL_BYTEOFF: Dict[str, int] = {
    "water_tools": 0x0C,   # bLakeEditDisabled -> water
    # a future sculpt/stamp item would map to 0x08 (bTerrainEditDisabled)
}

_MEM_COMMIT = 0x1000
_PAGE_GUARD = 0x100
_WRITABLE = {0x04, 0x08, 0x40, 0x80}  # RW / WC / EXEC_RW / EXEC_WC


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]


def _loadbool(reg_a: int, gate: int) -> bytes:
    """LOADBOOL R<reg_a>, gate, 0  - 4-byte Lua 5.3 instruction."""
    return struct.pack("<I", 3 | ((reg_a & 0xFF) << 6) | ((gate & 1) << 23))


def writable_regions(scanner):
    """All committed writable (non-guard) regions of the game process - the VM heap where loaded Lua
    code arrays live. Shared by every bytecode-patch gate (TerrainGate, ExhibitEnrichmentGate, probes)."""
    try:
        handle = scanner.pm.process_handle
    except Exception:
        return []
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    mbi = _MBI(); addr = 0; out = []
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        if (mbi.State == _MEM_COMMIT and not (mbi.Protect & _PAGE_GUARD)
                and (mbi.Protect & 0xFF) in _WRITABLE and mbi.RegionSize < 0x10000000):
            out.append((base, mbi.RegionSize))
        addr = base + mbi.RegionSize
    return out


class BackgroundFinder:
    """Runs a gate's full writable-heap search (~20 s live) on a worker thread so it NEVER blocks the poll
    tick, and paces retries in wall-clock seconds. Live 2026-11-14: after a park reload the Lua bytecode
    the terrain / exhibit-enrichment gates patch isn't in the heap until the player opens those menus, and
    the old tick-counted cooldown re-ran BOTH sweeps inline every ~8 ticks - ~70% of a core, 20-40 s ticks,
    and every check (e.g. a first acquisition) delayed by minutes.

    take(): the finished search result exactly once, else None (search running / retry not due yet - a
    new search is started when due). miss(): the caller found nothing usable -> retry after RETRY_S.
    ``background=False`` runs the search inline (tests) but keeps the same one-call deferral."""

    RETRY_S = 30.0
    _NONE = object()

    def __init__(self, find_fn, background: bool = True):
        self._find_fn = find_fn
        self.background = background
        self._thread = None
        self._result = self._NONE
        self._next_ok = 0.0

    def _run(self) -> None:
        try:
            self._result = self._find_fn()
        except Exception as e:      # a failed sweep is a miss, never a crash of the poll loop
            logger.debug("background heap search failed: %s", e)
            self._result = None

    def take(self):
        if self._thread is not None and self._thread.is_alive():
            return None
        self._thread = None
        if self._result is not self._NONE:
            result, self._result = self._result, self._NONE
            return result
        if time.monotonic() < self._next_ok:
            return None
        if self.background:
            self._thread = threading.Thread(target=self._run, name="pz-heap-search", daemon=True)
            self._thread.start()
        else:
            self._run()
        return None

    def miss(self) -> None:
        self._next_ok = time.monotonic() + self.RETRY_S

    @property
    def retry_armed(self) -> bool:
        return time.monotonic() < self._next_ok


class TerrainGate:
    """Native terrain-tool gate by patching main.2's loaded Lua bytecode. Driven from the received
    tool-item set each tick (authoritative + idempotent, restart-correct)."""

    def __init__(self, scanner):
        self.scanner = scanner
        self.gated_tools: set = set()
        self._addrs: List[int] = []          # located copies of main.2's code
        self._orig: Dict[int, bytes] = {}     # byteoff -> original 4 bytes (for restore)
        self._applied: Dict[int, int] = {}    # byteoff -> last gate value written (idempotency)
        # full-heap search off the poll thread (late-bound so tests can swap _find)
        self._finder = BackgroundFinder(lambda: self._find())

    def set_gated(self, tool_keys) -> None:
        """Declare which terrain tools have an AP item (only these are ever patched)."""
        self.gated_tools = {k for k in tool_keys if k in TOOL_BYTEOFF}
        self._applied.clear()

    # -- bytecode location -----------------------------------------------------

    def _regions(self):
        return writable_regions(self.scanner)

    def _cache_valid(self) -> bool:
        """Cheap check that the cached addresses still hold main.2's code (a scenario reload moves it)."""
        if not self._addrs:
            return False
        try:
            return all(self.scanner.read_bytes(a, len(_SIG_PREFIX)) == _SIG_PREFIX for a in self._addrs)
        except Exception:
            return False

    def _find(self) -> List[int]:
        hits: List[int] = []
        for rb, rs in self._regions():
            try:
                data = self.scanner.read_bytes(rb, rs)
            except Exception:
                continue
            i = data.find(_ANCHOR)
            while i != -1:
                start = i - _PATCH_END     # patch windows (+0x08/+0x0C) may hold ORIG or our LOADBOOL
                if start >= 0 and data[start:start + len(_SIG_PREFIX)] == _SIG_PREFIX:
                    hits.append(rb + start)
                i = data.find(_ANCHOR, i + 1)
        return hits

    def _ensure_located(self) -> bool:
        """Cached copies valid -> True. Otherwise the full-heap _find() (~20 s live) runs on the
        BackgroundFinder thread - never on the poll tick (this also keeps it off the first tick, so READY
        comes ~20 s sooner); a later tick adopts the result. The gate is ungated while it searches, but
        main.2 only exists once the terrain menu was opened, and the search resumes within RETRY_S."""
        if self._cache_valid():
            return True
        hits = self._finder.take()
        if hits is None:
            return False
        # the sweep took a while - keep only copies that still hold main.2's code
        hits = [a for a in hits if self._prefix_at(a)]
        self._addrs = hits
        self._orig.clear()
        self._applied.clear()
        if hits:
            logger.info("terrain gate: located main.2 bytecode at %s", [hex(a) for a in hits])
            return True
        self._finder.miss()
        return False

    def _prefix_at(self, addr: int) -> bool:
        try:
            return self.scanner.read_bytes(addr, len(_SIG_PREFIX)) == _SIG_PREFIX
        except Exception:
            return False

    # -- patch / reconcile -----------------------------------------------------

    def _patch(self, byteoff: int, gate: int) -> None:
        """Write LOADBOOL R<A>,gate,0 at every located copy (idempotent). The original instruction comes
        from MAIN2_CODE, never from the live copy: a copy found ALREADY patched (client restart) would
        otherwise record our LOADBOOL as the 'original' and shutdown() would restore the gate, not the game."""
        orig = MAIN2_CODE[byteoff:byteoff + 4]
        self._orig[byteoff] = orig
        reg_a = (struct.unpack("<I", orig)[0] >> 6) & 0xFF
        patch = _loadbool(reg_a, gate)
        for a in self._addrs:
            try:
                self.scanner.write_bytes(a + byteoff, patch)
            except Exception as e:
                logger.warning("terrain gate: write @0x%X+0x%X failed: %s", a, byteoff, e)
        self._applied[byteoff] = gate

    def reconcile(self, unlocked_tools) -> bool:
        """Authoritative gate sync: for each gated terrain tool, force its disabled flag = (item not
        received). Idempotent; only writes on change. Returns True if the bytecode is patched/located."""
        if not self.gated_tools:
            return True
        if not self._ensure_located():
            return False
        unlocked = set(unlocked_tools)
        changed = False
        for key in self.gated_tools:
            byteoff = TOOL_BYTEOFF[key]
            gate = 0 if key in unlocked else 1
            if self._applied.get(byteoff) != gate:
                self._patch(byteoff, gate)
                changed = True
        if changed:
            logger.info("terrain gate: tools %s gated=%s",
                        sorted(self.gated_tools), sorted(k for k in self.gated_tools if k not in unlocked) or "none")
        return True

    def shutdown(self) -> None:
        """Restore the original bytecode at every located copy (leave the game clean on disconnect)."""
        if self._cache_valid():
            for byteoff, orig in self._orig.items():
                for a in self._addrs:
                    try:
                        self.scanner.write_bytes(a + byteoff, orig)
                    except Exception:
                        pass
        self._addrs = []
        self._orig.clear()
        self._applied.clear()
