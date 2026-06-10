"""RegistryResolver - read-only species-NAME -> symbol-ID resolver.

The animal-market gate (market.py) writes species symbol IDs into the exchange
manager's include-set. Those IDs are runtime interns in the global string-symbol
registry ``DAT_14298ae00`` - the same registry ``sSpecies`` names and the market
autofill (``FUN_140EA0740`` lines 119-120) resolve through. The IDs are dense small
ints assigned in intern order and are session-dynamic (they can differ across runs),
so the client must resolve names -> IDs live each session.

RE writeup: tools/_decomp/registry/REGISTRY_RE.md (decomps fn_14bfb22e0 hash,
fn_141342d50 lookup, fn_14bfaf2d0 id->entry, fn_14bfb16b0 AddRef).

Registry object layout (base = ``*(module_base + REGISTRY_GLOBAL_RVA)``):
  +0x10 (i64) entry-slot stride           +0x48 (i32) capacity (slot count)
  +0x30 (ptr) arena TOP; slots BELOW it   +0x9c (u32) bucket count (hash modulus)
  +0xa0 (u8)  case-fold flag              entry = ``*(top - id*stride) & ~1``
Entry record: +0x00 i32 refcount, +0x04 u32 bucket-link, +0x08 char[] NAME.

Strategy (REGISTRY_RE.md "RECOMMENDED CLIENT STRATEGY"): rather than replicate the
hash + tagged-pointer chain walk, **iterate id=1..cap, deref each slot, read the name
at +8, and build the whole name->id map once per session**. id->name is trivial and
IDs are dense, so this is simpler and more robust than hashing. Degrades to an empty
map (no raise) if the registry isn't resolvable (not attached / not in a loaded zoo).
"""

from __future__ import annotations

import logging
import struct
from typing import Dict, Optional

logger = logging.getLogger("PZClient")

IMAGE_BASE = 0x140000000
REGISTRY_GLOBAL_RVA = 0x14298AE00 - IMAGE_BASE   # 0x298AE00 - the global holding the registry ptr

# registry-object field offsets
OFF_STRIDE = 0x10
OFF_TOP = 0x30
OFF_CAP = 0x48
OFF_BUCKETS = 0x9C
OFF_CASEFOLD = 0xA0
# entry-record field offsets
ENT_REFCOUNT = 0x00
ENT_NAME = 0x08

# Guards: a sane registry has a small slot stride and a bounded capacity. Used to (a) pick the
# right base (the global is a pointer; validate the object it points at) and (b) bound iteration.
MAX_STRIDE = 0x100
MAX_CAP = 1_000_000
MAX_NAME_LEN = 128


class RegistryResolver:
    def __init__(self, scanner):
        self.scanner = scanner
        self._name2id: Optional[Dict[str, int]] = None
        self._base_cache: Optional[int] = None

    # -- registry-object location ---------------------------------------------

    def _looks_like_registry(self, base: int) -> bool:
        """Cheap structural sanity check: plausible stride / capacity / non-null arena top."""
        if not base:
            return False
        try:
            stride = self.scanner.read_i64(base + OFF_STRIDE)
            top = self.scanner.read_qword(base + OFF_TOP)
            cap = self.scanner.read_i32(base + OFF_CAP)
        except Exception:
            return False
        return (0 < stride <= MAX_STRIDE and bool(top) and 0 < cap <= MAX_CAP)

    def _registry_base(self) -> Optional[int]:
        """Resolve the registry object. ``DAT_14298ae00`` is a global POINTER to the lazily-allocated
        singleton, so the object = ``*(module_base + RVA)``; we validate the layout and, as a fallback
        for the (unexpected) inline case, also try the global's own address. Cached per process."""
        if self._base_cache is not None:
            return self._base_cache
        mb = getattr(self.scanner, "module_base", None)
        if not self.scanner.attached or mb is None:
            return None
        global_addr = mb + REGISTRY_GLOBAL_RVA
        for cand in (self.scanner.read_qword(global_addr), global_addr):
            if cand and self._looks_like_registry(cand):
                self._base_cache = cand
                return cand
        logger.warning("registry: could not validate the symbol registry @global 0x%X "
                       "(in a loaded zoo? RVA stale after a patch?)", global_addr)
        return None

    # -- id <-> name ----------------------------------------------------------

    def _read_entry(self, top: int, stride: int, sid: int) -> Optional[int]:
        """Resolve a symbol id to its entry pointer (``*(top - id*stride) & ~1``), or None if invalid."""
        try:
            slot = self.scanner.read_qword(top - sid * stride)
        except Exception:
            return None
        if not slot or (slot & 1):          # null / odd = empty or tagged-busy slot
            return None
        return slot & ~1

    def id_to_name(self, sid: int) -> Optional[str]:
        """Resolve a single symbol id to its interned name (no map build)."""
        base = self._registry_base()
        if base is None or sid <= 0:
            return None
        try:
            top = self.scanner.read_qword(base + OFF_TOP)
            stride = self.scanner.read_i64(base + OFF_STRIDE)
        except Exception:
            return None
        if not top or stride <= 0:
            return None
        entry = self._read_entry(top, stride, sid)
        if entry is None:
            return None
        return self._read_name(entry)

    def _read_name(self, entry: int) -> Optional[str]:
        """Read the NUL-terminated name at entry+8 (skipping refcount==0 holes)."""
        try:
            if self.scanner.read_i32(entry + ENT_REFCOUNT) == 0:
                return None
            raw = self.scanner.read_bytes(entry + ENT_NAME, MAX_NAME_LEN)
        except Exception:
            return None
        nul = raw.find(b"\x00")
        if nul <= 0:
            return None
        try:
            return raw[:nul].decode("ascii")
        except UnicodeDecodeError:
            return None

    def build_name_map(self, force: bool = False) -> Dict[str, int]:
        """Build (and cache) the full name->id map by iterating the registry slots once.

        Cached for the session; pass ``force=True`` to rebuild (e.g. after a reload). Returns an empty
        dict (no raise) if the registry can't be resolved, so callers degrade gracefully."""
        if self._name2id is not None and not force:
            return self._name2id
        base = self._registry_base()
        if base is None:
            return {}
        try:
            top = self.scanner.read_qword(base + OFF_TOP)
            stride = self.scanner.read_i64(base + OFF_STRIDE)
            cap = self.scanner.read_i32(base + OFF_CAP)
        except Exception:
            return {}
        if not top or stride <= 0 or not (0 < cap <= MAX_CAP):
            return {}
        out: Dict[str, int] = {}
        for sid in range(1, cap):
            entry = self._read_entry(top, stride, sid)
            if entry is None:
                continue
            name = self._read_name(entry)
            if name:
                out[name] = sid
        self._name2id = out
        logger.info("registry: resolved %d interned names (cap=%d)", len(out), cap)
        return out

    def name_to_id(self, name: str) -> Optional[int]:
        """Resolve one species/symbol name to its current-session id, or None if not interned."""
        return self.build_name_map().get(name)

    def resolve_many(self, names) -> Dict[str, int]:
        """Resolve an iterable of names to ids; silently omits any name not currently interned."""
        m = self.build_name_map()
        return {n: m[n] for n in names if n in m}

    def invalidate(self) -> None:
        """Drop the cached map + base (call on zoo reload / disconnect)."""
        self._name2id = None
        self._base_cache = None
