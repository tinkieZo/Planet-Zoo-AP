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
        self._val: Optional[int] = None
        self._last: Optional[float] = None
        self._warned = False

    def _scan_max_years(self) -> Optional[int]:
        """Vtable-scan the park-info class; return the max completed-years (+0x1c8) over instances (the
        live park; the template reads 0), or None if no sane instance resolves."""
        base = getattr(self.scanner, "module_base", None)
        if not base:
            return None
        target = base + self.VTABLE_RVA
        try:
            hits = self.scanner.scan_heap_for_qword(target, max_hits=64)
        except Exception:
            return self._miss()
        best = None
        for h in hits:
            try:
                years = struct.unpack("<q", self.scanner.read_bytes(h + self.PERIODS_OFF, 8))[0]
            except Exception:
                continue
            if 0 <= years < _MAX_SANE_YEARS:
                best = years if best is None else max(best, years)
        return best if best is not None else self._miss()

    def read(self) -> Optional[int]:
        """Completed years the park has been open (0 in Year 1), or None if unresolved. Throttled."""
        now = time.monotonic()
        if self._last is not None and (now - self._last) < self.SCAN_COOLDOWN_S:
            return self._val
        self._last = now
        self._val = self._scan_max_years()
        return self._val

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
