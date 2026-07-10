"""Game-free tests for the conservation_release per-species detection.

The detection is a two-detour design on ReleaseAnimalIntoWild (FUN_145D84690):
  * the ENTRY gate counts releases + gates the conservation program, and
  * a SECOND detour at the call-prep (entry+0xFF) captures the released animal's HANDLE (rsi) and
    the manager/zoo it's resolved through (*(rbp+0x48)).
The trigger resolves handle -> entity -> species via AnimalResolver (the same path births uses),
trying every captured manager/zoo source, and fires cr_<species>. EXHIBIT releases use their own
action (FUN_146048940): its entry gets the same gate+counter, PLACED releases attribute via the
+0x318 census diff, and STORAGE releases via a THIRD detour in the action's storage branch
(rva 0x6048A92) that captures the released animal id + the def-map holder H = *(mgr+0xF8) - the id
resolves to a species through the {animal_id -> def} map cache. These tests cover that plumbing
game-free: the trampoline bytes, the scratch accessors, the resolver's wrong-manager guard, and the
multi-source attribution + idempotent firing.
"""
from __future__ import annotations

import os
import struct
import sys
import types
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import hook, releases  # noqa: E402
from pz_ap_client.memory.animals import (  # noqa: E402
    AnimalResolver, OFF_MGR_HASHMAP, OFF_MAP_CAP, OFF_MAP_BUCKETS, OFF_MGR_TABLE,
    OFF_SPECIES_HANDLE, RECORD_STRIDE,
)
from pz_ap_client.memory.triggers import MemoryTriggerSource  # noqa: E402

RELEASE_SP_ORIG = bytes.fromhex("488b4d484889f2")  # mov rcx,[rbp+0x48] ; mov rdx,rsi


class FakeScanner:
    """Sparse keyed memory: mem[addr] -> bytes. read_bytes returns a prefix; read_qword unpacks 8."""
    def __init__(self, mem):
        self.mem = mem

    def read_bytes(self, addr, n):
        if addr in self.mem and len(self.mem[addr]) >= n:
            return self.mem[addr][:n]
        raise OSError("unmapped 0x%X" % addr)

    def read_qword(self, addr):
        return int.from_bytes(self.read_bytes(addr, 8), "little")


# ── trampolines ──────────────────────────────────────────────────────────────────────────────────
def test_release_gate_captures_manager():
    # The entry gate still counts + gates (rcx capture is legacy/harmless but asserted for stability).
    code = hook.make_release_gate(0x1000, 0x1000, 0x2000, releases.RELEASE_ORIG)
    assert b"\xFF\x00" in code                      # inc dword [rax] (release count)
    assert b"\x31\xC0\xC3" in code                  # LOCKED: xor eax,eax; ret (abort path)


def test_release_species_capture_trampoline():
    """The second detour: run mov rcx,[rbp+0x48] first, capture rsi (handle) + rcx (mgr), then
    mov rdx,rsi, then jmp back - so at resume rcx=[rbp+0x48], rdx=rsi are both set."""
    scratch = 0x33000000
    code = hook.make_release_species_capture(0x1000, scratch, 0x2000, RELEASE_SP_ORIG)
    assert code[:4] == RELEASE_SP_ORIG[:4]          # starts with mov rcx,[rbp+0x48]
    assert struct.pack("<Q", scratch) in code       # movabs rax, scratch
    assert b"\xFF\x00" in code                      # inc dword [rax]            (capture count)
    assert b"\x48\x89\x70\x08" in code              # mov [rax+8], rsi           (animal handle)
    assert b"\x48\x89\x48\x10" in code              # mov [rax+0x10], rcx        (*(rbp+0x48))
    assert RELEASE_SP_ORIG[4:] in code              # mov rdx,rsi (second half of original)
    assert code[-5] == 0xE9                         # ends with the jmp-back rel32
    # offsets the detector reads must match the trampoline's store offsets
    assert hook.RELEASE_SP_HANDLE == 0x08 and hook.RELEASE_SP_MGR == 0x10


EXR_ORIG = bytes.fromhex("4d8bbef8000000")   # mov r15,[r14+0xf8] (the storage-branch holder rebind)


