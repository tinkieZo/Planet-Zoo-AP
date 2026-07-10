"""ExhibitEnrichmentGate - native exhibit-enrichment tier gate via Lua bytecode patch.

The exhibit panel's per-tier availability is NOT the rs+0x148 unlockable byte (fully cosmetic for
exhibits, proven live 2026-07-08/10) but the native per-species map at *(exhibitsSubsys+0x370)+0x2F8
(rb-tree, species-token -> vector of unlocked levels), queried via the world-API
``GetUnlockedSpeciesEnrichmentLevels``. We can't insert rb-tree nodes from outside, so we patch the
CONSUMERS in ExhibitInfoPopUp.lua's loaded bytecode - TWO sites, both keying off that same map:

* SITE A ``main.54`` (_GenerateEnrichmentLevelsUI's level loop, pc 44-48) decides items-shown vs
  "NOT YET RESEARCHED":  ``bIsUnlocked = unlockedEnrichmentLevels[unlockLevel] ~= nil``  becomes
  ``bIsUnlocked = (unlockLevel <= N)``, N = received Progressive Exhibit Enrichment count.
* SITE B ``main.32`` (the interior-tab refresh, pc 86-88) populates each level's ``selectedIDs``
  (which toggles render CHECKED, sourced from ``GetActiveExhibitEnrichments``) but breaks at the
  first level missing from the map:  ``if map[level] == nil then break``  becomes
  ``if not (unlockLevel <= N) then break``. Without B, applied items render unchecked on every
  panel reopen while their effect persists (the live 2026-07-10 symptom).

The native APPLY path does not re-check the unlock map (live-proven: toggling an item on a
patch-unlocked tier spawns it and raises the welfare/layout rating), so gating the UI consumers is
the full enforcement. Both functions re-run every time the panel is (re)opened, so the patch is
semi-live. Heap code arrays are byte-identical to the ovl chunk, so the constants below are sourced
from ``components.ui.exhibitinfopopup.lua.bin``. Same ``set_gated`` / ``reconcile`` / ``shutdown``
shape as TerrainGate (see terrain.py); degrades to a no-op if the bytecode can't be located (it is
not loaded until an exhibit info panel has been opened once).

Prototyped + live-validated by tools/exhibit_enrich_patch_probe.py (Eastern Brown Snake, set=1).
"""

from __future__ import annotations

import logging
import struct
from typing import Dict, List, Optional

from .terrain import writable_regions

logger = logging.getLogger("PZClient")

# main.54's full 56-instruction code array (ExhibitInfoPopUp.lua).
MAIN54_CODE = bytes.fromhex(
    "460040004740c0008600400087804001c7c0400001010100a400800164400000460040004780c000"
    "87c04000c1000100648080018600400087404101c00080000181010041c10100a44000028b000000"
    "c1000200074142001c01000241010200e8800280c7814200c7c1c203cc01c3034742420047828104"
    "e48180011c0280035c020001200082041e00008080008003e7c0fc7fc50080000781420007c14202"
    "0c41430224010001e40001001e00028007c201011f8043041e00008003420000030280004cc24300"
    "c00280030003000464420002e98000006a01fd7f26008000"
)
# main.32's full 138-instruction code array (same chunk).
MAIN32_CODE = bytes.fromhex(
    "470040001f40c0001e0000802600800047804000624000001e00018047c040004700c10062400000"
    "1e000080414001008780410087400001c7c04100c700c201cc40c20147814200e480800106c14200"
    "070143024741430081810300c1c103002481000247c140005c0180026100c1021e00008043410000"
    "4301800086c1420087014403c00100020142040041820400620100001e80008087c24401a2420000"
    "1e00008087024501c14205005dc28204a441000286c1420087014403c00100020182050062010000"
    "1e80008047c24501624200001e00008047024601a441000286c1420087014303c741430001820300"
    "41420600a4810002c6c14200c701c403000200034182060087c2c601e4410002c7c14100c701c203"
    "cc01c70347824200e481800107c24100070242040c42470480028000248280014302800085028000"
    "c7c24100c702c205cc82c705e4020001a40201001ec00b80c78303045f40c0071e800a80c6c34200"
    "c703c3070744430041c4070081040800c00400079dc40409c1440800e48380020504800047c44100"
    "4704c2084c84c808c00480000005000764040002240401001e40058046c542004705c30a80058007"
    "c1c508000706490add05860b6485800186c542008705440bc005800a014609004706490a47468603"
    "5f40c00c1e00018041c608008706490a5d86860c624600001e00008041460100a445000229840000"
    "aac4f97f1e400080430200001e400080a98200002a43f37f0ac0499326008000"
)

