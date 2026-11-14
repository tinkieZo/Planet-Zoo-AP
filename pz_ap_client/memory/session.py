"""ApSessionDetector - decides whether the LOADED park is the AP zoo, so the client only gates/awards
inside AP worlds instead of whatever park happens to be loaded. Two kinds of AP zoo:

* SCENARIO - the AP career entry (park-name marker, below).
* CHALLENGE - a vanilla Challenge-mode zoo the player CLAIMED for the slot (client ``/pz_claim``): the
  client binds the park's native identity (name + whatever identity fields are located, see
  ParkIdentity) and the session is active only in the park matching that binding. No typed name.

How the scenario is recognised: the AP scenario script (ScenarioScripts.Scenario_AP_Script, which the scriptutils hijack activates
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
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

from pz_ap_client.memory import signatures as sig

logger = logging.getLogger("PZClient")

# The marker the AP scenario script plants via SetParkName at Init. Must match the careerdata entry's
# label (database.c0careerdata.lua) AND scenarioscripts.scenario_ap_script.lua - keep all three in sync.
AP_PARK_NAME = "ARCHIPELAGO ZOO"

# Park-info identity fields beyond the name (darwinworld GenerateMetadataTable persists exactly Name,
# Geome, ContinentEnum, Latitude, Longitude on the park-info object). field -> (type, offset); an offset
# of None = not located / not used, the field is simply not read or compared. "str" = refcounted string.
# Located live 2026-11-13 (Challenge zoo Temperate/Europe: continent 1 @+0x1F0, lat 49.0 @+0x1F4,
# lon 23.0 @+0x1F8, right after the name @+0x1E8). The geome is one of the two 'temperate' strings at
# +0x1D0/+0x1E0 (not disambiguated) - left out: the globe pin already pins the area, and a mis-picked
# string that the engine rewrites would silently un-match the claimed zoo.
# continent is ONE BYTE: the 3 bytes after it vary at runtime (live 2026-11-13: read 0 at first, 0x0676B0 at
# claim time -> an i32 read stored continent 108441601 and the claimed zoo never matched again).
IDENTITY_FIELDS = {
    "geome": ("str", None),
    "continent": ("u8", 0x1F0),
    "lat": ("f32", 0x1F4),
    "lon": ("f32", 0x1F8),
}
# The claim NONCE: /pz_claim adds a random 1..PIN_NONCE_MAX_ULPS float steps to the park's latitude (raw
# f32 bits), so the claimed zoo carries a unique invisible mark - at |lat| < 90 one step is <= 7.6e-6
# degrees, the max shift < 1 km on the globe. It tells the claimed zoo apart from a restart at the same
# spot with the same name, and from saves made BEFORE the claim. Floats are therefore compared EXACTLY
# (an f32 read as a Python float round-trips through JSON / data storage bit-exactly).
PIN_NONCE_MAX_ULPS = 1023


@dataclass(frozen=True)
class ParkIdentity:
    """What the engine persists about a park that can tell one zoo from another (no typed marker)."""
    name: str
    geome: Optional[str] = None
    continent: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    def to_binding(self) -> dict:
        """The JSON-safe binding stored for a claimed zoo (fields that weren't readable are left out)."""
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def matches(self, binding: Optional[dict]) -> bool:
        """True iff this park is the bound zoo: same name AND every other field present on BOTH sides
        equal (floats exactly - see PIN_NONCE_MAX_ULPS). A field the binding lacks (claimed by an older
        client) or this read lacks (unresolved) is not compared."""
        if not binding or binding.get("name") != self.name:
            return False
        for key in IDENTITY_FIELDS:
            mine, theirs = getattr(self, key), binding.get(key)
            if mine is None or theirs is None:
                continue
            if isinstance(mine, float):
                if mine != float(theirs):
                    return False
            elif mine != theirs:
                return False
        return True

    def describe(self) -> str:
        return f"'{self.name}'"


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
        self.last_addr: Optional[int] = None     # instance the last read() name came from

    def invalidate(self) -> None:
        """Forget cached instances, the last value, AND the scan throttle. Call on park unload: freed
        park-info memory usually stays committed and intact, so the cached instances can ghost-read the
        OLD park's name indefinitely - and read() never rescans while a cached name still 'works'. With
        the throttle cleared, the tick after a reload rescans immediately and redetects the park."""
        self._cached = None
        self._last_val = None
        self._last_scan = None
        self.last_addr = None

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
                self.last_addr = a
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
        addrs, val, val_addr = [], None, None
        for h in hits:
            try:
                name = self._name_at(h, target)
            except Exception:
                continue
            addrs.append(h)
            if name and not val:
                val, val_addr = name, h
        self._cached = addrs or None
        self._last_val = val
        self.last_addr = val_addr
        return val

    def read_identity(self) -> Optional[ParkIdentity]:
        """The loaded park's ParkIdentity (name + every located IDENTITY_FIELDS entry), read off the
        same park-info instance the name came from; None if no named park is loaded."""
        name = self.read()
        target = self._target()
        if not name or target is None:
            return None
        for addr in self._cached or ():
            try:
                if self._name_at(addr, target) != name:
                    continue
                fields = {k: self._read_field(addr, typ, off)
                          for k, (typ, off) in IDENTITY_FIELDS.items() if off is not None}
            except Exception:
                continue
            return ParkIdentity(name=name, **fields)
        return ParkIdentity(name=name)

    def stamp_pin_nonce(self, ulps: int) -> Optional[ParkIdentity]:
        """Write the claim nonce: add ``ulps`` float steps to the loaded park's latitude (raw f32 bits,
        so the result is exact and never NaN/inf for |lat| <= 90). Returns the re-read identity, or None
        if the latitude isn't located or the park-info can't be resolved (claim then binds without it)."""
        _typ, off = IDENTITY_FIELDS["lat"]
        name = self.read()
        target = self._target()
        if off is None or not name or target is None:
            return None
        for addr in self._cached or ():
            try:
                if self._name_at(addr, target) != name:
                    continue
                bits = struct.unpack("<I", self.scanner.read_bytes(addr + off, 4))[0]
                self.scanner.write_bytes(addr + off, struct.pack("<I", (bits + ulps) & 0xFFFFFFFF))
            except Exception:
                continue
            return self.read_identity()
        return None

    def _read_field(self, addr: int, typ: str, off: int):
        if typ == "str":
            return read_refcounted_string(self.scanner, self.scanner.read_qword(addr + off))
        if typ == "u8":
            return self.scanner.read_bytes(addr + off, 1)[0]
        raw = self.scanner.read_bytes(addr + off, 4)
        return struct.unpack("<i" if typ == "i32" else "<f", raw)[0]


_UNSET = object()


class ApSessionDetector:
    """is_ap_session() = the loaded park is an AP zoo, of either kind (see ``kind``):

    * scenario  - park-name marker present AND the market runs in scenario mode;
    * challenge - the market runs in challenge mode (not franchise) AND ``claim_match(identity)`` - the
      client's check against the zoo the player claimed for this slot.

    ``game_mode`` is a zero-arg callable returning the park's mode ('scenario' | 'challenge' |
    'franchise' | 'sandbox' | 'off' | None; SpeciesMarketGate.game_mode) - injected so this module stays
    decoupled from market internals. ``mode_check`` (bool: scenario mode?) is the older scenario-only
    form, kept for callers that only know scenarios. Logs every session-state transition once instead of
    spamming each poll tick. After each call, ``mode`` and ``identity`` hold what was read, so the client
    can prompt a claim for an unbound challenge park.
    """

    def __init__(self, scanner, mode_check=None,
                 game_mode: "Optional[Callable[[], Optional[str]]]" = None,
                 claim_match: "Optional[Callable[[ParkIdentity], bool]]" = None):
        self.names = ParkNameReader(scanner)
        if game_mode is None and mode_check is not None:
            game_mode = lambda: "scenario" if mode_check() else None  # noqa: E731
        self._game_mode = game_mode
        self._claim_match = claim_match
        self._last_state = _UNSET
        self.kind: Optional[str] = None                 # active session kind, None when idle
        self.mode: Optional[str] = None                 # last read game mode
        self.identity: Optional[ParkIdentity] = None    # last read identity of a challenge park
        # The claimed Challenge zoo was renamed in-game and is still loaded: its new identity, for the
        # client to re-bind the claim to (None otherwise). Set by is_ap_session().
        self.renamed: Optional[ParkIdentity] = None
        # The park the session is active in: (mode, park-info instance, globe pin, name), else None.
        self._active: Optional[tuple] = None

    def _same_park(self, mode, name, addr, pin) -> bool:
        """The park the session is active in is still the one loaded, possibly RENAMED in-game: same game
        mode, same park-info instance, same globe pin. Loading another park passes through a no-zoo tick
        (mode None -> session idle -> latch cleared), so a new name on the latched instance is a rename -
        live 2026-11-13 (beta report): renaming the AP zoo turned the client off mid-session, which
        reopened the full market and dropped every check until the next load."""
        a = self._active
        return a is not None and bool(name) and addr is not None and (mode, addr, pin) == a[:3]

    def is_ap_session(self) -> bool:
        name = self.names.read()
        addr = getattr(self.names, "last_addr", None)
        mode = self._game_mode() if self._game_mode is not None else "scenario"
        identity = self.names.read_identity() if name and mode in ("scenario", "challenge") else None
        pin = (identity.continent, identity.lat, identity.lon) if identity is not None else None
        same = self._same_park(mode, name, addr, pin)
        kind = self._classify(mode, name, identity, same)
        if kind is not None and same and name != self._active[3]:
            logger.info("The AP zoo was renamed to %r - it is still tracked as the AP zoo%s.", name,
                        " (the scenario names it %r again on its next load)" % AP_PARK_NAME
                        if kind == "scenario" else "; updating this slot's claim to the new name")
        self._active = (mode, addr, pin, name) if kind is not None else None
        self.mode, self.identity, self.kind = mode, (identity if mode == "challenge" else None), kind
        if kind != self._last_state:
            self._last_state = kind
            self._log_transition(kind, name, mode, identity)
        return kind is not None

    def _classify(self, mode, name, identity, same) -> Optional[str]:
        """Session kind for this read ('scenario' | 'challenge' | None); sets ``renamed`` when the claimed
        Challenge zoo is only still recognised because it is the same (renamed) park."""
        self.renamed = None
        if mode == "scenario":
            return "scenario" if name == AP_PARK_NAME or same else None
        if mode != "challenge" or identity is None:
            return None
        if self._claim_match is not None and self._claim_match(identity):
            return "challenge"
        if same:
            self.renamed = identity
            return "challenge"
        return None

    @staticmethod
    def _log_transition(kind, name, mode, identity) -> None:
        if kind == "scenario":
            logger.info("AP session detected (park name %r, scenario mode) - client active.", name)
        elif kind == "challenge":
            logger.info("AP session detected (claimed Challenge zoo %s) - client active.", identity.describe())
        else:
            logger.info("No AP session (park name %r, mode %s) - client idle until the AP zoo loads: "
                        "the AP career scenario, or a Challenge zoo claimed with /pz_claim. If this IS "
                        "the AP scenario, the Main.ovl in play predates the SetParkName marker or the "
                        "park-info vtable RVA is stale; set PZAP_NO_SESSION_GATE=1 to bypass detection "
                        "while debugging.", name, mode)