def test_exhibit_release_capture_trampoline():
    """The exhibit STORAGE-branch capture: relocated `mov r15,[r14+0xf8]` FIRST (it loads r15 = H,
    the def-map holder), then the ring store of ebx (released animal id) + r14 (mgr) + r15 (H),
    framed by pushfq/push rax/push r11 so the resume path sees identical registers + flags."""
    scratch = 0x44000000
    code = hook.make_exhibit_release_capture(0x1000, scratch, 0x2000, EXR_ORIG)
    assert code.startswith(EXR_ORIG)                # original first (contiguous -> leak-check needs no frags)
    assert struct.pack("<Q", scratch) in code       # movabs rax, scratch
    assert b"\x42\x89\x5C\x98" + bytes([hook.EXR_RING_OFF]) in code  # mov [rax+r11*4+off], ebx (id ring)
    assert b"\x4C\x89\x70" + bytes([hook.EXR_MGR]) in code           # mov [rax+8], r14     (manager)
    assert b"\x4C\x89\x78" + bytes([hook.EXR_HOLDER]) in code        # mov [rax+0x10], r15  (holder H)
    assert code[-5] == 0xE9                         # ends with the jmp-back rel32
    # push/pop framing balanced: pushfq/rax/r11 ... r11/rax/popfq
    assert b"\x9C\x50\x41\x53" in code and b"\x41\x5B\x58\x9D" in code


def test_read_exhibit_release_events_drain():
    """The ring drain: returns only ids past the cursor, capped at the ring size."""
    scratch = 0x6000000
    ring = bytearray(hook.EXR_RING * 4)
    for i, aid in enumerate((0x501, 0x502, 0x503)):
        struct.pack_into("<I", ring, i * 4, aid)
    mem = {scratch: struct.pack("<I", 3), scratch + hook.EXR_RING_OFF: bytes(ring)}
    assert hook.read_exhibit_release_events(FakeScanner(mem), scratch, 0) == (3, [0x501, 0x502, 0x503])
    assert hook.read_exhibit_release_events(FakeScanner(mem), scratch, 1) == (3, [0x502, 0x503])
    assert hook.read_exhibit_release_events(FakeScanner(mem), scratch, 3) == (3, [])


# ── ReleaseDetector accessors ──────────────────────────────────────────────────────────────────────
def test_last_released_handle_and_manager():
    sp = 0x5000000
    mem = {
        sp + hook.RELEASE_SP_HANDLE: struct.pack("<Q", 0xBA9E),       # the released animal handle
        sp + hook.RELEASE_SP_MGR: struct.pack("<Q", 0x9126D10),       # *(rbp+0x48)
    }
    rd = releases.ReleaseDetector(FakeScanner(mem))
    rd.sp_scratch = sp
    assert rd.last_released_handle() == 0xBA9E
    assert rd.last_release_manager() == 0x9126D10


def test_release_accessors_none_without_capture():
    rd = releases.ReleaseDetector(FakeScanner({}))
    rd.sp_scratch = None                            # species capture not installed
    assert rd.last_released_handle() is None
    assert rd.last_release_manager() is None


def test_release_detector_exhibit_capture_accessors():
    """The storage-capture scratch accessors: drained ids, the recorded manager + holder."""
    sp = 0x7000000
    ring = bytearray(hook.EXR_RING * 4)
    struct.pack_into("<I", ring, 0, 0x501)
    mem = {
        sp: struct.pack("<I", 1),
        sp + hook.EXR_MGR: struct.pack("<Q", 0xAEA5950),
        sp + hook.EXR_HOLDER: struct.pack("<Q", 0xBEEF00),
        sp + hook.EXR_RING_OFF: bytes(ring),
    }
    rd = releases.ReleaseDetector(FakeScanner(mem))
    rd.exr_scratch = sp
    assert rd.exhibit_capture_mgr() == 0xAEA5950
    assert rd.exhibit_capture_holder() == 0xBEEF00
    assert rd.exhibit_release_events(0) == (1, [0x501])
    # not installed -> inert (no reads, cursor unchanged)
    rd2 = releases.ReleaseDetector(FakeScanner({}))
    rd2.exr_scratch = None
    assert rd2.exhibit_capture_mgr() is None
    assert rd2.exhibit_capture_holder() is None
    assert rd2.exhibit_release_events(5) == (5, [])