MAX_LEVEL = 3    # exhibit enrichment tiers are 1..3
_FIND_COOLDOWN = 8  # reconciles to skip after a failed find (bytecode absent until a panel opened)


def _abc(op: int, a: int, b: int, c: int) -> int:
    return op | ((a & 0xFF) << 6) | ((c & 0x1FF) << 14) | ((b & 0x1FF) << 23)


def _jmp(sbx: int) -> int:
    return 30 | ((sbx + 0x1FFFF) & 0x3FFFF) << 14


def _pack(ins: "List[int]") -> bytes:
    return b"".join(struct.pack("<I", x) for x in ins)


def variant54(n: int) -> bytes:
    """5 replacement instructions computing R8 = (R7 <= n). R7=unlockLevel, R8=bIsUnlocked,
    K8=constant 1 (RK 0x108), continuation at pc49."""
    if n <= 0:      # all locked
        ins = [_abc(3, 8, 0, 0), _jmp(0), _jmp(0), _jmp(0), _jmp(0)]
    elif n == 1:    # R8 = (R7 == 1)
        ins = [_abc(31, 1, 7, 0x108),    # EQ 1 R7 K8      ; != -> skip JMP
               _jmp(1),                  # -> true branch (pc47)
               _abc(3, 8, 0, 1),         # LOADBOOL R8,false, skip next
               _abc(3, 8, 1, 0),         # LOADBOOL R8,true
               _jmp(0)]                  # pad -> pc49
    elif n == 2:    # R8 = 1+1; R8 = (R7 <= R8)
        ins = [_abc(13, 8, 0x108, 0x108),  # ADD R8 = K8+K8 = 2
               _abc(33, 1, 7, 8),          # LE 1 R7 R8    ; false -> skip JMP
               _jmp(1),                    # -> true branch (pc48)
               _abc(3, 8, 0, 1),           # LOADBOOL R8,false, skip next
               _abc(3, 8, 1, 0)]           # LOADBOOL R8,true
    else:           # n >= 3: all levels unlocked
        ins = [_abc(3, 8, 1, 0), _jmp(0), _jmp(0), _jmp(0), _jmp(0)]
    return _pack(ins)


def variant32(n: int) -> bytes:
    """3 replacement instructions for main.32 pc86-88 (orig: R15=map[R14]; ==nil -> JMP 132).
    R14=unlockLevel, R15 free (orig map value, only consumed by the removed EQ), K4=constant 1
    (RK 0x104). Locked path = JMP to pc132 (allUnlocked=false + loop break), continue = pc89."""
    if n <= 0:      # all locked: unconditional -> 132
        ins = [_jmp(45), _jmp(0), _jmp(0)]
    elif n == 1:    # R15 = 1
        ins = [_abc(1, 15, 0, 4),          # LOADK R15 = K4 = 1
               _abc(33, 0, 14, 15),        # LE 0 R14 R15  ; <= -> skip JMP -> pc89
               _jmp(43)]                   # -> locked path (pc132)
    elif n == 2:    # R15 = 1+1
        ins = [_abc(13, 15, 0x104, 0x104),  # ADD R15 = K4+K4 = 2
               _abc(33, 0, 14, 15),         # LE 0 R14 R15  ; <= -> skip JMP -> pc89
               _jmp(43)]                    # -> locked path (pc132)
    else:           # n >= 3: all levels unlocked: fall straight through to pc89
        ins = [_jmp(2), _jmp(0), _jmp(0)]
    return _pack(ins)


class Site:
    """One patch window in one function's code array."""

    def __init__(self, name: str, code: bytes, patch_pc: int, patch_nins: int, variant) -> None:
        self.name = name
        self.off = patch_pc * 4
        self.len = patch_nins * 4
        self.orig = code[self.off:self.off + self.len]
        self.prefix = code[:self.off]   # never patched -> stable find/cache-validate signature
        self.variant = variant
        # every byte pattern we may legitimately find in the window (safety: never overwrite unknowns)
        self.known = {self.orig} | {variant(n) for n in range(0, MAX_LEVEL + 1)}


SITES = [
    Site("main.54", MAIN54_CODE, 44, 5, variant54),
    Site("main.32", MAIN32_CODE, 86, 3, variant32),
]


