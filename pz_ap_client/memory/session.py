"""ApSessionDetector - decides whether the LOADED park is the AP scenario, so the client only
gates/awards inside AP worlds instead of whatever park happens to be loaded.

How: the AP scenario script (ScenarioScripts.Scenario_AP_Script, which the scriptutils hijack activates
ONLY for worlds whose save code is 'Scenario_01_Empty') calls park:SetParkName("ARCHIPELAGO ZOO") in
Init. SetParkName (executor 0x14667A8F0) interns that string and stores it natively at
park-info+0x1E8 - the same vtable-scannable park-info class the fresh-save signal lives on - and the
name PERSISTS in saves (darwinworld's metadata round-trip keeps an existing park name). So:

    AP session  ==  park name marker present  AND  animal-exchange mode byte == scenario (0)

The mode check is belt-and-braces against the one spoofable path: a sandbox/franchise park the player
manually renamed to the marker (those run mode 1/2 - never 0). Vanilla career scenarios run mode 0 but
their names are fixed by their park bins (Goodwin House etc.), never the marker.

Fail-safe direction: any unresolved read -> NOT an AP session -> the client goes idle (no hooks, no
item writes, no checks) in foreign parks. If the marker chain breaks (vtable RVA stale after a game
patch, or an old Main.ovl without the SetParkName call), the client logs the reason loudly instead of
silently gating a park it shouldn't - and the PZAP_NO_SESSION_GATE=1 escape hatch restores the old
gate-everything behaviour for debugging.

Reader pattern mirrors zoodate.ParkAgeReader: steady state is a cheap revalidate of cached park-info
instances; the heap scan runs only on a cache miss, throttled, so the poll loop never stalls.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from pz_ap_client.memory import signatures as sig

logger = logging.getLogger("PZClient")

# The marker the AP scenario script plants via SetParkName at Init. Must match the careerdata entry's
# label (database.c0careerdata.lua) AND scenarioscripts.scenario_ap_script.lua - keep all three in sync.
AP_PARK_NAME = "ARCHIPELAGO ZOO"


def read_refcounted_string(scanner, str_ptr: int, max_len: int = 96) -> Optional[str]:
    """The engine's refcounted string {len i64 @+0x00, refcount i32 @+0x10, chars @+0x14}, or None."""
    if not str_ptr:
        return None
    try:
        n = scanner.read_qword(str_ptr)
        if not 0 < n <= max_len:
            return None
        raw = scanner.read_bytes(str_ptr + 0x14, n)
        txt = raw.decode("ascii")
    except Exception:
        return None
    return txt if all(32 <= ord(c) < 127 for c in txt) else None


class ParkNameReader:
    """Reads the loaded park's native name string (park-info +0x1E8) via the park-info vtable scan."""

    VTABLE_RVA = sig.PARKINFO_VTABLE_RVA
    NAME_OFF = sig.PARKINFO_NAME_OFF
    SCAN_COOLDOWN_S = 5.0
    # A valid-but-nameless cache is NOT proof there is no name: the live park-info is allocated at
    # world load, so a cache built at the main menu (template only) would hide it forever. Rescan in
    # that state too, just on a longer throttle - it is the steady state while a foreign/unnamed park
    # idles, and a full heap sweep every poll tick is exactly what the cache exists to avoid.
    NONE_RESCAN_S = 20.0

    def __init__(self, scanner):
        self.scanner = scanner
        self._cached = None                      # park-info instance addresses
        self._last_scan: Optional[float] = None
        self._last_val: Optional[str] = None

    def _target(self) -> Optional[int]:
        base = getattr(self.scanner, "module_base", None)
        return base + self.VTABLE_RVA if base else None

    def _name_at(self, addr: int, target: int) -> Optional[str]:
        """Park name at one instance IFF it still carries the park-info vtable; the static template
        instance (and an unnamed park) has a NULL name pointer -> None."""
        if self.scanner.read_qword(addr) != target:
            raise LookupError("stale instance")
        return read_refcounted_string(self.scanner, self.scanner.read_qword(addr + self.NAME_OFF))

    def _first_cached(self, target: int) -> Optional[str]:
        """First non-empty name over cached instances; raises (via _name_at) on a stale cache."""
        if not self._cached:
            raise LookupError("no cache")
        for a in self._cached:
            name = self._name_at(a, target)
            if name:
                return name
        return None

    def read(self) -> Optional[str]:
        """The loaded park's name, or None if unnamed / no zoo loaded / vtable unresolved."""
        target = self._target()
        if target is None:
            return None
        try:
            val = self._first_cached(target)
            if val:
                self._last_val = val
                return val
            cooldown = self.NONE_RESCAN_S      # valid cache, no name: live instance may be newer
        except Exception:
            cooldown = self.SCAN_COOLDOWN_S    # empty/stale cache: standard rescan throttle
        now = time.monotonic()
        if self._last_scan is not None and (now - self._last_scan) < cooldown:
            return self._last_val
        self._last_scan = now
        return self._rescan(target)

    def _rescan(self, target: int) -> Optional[str]:
        try:
            hits = self.scanner.scan_heap_for_qword(target, max_hits=64)
        except Exception:
            hits = []
        addrs, val = [], None
        for h in hits:
            try:
                name = self._name_at(h, target)
            except Exception:
                continue
            addrs.append(h)
            val = val or name
        self._cached = addrs or None
        self._last_val = val
        return val


class ApSessionDetector:
    """is_ap_session() = park-name marker present AND the exchange manager runs in scenario mode.

    ``mode_check`` is a zero-arg callable returning True iff the animal exchange is in scenario mode
    (SpeciesMarketGate.scenario_mode) - injected so this module stays decoupled from market internals.
    Logs every session-state transition once instead of spamming each poll tick.
    """

    def __init__(self, scanner, mode_check=None):
        self.names = ParkNameReader(scanner)
        self._mode_check = mode_check
        self._last_state: Optional[bool] = None

    def is_ap_session(self) -> bool:
        name = self.names.read()
        ok = name == AP_PARK_NAME
        if ok and self._mode_check is not None:
            ok = bool(self._mode_check())
        if ok != self._last_state:
            self._last_state = ok
            if ok:
                logger.info("AP session detected (park name %r, scenario mode) - client active.", name)
            else:
                logger.info("No AP session (park name %r) - client idle until the AP scenario loads. "
                            "If this IS the AP scenario, the Main.ovl in play predates the SetParkName "
                            "marker or the park-info vtable RVA is stale; set PZAP_NO_SESSION_GATE=1 "
                            "to bypass detection while debugging.", name)
        return ok