# ── AnimalResolver via-manager (the wrong-manager guard makes multi-source resolution safe) ─────────
def test_resolve_entity_via_manager_rejects_non_power_of_two_cap():
    """A wrong manager pointer reads a garbage cap (e.g. the 190886528 we saw live at *(rbp+0x48));
    the power-of-two guard must reject it -> None, never a false entity."""
    mgr = 0x300000
    mem = {
        mgr + OFF_MGR_HASHMAP + OFF_MAP_CAP: struct.pack("<Q", 190886528),  # not a power of two
        mgr + OFF_MGR_HASHMAP + OFF_MAP_BUCKETS: struct.pack("<Q", 0x1),
        mgr + OFF_MGR_TABLE: struct.pack("<Q", 0x1),
    }
    res = AnimalResolver(FakeScanner(mem))
    assert res.resolve_entity_via_manager(mgr, 0xBA9E) is None
    assert res.resolve_entity_via_manager(0, 0xBA9E) is None             # null mgr


def test_resolve_entity_via_manager_positive():
    """Construct a minimal valid roster hashmap (power-of-two cap) and resolve a handle through it."""
    mgr, buckets, table = 0x300000, 0x100000, 0x200000
    cap, key, idx = 16, 0xBA9E, 3
    slot = AnimalResolver._hash(key, cap)
    bitmap_sz = ((cap >> 3) + 7) & ~7
    blob = bytearray(bitmap_sz + cap * 0x10)
    struct.pack_into("<Q", blob, 0, 1 << slot)                          # bitmap word: occupy `slot`
    entry = bitmap_sz + slot * 0x10
    struct.pack_into("<Q", blob, entry, key)                            # stored key
    struct.pack_into("<I", blob, entry + 8, idx)                        # stored record index
    entity = table + idx * RECORD_STRIDE
    mem = {
        mgr + OFF_MGR_HASHMAP + OFF_MAP_CAP: struct.pack("<Q", cap),
        mgr + OFF_MGR_HASHMAP + OFF_MAP_BUCKETS: struct.pack("<Q", buckets),
        mgr + OFF_MGR_TABLE: struct.pack("<Q", table),
        buckets: bytes(blob),
        entity + OFF_SPECIES_HANDLE: struct.pack("<I", 0x30A2),
    }
    res = AnimalResolver(FakeScanner(mem))
    assert res.resolve_entity_via_manager(mgr, key) == entity
    assert res.species_handle(entity) == 0x30A2
    assert res.resolve_entity_via_manager(mgr, 0xDEAD) is None          # absent key -> None


def test_resolve_exhibit_defmap_holder():
    """H = *(mgr + 0xF8): the sub-object the release fn rebinds to before touching the owned-id set
    and the def map (missing this deref is the bug that killed the 2026-07-06 id-roster path)."""
    from pz_ap_client.memory.animals import OFF_EXHIBIT_DEF_HOLDER
    mgr = 0x400000
    res = AnimalResolver(FakeScanner({mgr + OFF_EXHIBIT_DEF_HOLDER: struct.pack("<Q", 0xBEEF00)}))
    assert res.resolve_exhibit_defmap_holder(mgr) == 0xBEEF00
    assert res.resolve_exhibit_defmap_holder(0) is None                 # no manager
    assert res.resolve_exhibit_defmap_holder(0x999999) is None          # unreadable -> None, no raise


# ── trigger attribution + firing ─────────────────────────────────────────────────────────────────
class _StubResolver:
    """resolve_entity matches only ZOO_GOOD; resolve_entity_via_manager only MGR_GOOD."""
    ZOO_GOOD, MGR_GOOD, ENTITY, HANDLE = 0x900000, 0x300000, 0xE000, 0xBA9E

    def resolve_entity(self, zoo, handle):
        return self.ENTITY if (zoo == self.ZOO_GOOD and handle == self.HANDLE) else None

    def resolve_entity_via_manager(self, mgr, handle):
        return self.ENTITY if (mgr == self.MGR_GOOD and handle == self.HANDLE) else None

    def species_handle(self, entity):
        return 0x30A2 if entity == self.ENTITY else None

    def resolve_exhibit_manager(self):
        return None   # no exhibit manager in the habitat-attribution tests -> exhibit path is a no-op


