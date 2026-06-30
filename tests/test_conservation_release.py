"""Game-free tests for the conservation_release per-species detection.

The detection is a two-detour design on ReleaseAnimalIntoWild (FUN_145D84690):
  * the ENTRY gate counts releases + gates the conservation program, and
  * a SECOND detour at the call-prep (entry+0xFF) captures the released animal's HANDLE (rsi) and
    the manager/zoo it's resolved through (*(rbp+0x48)).
The trigger resolves handle -> entity -> species via AnimalResolver (the same path births uses),
trying every captured manager/zoo source, and fires cr_<species>. These tests cover that plumbing
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


def _exhibit_src(census_handles, exhibit_count):
    """Build a src for _poll_exhibit_release: census_handles is the live {species_handle->count} the fake
    exhibit manager returns; exhibit_count is a callable -> current release-hook count."""
    resolver = types.SimpleNamespace(
        resolve_exhibit_manager=lambda: 0xE0,
        read_exhibit_census=lambda mgr: dict(census_handles),
        read_exhibit_population=lambda mgr: sum(census_handles.values()),
        scan_exhibit_census_candidates=lambda handle_set: iter(()))
    src = types.SimpleNamespace(
        births=types.SimpleNamespace(resolver=resolver),
        research=types.SimpleNamespace(handle_key_map=lambda: {0x3033: "mtarantula", 0x3084: "gorilla"}),
        releases=types.SimpleNamespace(exhibit_count=exhibit_count),
        _released_species=set(), _last_exhibit_count=0,
        _exhibit_baseline=None, _pending_exhibit=0, _pending_exhibit_ticks=0)
    src._read_exhibit_census = lambda: MemoryTriggerSource._read_exhibit_census(src)
    src._attribute_exhibit_drops = lambda raw: MemoryTriggerSource._attribute_exhibit_drops(src, raw)
    src._exhibit_giveup_diag = lambda raw: MemoryTriggerSource._exhibit_giveup_diag(src, raw)
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
    in the research map): give up after the tick budget so the baseline doesn't stick forever."""
    src = _exhibit_src({0x3033: 1}, lambda: 1)   # count says a release happened
    poll = lambda: MemoryTriggerSource._poll_exhibit_release(src)
    poll()                      # prime (adopts ecount=1, so no pending yet)
    assert src._pending_exhibit == 0
    # now a genuinely new release whose species' census never drops (census for mapped species unchanged)
    src.releases.exhibit_count = lambda: 2
    from pz_ap_client.memory.triggers import EXHIBIT_GIVEUP_TICKS
    for _ in range(EXHIBIT_GIVEUP_TICKS + 1):
        poll()
    assert src._pending_exhibit == 0             # given up after the budget
    assert src._released_species == set()


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