class ExhibitEnrichmentGate:
    """Native exhibit-enrichment tier gate by patching ExhibitInfoPopUp's loaded Lua bytecode.
    Driven from the received Progressive Exhibit Enrichment count each tick (authoritative +
    idempotent, restart-correct)."""

    def __init__(self, scanner):
        self.scanner = scanner
        self.enabled = False
        self._addrs: Dict[str, List[int]] = {}   # site name -> located copies of its code array
        self._applied: Optional[int] = None       # last count written (idempotency)
        self._cooldown = 0
        self._first_scan_pending = True           # defer the first full-heap scan off the first poll tick

    def set_gated(self, enabled: bool) -> None:
        """Declare whether this seed carries the Progressive Exhibit Enrichment item (only then
        is the bytecode ever patched)."""
        self.enabled = bool(enabled)
        self._applied = None

    # -- bytecode location -----------------------------------------------------

    def _cache_valid(self) -> bool:
        """Cheap check that the cached addresses still hold the code arrays (a reload moves them)."""
        if not self._addrs or not all(self._addrs.get(s.name) for s in SITES):
            return False
        try:
            return all(self.scanner.read_bytes(a, 8) == site.prefix[:8]
                       for site in SITES for a in self._addrs[site.name])
        except Exception:
            return False

    def _find(self) -> Dict[str, List[int]]:
        """Every heap copy of each site's code array in ONE writable-region sweep (matched on the
        stable prefix, i.e. found whether the window currently holds ORIG or one of our variants)."""
        hits: Dict[str, List[int]] = {site.name: [] for site in SITES}
        for rb, rs in writable_regions(self.scanner):
            try:
                data = self.scanner.read_bytes(rb, rs)
            except Exception:
                continue
            for site in SITES:
                i = data.find(site.prefix)
                while i != -1:
                    hits[site.name].append(rb + i)
                    i = data.find(site.prefix, i + 1)
        return hits

    def _ensure_located(self) -> bool:
        if self._cache_valid():
            return True
        if self._first_scan_pending:
            # Keep the first full-heap sweep off the first poll tick (same defer as TerrainGate -
            # see terrain.py). The gate is invisible for that tick: the bytecode isn't even loaded
            # until the player opens an exhibit info panel.
            self._first_scan_pending = False
            return False
        if self._cooldown > 0:
            self._cooldown -= 1
            return False
        hits = self._find()
        self._applied = None
        if all(hits[s.name] for s in SITES):
            self._addrs = hits
            logger.info("exhibit-enrichment gate: located bytecode %s",
                        {k: [hex(a) for a in v] for k, v in hits.items()})
            return True
        self._addrs = {}
        if any(hits[s.name] for s in SITES):
            # both arrays live in the same chunk, so a partial find means a stale/foreign copy
            logger.warning("exhibit-enrichment gate: PARTIAL find %s - not patching",
                           {k: len(v) for k, v in hits.items()})
        self._cooldown = _FIND_COOLDOWN
        return False

    # -- patch / reconcile -----------------------------------------------------

    def reconcile(self, count: int) -> bool:
        """Authoritative gate sync: exhibit-enrichment tiers <= count read as unlocked for every
        exhibit species (shown + toggle-state populated), higher tiers as NOT YET RESEARCHED.
        Idempotent; only writes on change. Returns True if the bytecode is patched/located."""
        if not self.enabled:
            return True
        if not self._ensure_located():
            return False
        n = max(0, min(MAX_LEVEL, int(count)))
        if self._applied == n:
            return True
        for site in SITES:
            blob = site.variant(n)
            for a in self._addrs[site.name]:
                try:
                    cur = self.scanner.read_bytes(a + site.off, site.len)
                    if cur not in site.known:
                        logger.warning("exhibit-enrichment gate: %s @0x%X window unexpected - not touching",
                                       site.name, a)
                        continue
                    self.scanner.write_bytes(a + site.off, blob)
                except Exception as e:
                    logger.warning("exhibit-enrichment gate: write %s @0x%X failed: %s", site.name, a, e)
        self._applied = n
        logger.info("exhibit-enrichment gate: levels <= %d of %d unlocked", n, MAX_LEVEL)
        return True

    def shutdown(self) -> None:
        """Restore the original bytecode at every located copy (leave the game clean on disconnect)."""
        if self._cache_valid():
            for site in SITES:
                for a in self._addrs[site.name]:
                    try:
                        cur = self.scanner.read_bytes(a + site.off, site.len)
                        if cur in site.known and cur != site.orig:
                            self.scanner.write_bytes(a + site.off, site.orig)
                    except Exception:
                        pass
        self._addrs = {}
        self._applied = None