def _src(*, handle, mgr_cand, last_zoo, count=1, cache=None):
    return types.SimpleNamespace(
        releases=types.SimpleNamespace(
            count=lambda: count,
            habitat_count=lambda: count,
            exhibit_count=lambda: 0,
            last_released_handle=lambda: handle,
            last_release_manager=lambda: mgr_cand),
        births=types.SimpleNamespace(resolver=_StubResolver(), last_zoo=last_zoo,
                                     handle_species=cache or {}, sweep_roster=lambda: 0),
        research=types.SimpleNamespace(handle_key_map=lambda: {0x30A2: "aardvark"}),
        _released_species=set(), _last_habitat_count=0, _warned_release_attr=False,
        _last_exhibit_count=0, _exhibit_baseline=None, _pending_exhibit=0, _pending_exhibit_ticks=0)


def test_attribute_release_uses_insert_cache_first():
    # The race-free PRIMARY path: the births insert-cache attributes the handle with no live read.
    src = _src(handle=0xBA9E, mgr_cand=0, last_zoo=0, cache={0xBA9E: "zebra_plains"})
    assert MemoryTriggerSource._attribute_release(src) == "zebra_plains"


def test_attribute_release_via_manager_source():
    # FALLBACK: not cached -> *(rbp+0x48) is the animal manager (resolve_entity fails, via_manager wins).
    src = _src(handle=0xBA9E, mgr_cand=_StubResolver.MGR_GOOD, last_zoo=0)
    assert MemoryTriggerSource._attribute_release(src) == "aardvark"


def test_attribute_release_falls_back_to_births_zoo():
    # The release-site manager candidate resolves to nothing; the births-captured zoo saves it.
    src = _src(handle=0xBA9E, mgr_cand=0xBADBAD, last_zoo=_StubResolver.ZOO_GOOD)
    assert MemoryTriggerSource._attribute_release(src) == "aardvark"


def test_attribute_release_returns_none_when_all_sources_fail():
    src = _src(handle=0xBA9E, mgr_cand=0xBADBAD, last_zoo=0xC0FFEE)   # neither resolves
    assert MemoryTriggerSource._attribute_release(src) is None
    # no handle captured at all -> None
    src2 = _src(handle=None, mgr_cand=_StubResolver.MGR_GOOD, last_zoo=0)
    assert MemoryTriggerSource._attribute_release(src2) is None


_EXHIBIT_TOKEN_KEYS = {"GoliathBeetle": "gbeetle", "GiantDesertHairyScorpion": "gdscorpion"}


def _exhibit_src(census_handles, exhibit_count, ids=None, def_names=None, captures=None,
                 mgr=0xE0, holder=0xF8E0):
    """Build a MemoryTriggerSource (bypassing __init__) for _poll_exhibit_release: census_handles is
    the live {species_handle->count} the fake exhibit manager returns; exhibit_count is a callable ->
    current release-hook count. ids (a MUTABLE set the test edits, or None = id set unresolvable) +
    def_names ({animal_id: [name candidates] OR (species_handle, [names]) - a MUTABLE dict) drive
    the def map on the holder; captures (a MUTABLE list of released ids the test appends to) drives
    the storage-capture drain. mgr=0 makes the exhibit manager (and thus the placed census)
    unresolvable; holder=None the def-map holder."""
    src = MemoryTriggerSource.__new__(MemoryTriggerSource)

    def _defs(_h):
        return {a: (v if isinstance(v, tuple) else (0, list(v)))
                for a, v in (def_names or {}).items()}

    resolver = types.SimpleNamespace(
        resolve_exhibit_manager=lambda: mgr,
        resolve_exhibit_defmap_holder=lambda m: holder,
        read_exhibit_census=lambda m: dict(census_handles),
        read_exhibit_ids=lambda h: (set(ids) if ids is not None else None),
        read_exhibit_defs=_defs)
    cap = captures if captures is not None else []
    src.births = types.SimpleNamespace(resolver=resolver)
    src.research = types.SimpleNamespace(
        handle_key_map=lambda: {0x3033: "mtarantula", 0x3084: "gorilla"},
        species_key_for_name=lambda nm: _EXHIBIT_TOKEN_KEYS.get(nm))
    src.releases = types.SimpleNamespace(
        exhibit_count=exhibit_count,
        exhibit_capture_holder=lambda: None,
        exhibit_release_events=lambda cursor: (len(cap), list(cap[cursor:])))
    src._released_species = set()
    src._last_exhibit_count = 0
    src._exhibit_storage_hint_logged = False
    src._exhibit_baseline = None
    src._pending_exhibit = 0
    src._pending_exhibit_ticks = 0
    src._exhibit_prev_ids = None
    src._exhibit_id_species = {}
    src._exhibit_cap_cursor = 0
    src._exhibit_captured_done = set()
    src._exhibit_capture_unresolved = set()
    src._exhibit_roster_warned = False
    return src


