"""ParkAgeReader - reads the number of COMPLETED YEARS the park has been open, to detect a FRESH save so
cumulative items (cash / conservation credits) re-award on a brand-new zoo. The unlock gates already
re-apply on load; this covers the cumulative grants, which a fresh zoo starts without.

Why this signal: the displayed YEAR is *computed* in this build (never a stored field - confirmed across
the calendar object, the script-native date getters, and the duration math), so it can't be read
directly. But the "park-info" object exposes a plain counter at +0x1c8 = years the park has been open
(GetParkPeriodsOpen reads *(world+0xa8)+0x1c8). It's monotonic and persisted in the save - confirmed live:
Year 1 -> 0, Year 2 -> 1, Year 30 -> 29. A value < 1 (still in Year 1) == a freshly-founded zoo.

How it's located (cross-save robust): we VTABLE-SCAN the park-info class (signatures.PARKINFO_VTABLE_RVA)
- the same layout-independent technique that made the research system robust - rather than chase a
pointer chain to the unreachable world object. There are two instances: a static template that always
reads 0, and the live park carrying the real count; taking the MAX +0x1c8 over instances picks the live
one and ignores the template. The scan is throttled (cached for a few seconds) so it never stalls the
poll loop. Returns None if no instance resolves (no loaded zoo, or the vtable RVA went stale on a patch
-> re-derive with tools/parkage_probe.py --find-world); the caller treats None as "not fresh" (fail safe).
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Optional

from pz_ap_client.memory import signatures as sig

logger = logging.getLogger("PZClient")

# A zoo is "fresh" while still in its first year (completed-years counter below this). Year 1 -> 0.
FRESH_YEARS = 1
# Master gate. The park-age signal is confirmed (Year 1->0, 2->1, 30->29) and vtable-scannable, so the
# fresh re-award is ON. If a game patch shifts PARKINFO_VTABLE_RVA, read() fails safe (None -> not fresh,
# no spurious grant) and the self-check / tools/parkage_probe.py --find-world re-derives the RVA.
PARKAGE_ENABLED = True
_MAX_SANE_YEARS = 100000  # reject garbage reads (a real park is open at most a few hundred years)


class ParkAgeReader:
    VTABLE_RVA = sig.PARKINFO_VTABLE_RVA
    PERIODS_OFF = sig.PARKINFO_PERIODS_OFF
    SCAN_COOLDOWN_S = 5.0   # heap scan is not free; cache the result briefly (years change slowly)

    def __init__(self, scanner):
        self.scanner = scanner
        self._cached = None             # park-info instance addresses (revalidated cheaply each read)
        self._last_scan: Optional[float] = None
        self._last_val: Optional[int] = None
        self._warned = False

    def _target(self) -> Optional[int]:
        base = getattr(self.scanner, "module_base", None)
        return base + self.VTABLE_RVA if base else None

    def _years_at(self, addr: int, target: int) -> Optional[int]:
        """+0x1c8 at addr IFF it still carries the park-info vtable (so a freed/moved object is rejected),
        else None. Raises only on a bad read (caller treats that as a stale cache)."""
        if self.scanner.read_qword(addr) != target:
            return None
        years = struct.unpack("<q", self.scanner.read_bytes(addr + self.PERIODS_OFF, 8))[0]
        return years if 0 <= years < _MAX_SANE_YEARS else None

    def _max_cached(self, target: int) -> Optional[int]:
        """Max years over the cached instances - CHEAP (no heap scan). None if the cache is empty or any
        instance is stale (lost its vtable), which forces a rescan."""
        if not self._cached:
            return None
        best = None
        for a in self._cached:
            try:
                years = self._years_at(a, target)
            except Exception:
                return None
            if years is None:
                return None
            best = years if best is None else max(best, years)
        return best

    def _rescan(self, target: int) -> Optional[int]:
        """Heap-scan the park-info class (the only blocking path), cache the live instances, return max."""
        try:
            hits = self.scanner.scan_heap_for_qword(target, max_hits=64)
        except Exception:
            return self._miss()
        addrs, best = [], None
        for h in hits:
            try:
                years = self._years_at(h, target)
            except Exception:
                continue
            if years is not None:
                addrs.append(h)
                best = years if best is None else max(best, years)
        self._cached = addrs or None
        self._last_val = best
        return best if best is not None else self._miss()

    def read(self) -> Optional[int]:
        """Completed years the park has been open (0 in Year 1), or None if unresolved. Steady state is a
        cheap revalidate of cached instances; a heap scan happens only on a cache miss, throttled, so the
        poll loop (and the websocket keepalive on its event loop) isn't stalled by a recurring scan."""
        target = self._target()
        if target is None:
            return None
        cached = self._max_cached(target)
        if cached is not None:
            return cached
        now = time.monotonic()
        if self._last_scan is not None and (now - self._last_scan) < self.SCAN_COOLDOWN_S:
            return self._last_val
        self._last_scan = now
        return self._rescan(target)

    def _miss(self) -> None:
        if not self._warned:
            self._warned = True
            logger.info("park-age: no park-info instance resolved (no loaded zoo, or vtable RVA stale "
                        "after a patch - re-run tools/parkage_probe.py --find-world). Re-award inactive.")
        return None

    def is_fresh(self) -> bool:
        """True iff the loaded zoo is still in Year 1 (a brand-new save)."""
        v = self.read()
        return v is not None and v < FRESH_YEARS