def test_exhibit_release_attributed_by_census_diff():
    """An exhibit release is attributed when the census count drops, even though the drop LAGS the hook
    count (the release posts a deferred message): the pending release is held until the census reflects it."""
    census = {0x3033: 2}        # two tarantulas in exhibits
    ec = [0]                    # release-hook count (mutable)
    src = _exhibit_src(census, lambda: ec[0])
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                      # prime baseline (RAW handles); no fire
    assert src._exhibit_baseline == {0x3033: 2}
    assert src._released_species == set()
    ec[0] = 1                   # release detected by the hook, but census not yet decremented
    poll()
    assert src._pending_exhibit == 1
    assert src._released_species == set()        # held pending until the census drops
    census[0x3033] = 1          # deferred message processed: one tarantula gone
    poll()
    assert src._released_species == {"mtarantula"}   # handle 0x3033 mapped at attribution time
    assert src._pending_exhibit == 0
    assert src._exhibit_baseline == {0x3033: 1}


def test_exhibit_census_drop_without_release_not_attributed():
    """A death/transfer drops the census with NO release event - must not fire cr_ (the hook count
    is what distinguishes a release from a death)."""
    census = {0x3033: 1}
    src = _exhibit_src(census, lambda: 0)        # exhibit release count never rises
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                      # prime
    census[0x3033] = 0          # animal gone, but not released
    poll()
    assert src._released_species == set()


def test_exhibit_release_gives_up_when_no_mapped_drop():
    """A release is detected but no MAPPED species' census drops (e.g. the released species' handle isn't
    in the research map, or it's a storage animal absent from the placed census): give up after the tick
    budget so the baseline doesn't stick forever."""
    src = _exhibit_src({0x3033: 1}, lambda: 0)   # start with no releases
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                      # prime baseline {0x3033:1}; count 0 -> no pending
    assert src._pending_exhibit == 0
    # now a genuinely new release whose species' census never drops (placed census unchanged)
    src.releases.exhibit_count = lambda: 1
    from pz_ap_client.memory.triggers import EXHIBIT_GIVEUP_TICKS
    for _ in range(EXHIBIT_GIVEUP_TICKS + 1):
        poll()
    assert src._pending_exhibit == 0             # given up after the budget
    assert src._released_species == set()


def test_exhibit_release_detected_without_placed_census():
    """Nothing-resolvable zoo (no manager, no holder, no id set): a release must still be DETECTED
    (count) and, after the budget, hit the give-up diagnostic. Regression for the bug where reading
    the census first suppressed detection entirely."""
    ec = [0]
    src = _exhibit_src({}, lambda: ec[0], mgr=0, holder=None)
    poll = src._poll_exhibit_release
    poll()                       # census None; no release yet
    assert src._pending_exhibit == 0
    ec[0] = 1                     # release from storage -> hook count rises
    poll()
    assert src._pending_exhibit == 1, "release detected even though the placed census is None"
    from pz_ap_client.memory.triggers import EXHIBIT_GIVEUP_TICKS
    for _ in range(EXHIBIT_GIVEUP_TICKS):
        poll()
    assert src._pending_exhibit == 0, "give-up diagnostic ran and retired the pending release"


def test_exhibit_release_unmapped_handle_drop_consumes_pending_without_firing():
    """If the census drops for a handle NOT in the research map, the pending release is still consumed
    (logged, not stuck waiting) but no cr_ fires - so the diagnostic shows the exact handle to map."""
    census = {0x9999: 1}        # 0x9999 is not in the stub research map
    ec = [0]
    src = _exhibit_src(census, lambda: ec[0])
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                      # prime baseline {0x9999: 1}
    ec[0] = 1                   # release detected
    poll()                      # pending=1, census not yet dropped
    assert src._pending_exhibit == 1
    census[0x9999] = 0          # the unmapped species' census drops
    poll()
    assert src._pending_exhibit == 0             # consumed (not stuck -> no spurious give-up later)
    assert src._released_species == set()        # unmapped -> no cr_, but the drop was accounted


def test_exhibit_storage_release_attributed_by_id_roster_diff():
    """A release straight from STORAGE never drops the placed census - the id-roster diff attributes it:
    the owned-id set loses the id synchronously and the def-map species cache names it. The def object's
    string fields are matched against the token map, so a non-species string ('Fluffy') is skipped."""
    ids = {0x501, 0x502}
    def_names = {0x501: ["Fluffy", "GoliathBeetle"], 0x502: ["GiantDesertHairyScorpion"]}
    ec = [0]
    src = _exhibit_src({}, lambda: ec[0], ids=ids, def_names=def_names)   # EMPTY placed census (storage-only)
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                       # prime: both ids cached with their species
    assert src._exhibit_id_species == {0x501: "gbeetle", 0x502: "gdscorpion"}
    ec[0] = 1                    # release detected by the hook...
    ids.discard(0x501)           # ...and the id left the owned set synchronously
    poll()
    assert src._released_species == {"gbeetle"}, "storage release attributed via the id-roster diff"
    assert src._pending_exhibit == 0
    assert 0x501 not in src._exhibit_id_species, "released id evicted from the cache"


def test_exhibit_id_removal_without_release_not_attributed():
    """A death/sale removes the id with NO release event - must not fire cr_ (pairing with the hook
    count is what makes a removal a release), but the cache entry is still evicted."""
    ids = {0x501}
    src = _exhibit_src({}, lambda: 0, ids=ids, def_names={0x501: ["GoliathBeetle"]})
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                       # prime the id cache
    ids.clear()                  # animal gone, but not released
    poll()
    assert src._released_species == set()
    assert src._exhibit_id_species == {}, "non-release removal still evicts the cache entry"


def test_exhibit_storage_release_attributed_by_captured_id():
    """The PRIMARY storage path: the capture detour recorded the released animal id; the def-map
    cache (filled on the prime tick) names it - the fire comes from the capture drain, not the
    id-set diff. The engine removes the id from the owned set SYNCHRONOUSLY with the release
    (fn_146048940), so the sweep cannot re-cache the evicted id afterwards - model that here."""
    ids = {0x501, 0x502}
    captures = []
    def_names = {0x501: ["Fluffy", "GoliathBeetle"], 0x502: ["GiantDesertHairyScorpion"]}
    ec = [0]
    src = _exhibit_src({}, lambda: ec[0], ids=ids, def_names=def_names, captures=captures)
    poll = src._poll_exhibit_release
    poll()                       # prime: both ids cached with their species
    assert src._exhibit_id_species == {0x501: "gbeetle", 0x502: "gdscorpion"}
    ec[0] = 1                    # release detected by the hook...
    captures.append(0x501)       # ...the storage capture recorded the released id...
    ids.discard(0x501)           # ...and the engine dropped it from the owned set (synchronous)
    poll()
    assert src._released_species == {"gbeetle"}, "captured id attributed via the def-map cache"
    assert src._pending_exhibit == 0
    assert 0x501 not in src._exhibit_id_species, "attributed id evicted from the cache"


def test_exhibit_captured_id_resolves_via_fresh_defmap_read():
    """First release before any sweep (id set unresolvable -> cache empty): the drain falls back to
    a fresh def-map read through the resolvable holder and still attributes the captured id."""
    captures = []
    ec = [0]
    src = _exhibit_src({}, lambda: ec[0], ids=None, def_names={0x501: ["GoliathBeetle"]},
                       captures=captures)
    poll = src._poll_exhibit_release
    poll()
    assert src._exhibit_id_species == {}         # no id set -> nothing cached
    ec[0] = 1
    captures.append(0x501)
    poll()
    assert src._released_species == {"gbeetle"}, "fresh def-map read resolved the captured id"
    assert src._pending_exhibit == 0


def test_exhibit_storage_release_attributed_by_species_handle():
    """The live 2026-07-10 failure shape: the id set is dead (holder chain NULL all session) AND the
    def object's strings are ONLY given names / name-bank tokens - no species token. The species
    HANDLE @entry+0x30 through the research map must attribute the captured release anyway."""
    captures = []
    ec = [0]
    defs = {0x1043E: (0x3033, ["Amidio", "AnimalNames_Spanish_Male_00010"])}   # names unmappable
    src = _exhibit_src({}, lambda: ec[0], ids=None, def_names=defs, captures=captures)
    poll = src._poll_exhibit_release
    poll()
    ec[0] = 1
    captures.append(0x1043E)
    poll()
    assert src._released_species == {"mtarantula"}, "species handle resolved via the research map"
    assert src._pending_exhibit == 0


def test_exhibit_capture_retry_resolves_on_later_tick():
    """A captured id that resolves to NO species this tick (def map unreadable mid-load) is parked
    and retried while the release is pending - def entries survive the release, so a later tick can
    still attribute it instead of the old drop-on-first-miss."""
    captures = []
    ec = [0]
    defs = {}                     # def map empty/unreachable at capture time
    src = _exhibit_src({}, lambda: ec[0], ids=None, def_names=defs, captures=captures)
    poll = src._poll_exhibit_release
    poll()
    ec[0] = 1
    captures.append(0x1043E)
    poll()
    assert src._released_species == set()
    assert src._exhibit_capture_unresolved == {0x1043E}, "unresolved capture parked for retry"
    defs[0x1043E] = (0x3084, [])  # def map becomes readable on a later tick
    poll()
    assert src._released_species == {"gorilla"}, "retry attributed the parked capture"
    assert src._pending_exhibit == 0
    assert src._exhibit_capture_unresolved == set()


def test_exhibit_capture_retry_stops_when_pending_retires():
    """Give-up path: if the pending release retires unattributed, the parked capture is dropped too
    (nothing left for it to consume) - no eternal retry, no late false fire."""
    captures = []
    ec = [0]
    src = _exhibit_src({}, lambda: ec[0], ids=None, def_names={}, captures=captures)
    poll = src._poll_exhibit_release
    poll()
    ec[0] = 1
    captures.append(0x777)
    for _ in range(20):           # well past EXHIBIT_GIVEUP_TICKS
        poll()
    assert src._pending_exhibit == 0
    assert src._exhibit_capture_unresolved == set(), "parked capture cleared with the retired pending"
    assert src._released_species == set()


def test_exhibit_capture_and_id_removal_do_not_double_consume():
    """One storage release is seen by BOTH the capture drain and the id-set diff - it must consume
    exactly ONE pending release, so a second concurrent release stays attributable."""
    ids = {0x501, 0x502}
    captures = []
    def_names = {0x501: ["GoliathBeetle"], 0x502: ["GiantDesertHairyScorpion"]}
    ec = [0]
    src = _exhibit_src({}, lambda: ec[0], ids=ids, def_names=def_names, captures=captures)
    poll = src._poll_exhibit_release
    poll()                       # prime the id cache
    ec[0] = 2                    # TWO releases detected this tick...
    captures.append(0x501)       # ...but only 0x501's capture + id removal are visible yet
    ids.discard(0x501)
    poll()
    assert src._released_species == {"gbeetle"}
    assert src._pending_exhibit == 1, "the id-set removal of the already-captured id must not consume pending #2"
    captures.append(0x502)       # the second release becomes visible
    ids.discard(0x502)
    poll()
    assert src._released_species == {"gbeetle", "gdscorpion"}
    assert src._pending_exhibit == 0


def test_exhibit_capture_without_pending_does_not_fire():
    """A capture with no pending release (hook count never rose - e.g. the entry gate isn't
    installed) must not fire cr_: the count pairing is what makes a capture a release."""
    captures = [0x501]
    src = _exhibit_src({}, lambda: 0, ids=None, def_names={0x501: ["GoliathBeetle"]},
                       captures=captures)
    src._poll_exhibit_release()
    assert src._released_species == set()
    assert src._exhibit_cap_cursor == 1, "the capture was still drained (cursor advanced)"


def test_trigger_fires_cr_for_resolved_release():
    """_poll_conservation_release: a new release whose handle attributes to a species fires that
    species' cr_ location, once (idempotent against already-checked)."""
    cr_loc = types.SimpleNamespace(id=2500, trigger_args={"species_key": "aardvark"})
    src = _src(handle=0xBA9E, mgr_cand=_StubResolver.MGR_GOOD, last_zoo=0)
    src.game_data = types.SimpleNamespace(
        locations_by_trigger=lambda t: [cr_loc] if t == "conservation_release" else [])
    src._attribute_release = lambda: MemoryTriggerSource._attribute_release(src)  # real resolver
    fired = MemoryTriggerSource._poll_conservation_release(src, already=set())
    assert fired == [2500], "cr_aardvark fires once its release is attributed"
    assert MemoryTriggerSource._poll_conservation_release(src, already={2500}) == []


# ── zoo_rating milestone: displayed-star rounding (live bug 2026-07-08: a 5-star zoo read 4.98) ──────
def _milestone_src(rating_stars):
    """src for _poll_milestones with the 5 'Zoo Rating - N' locations and a stubbed zoo_rating anchor
    that returns `rating_stars` (the clamp01 float already scaled x5, as AnchorTable.read does)."""
    locs = [types.SimpleNamespace(id=2686 + n, trigger_args={"metric": "zoo_rating", "threshold": n})
            for n in range(1, 6)]
    src = types.SimpleNamespace(
        game_data=types.SimpleNamespace(
            locations_by_trigger=lambda t: locs if t == "milestone" else []),
        anchors=types.SimpleNamespace(read=lambda scanner, name: rating_stars),
        scanner=None)
    src._metric_value = lambda metric: MemoryTriggerSource._metric_value(src, metric)
    return src


def test_zoo_rating_5_fires_at_displayed_5_stars():
    """raw clamp01 maxes at 1.0 -> stars ~4.98 for a 5-star zoo; comparing the DISPLAYED (half-star-
    rounded) value fires 'Zoo Rating - 5' at 4.98, while 1..4 still fire."""
    src = _milestone_src(4.9808)   # the live value that missed
    fired = MemoryTriggerSource._poll_milestones(src, already=set())
    assert fired == [2687, 2688, 2689, 2690, 2691], "all five fire once 4.98 rounds to 5.0 stars"


def test_zoo_rating_below_half_star_does_not_overfire():
    """The rounding must not fire a higher rung early: 4.70 stars displays as 4.5, so rung 5 stays unlit."""
    src = _milestone_src(4.70)     # round(9.4)/2 = 4.5
    fired = MemoryTriggerSource._poll_milestones(src, already=set())
    assert fired == [2687, 2688, 2689, 2690], "1..4 fire; 5 does not (displays 4.5 stars)"


def _guest_milestone_src(guest_value):
    """src for _poll_milestones with the guest_count milestones and a stubbed guest_count anchor."""
    thresholds = [250, 500, 750, 1000, 1250, 2500]
    locs = [types.SimpleNamespace(id=2692 + i, trigger_args={"metric": "guest_count", "threshold": t})
            for i, t in enumerate(thresholds)]
    return types.SimpleNamespace(
        game_data=types.SimpleNamespace(
            locations_by_trigger=lambda t: locs if t == "milestone" else []),
        anchors=types.SimpleNamespace(read=lambda scanner, name: guest_value),
        scanner=None)


def test_guest_count_garbage_read_does_not_fire():
    """Regression (live 2026-07-08): a client connected before the zoo loaded read guest_count=1953393007
    (ASCII 'port'), >= every threshold, and fired ALL guest checks. An out-of-sane-range read must be
    treated as unresolved so nothing fires."""
    src = _guest_milestone_src(1953393007)   # the actual garbage value observed
    src._metric_value = lambda metric: MemoryTriggerSource._metric_value(src, metric)
    assert MemoryTriggerSource._poll_milestones(src, already=set()) == []


def test_guest_count_sane_read_fires_expected():
    """A plausible in-range guest count still fires the rungs it has passed."""
    src = _guest_milestone_src(800)          # passes 250/500/750, not 1000/1250/2500
    src._metric_value = lambda metric: MemoryTriggerSource._metric_value(src, metric)
    assert MemoryTriggerSource._poll_milestones(src, already=set()) == [2692, 2693, 2694]
