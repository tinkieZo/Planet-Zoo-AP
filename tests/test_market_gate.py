"""Game-free unit tests for the animal-market species gate (market.py) + the registry
resolver (registry.py) + the scenario-schedule spawner (the Goodwin House hijack).
Exercises the pure data-structure layer: the int32 hash-set build is validated by
replicating the game's own lookup (FUN_1444E1A90) over the blob, the registry iteration
against a synthetic registry image, and the spawner against a synthetic schedule array.

Run:  python -m tests.test_market_gate
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory import market as mk  # noqa: E402
from pz_ap_client.memory.market import build_int32_set, set_hash, SET_BUFFER, SET_CAP  # noqa: E402
from pz_ap_client.memory.registry import (RegistryResolver, REGISTRY_GLOBAL_RVA,  # noqa: E402
                                          OFF_STRIDE, OFF_TOP, OFF_CAP, ENT_NAME)


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


class FakeMem:
    """Sparse byte-addressable memory with typed reads, mirroring MemoryScanner's API.
    Writes are logged (addr, size) in order so tests can assert write SEQUENCE."""
    def __init__(self, module_base: int = 0x140000000):
        self.module_base = module_base
        self.attached = True
        self._b: dict = {}
        self.write_log: list = []

    def write_bytes(self, addr: int, data: bytes) -> None:
        self.write_log.append((addr, len(data)))
        for i, byte in enumerate(data):
            self._b[addr + i] = byte

    def write_i32(self, addr: int, value: int) -> None:
        self.write_bytes(addr, struct.pack("<i", value))

    def read_bytes(self, addr: int, size: int) -> bytes:
        return bytes(self._b.get(addr + i, 0) for i in range(size))

    def read_qword(self, addr):
        try:
            return struct.unpack("<Q", self.read_bytes(addr, 8))[0]
        except Exception:
            return None

    def read_i64(self, addr):
        return struct.unpack("<q", self.read_bytes(addr, 8))[0]

    def read_i32(self, addr):
        return struct.unpack("<i", self.read_bytes(addr, 4))[0]


def _game_lookup(blob: bytes, cap: int, key: int) -> bool:
    """Replicate FUN_1444E1A90 over a freshly-built set blob: True iff ``key`` is a member.

    blob = 64-bit-word occupancy bitvector (cap bits) then int32 keys[cap]; probe stops at the
    first clear occupancy bit (a miss). This is the exact contract market.build_int32_set targets."""
    bitvec_bytes = ((cap >> 3) + 7) & ~7
    keys_off = bitvec_bytes
    slot = set_hash(key, cap)
    start = slot
    while True:
        word = struct.unpack_from("<Q", blob, (slot >> 6) * 8)[0]
        if not ((word >> (slot & 0x3F)) & 1):
            return False  # clear occupancy bit ends the probe -> not present
        k = struct.unpack_from("<i", blob, keys_off + slot * 4)[0]
        if k == key:
            return True
        slot = (slot + 1) % cap
        if slot == start:
            return False


def test_hash_set() -> None:
    present = [0x309F, 0x46DA, 0x3084, 0x3096, 0x640, 0x1, 0xFFFF, 0xABCDE]
    cap, blob = build_int32_set(present)
    _check(cap & (cap - 1) == 0, "set capacity is a power of two")
    _check(cap >= len(present), "capacity >= element count")
    bitvec_bytes = ((cap >> 3) + 7) & ~7
    _check(len(blob) == bitvec_bytes + cap * 4, "blob = bitvector + int32 keys[cap]")
    for k in present:
        _check(_game_lookup(blob, cap, k), f"present key 0x{k:X} found by game-lookup")
    for k in [0x111, 0x999, 0x46DB, 0x30A0, 0xDEAD]:
        _check(not _game_lookup(blob, cap, k), f"absent key 0x{k:X} not found")
    # de-dup: repeated keys don't bloat or break membership
    cap2, blob2 = build_int32_set([7, 7, 7, 9, 9])
    _check(_game_lookup(blob2, cap2, 7) and _game_lookup(blob2, cap2, 9), "dedup keeps membership")
    _check(not _game_lookup(blob2, cap2, 8), "dedup set excludes non-member")
    # empty set: builds, nothing present
    cap3, blob3 = build_int32_set([])
    _check(not _game_lookup(blob3, cap3, 1), "empty set has no members")


def _build_registry_mem() -> "tuple[FakeMem, dict]":
    """Synthetic symbol registry: a global ptr -> object {stride@0x10, top@0x30, cap@0x48}, with a
    downward pointer table of entries {refcount@0, link@4, name@8}. Includes a valid set of named
    entries plus a refcount-0 hole and an odd-tagged slot that must both be skipped."""
    m = FakeMem()
    obj = 0x200000000
    arena = 0x300000000
    stride = 8
    top = arena + 0x10000          # entries live BELOW top: slot = top - id*stride
    names = {1: "CommonWarthog", 2: "PlainsZebra", 3: "GiantPanda", 4: "WesternLowlandGorilla"}
    # registry object fields
    m.write_bytes(obj + OFF_STRIDE, struct.pack("<q", stride))
    m.write_bytes(obj + OFF_TOP, struct.pack("<Q", top))
    m.write_bytes(obj + OFF_CAP, struct.pack("<i", 8))      # ids 1..7 examined
    # global pointer -> object
    m.write_bytes(m.module_base + REGISTRY_GLOBAL_RVA, struct.pack("<Q", obj))
    # entries
    ent_base = 0x400000000
    for sid, name in names.items():
        ent = ent_base + sid * 0x80
        m.write_bytes(ent + 0, struct.pack("<i", 1))                       # refcount
        m.write_bytes(ent + ENT_NAME, name.encode() + b"\x00")
        m.write_bytes(top - sid * stride, struct.pack("<Q", ent))          # slot -> entry
    # id 5: a refcount-0 hole (entry exists but should be skipped)
    hole = ent_base + 5 * 0x80
    m.write_bytes(hole + 0, struct.pack("<i", 0))
    m.write_bytes(hole + ENT_NAME, b"GhostSpecies\x00")
    m.write_bytes(top - 5 * stride, struct.pack("<Q", hole))
    # id 6: an odd-tagged (busy) slot pointer -> must be skipped
    m.write_bytes(top - 6 * stride, struct.pack("<Q", (ent_base + 0x999) | 1))
    return m, names


def test_registry() -> None:
    m, names = _build_registry_mem()
    r = RegistryResolver(m)
    nm = r.build_name_map()
    for sid, name in names.items():
        _check(nm.get(name) == sid, f"resolved {name} -> id {sid}")
    _check("GhostSpecies" not in nm, "refcount-0 hole skipped")
    _check(r.name_to_id("PlainsZebra") == 2, "name_to_id round-trips")
    _check(r.id_to_name(3) == "GiantPanda", "id_to_name resolves")
    _check(r.name_to_id("NoSuchAnimal") is None, "unknown name -> None")
    _check(r.resolve_many(["GiantPanda", "NoSuchAnimal", "PlainsZebra"]) ==
           {"GiantPanda": 3, "PlainsZebra": 2}, "resolve_many omits unknowns")
    # not-attached / unresolvable -> empty map, no raise
    detached = FakeMem(); detached.attached = False
    _check(RegistryResolver(detached).build_name_map() == {}, "detached registry -> empty map")


class FakeResearch:
    """Research-map stub: key -> handle for mapped species, None otherwise."""
    def __init__(self, handles: dict):
        self._h = handles

    def _snapshot(self):
        return object()

    def current_handle(self, key, _snap):
        return self._h.get(key)


def _write_tag(m: FakeMem, addr: int, tag: str) -> None:
    m.write_bytes(addr, struct.pack("<q", len(tag)))
    m.write_bytes(addr + 0x14, tag.encode())


def _build_schedule_mem(n_entries: int = 3, reward_idx: int = 1) -> "tuple[FakeMem, int, int]":
    """Synthetic exchange manager (mode 0) with a schedule of n entries; one is bIsReward."""
    m = FakeMem()
    mgr = 0x500000000
    sched = 0x600000000
    tags = 0x700000000
    m.write_bytes(mgr + mk.OFF_MGR_MODE, b"\x00")
    m.write_bytes(mgr + mk.OFF_MGR_SCHED_COUNT, struct.pack("<q", n_entries))
    m.write_bytes(mgr + mk.OFF_MGR_SCHED_DATA, struct.pack("<Q", sched))
    for i in range(n_entries):
        ent = sched + i * mk.SCHED_STRIDE
        m.write_i32(ent + mk.ENT_SPECIES, 0x1000 + i)
        tag_addr = tags + i * 0x100
        _write_tag(m, tag_addr, f"warthog{i}")
        m.write_bytes(ent + mk.ENT_TAG, struct.pack("<Q", tag_addr))
        m.write_bytes(ent + mk.ENT_GEN_MODE, b"\x01")
        m.write_bytes(ent + mk.ENT_SPAWNED, b"\x01" if i == 0 else b"\x00")   # slot 0 already consumed
        m.write_bytes(ent + mk.ENT_REWARD, b"\x01" if i == reward_idx else b"\x00")
    # live array: 2 listings
    live = 0x800000000
    m.write_bytes(mgr + mk.OFF_MGR_LIVE_COUNT, struct.pack("<q", 2))
    m.write_bytes(mgr + mk.OFF_MGR_LIVE_DATA, struct.pack("<Q", live))
    m.write_i32(live + mk.ENT_SPECIES, 0x309C)
    m.write_i32(live + mk.LIVE_STRIDE + mk.ENT_SPECIES, 0x30A8)
    return m, mgr, sched


def test_schedule_spawner() -> None:
    m, mgr, sched = _build_schedule_mem()
    sp = mk.ScheduleSpawner(m, research=FakeResearch({"zebra": 0x309C, "bison": 0x30A8}))
    sp._mgr_cache = mgr

    entries = sp.schedule_entries()
    _check(len(entries) == 3, "schedule dump returns all entries")
    _check(entries[0]["tag"] == "warthog0" and entries[2]["tag"] == "warthog2", "tags read via native-string layout")
    _check(entries[1]["reward"] == 1, "reward flag parsed")
    _check(sp.live_species() == [0x309C, 0x30A8], "live listings species parsed")

    # spawn 1: slot rotation starts at entry 0 (non-reward), even though it was consumed (re-armed)
    m.write_log.clear()
    _check(sp.spawn_species_id(0x4242), "spawn_species_id arms a slot")
    ent0 = sched
    _check(m.read_i32(ent0 + mk.ENT_SPECIES) == 0x4242, "species id repointed")
    _check(m.read_bytes(ent0 + mk.ENT_GEN_MODE, 1) == b"\x01", "generate-from-species mode set")
    _check(m.read_bytes(ent0 + mk.ENT_SPAWNED, 1) == b"\x00", "consumed slot re-armed (+0x249=0)")
    _check(m.read_bytes(ent0 + mk.ENT_IMMEDIATE, 1) == b"\x01", "immediate flag fired (+0x24A=1)")
    _check(m.read_bytes(ent0 + mk.ENT_USE_SPAWNTIME, 1) == b"\x00", "spawn-time path disabled")
    _check(m.read_bytes(ent0 + mk.ENT_COST_FLAGS, 1)[0] & 0x40, "cost-dirty bit set")
    _check(m.write_log[-1] == (ent0 + mk.ENT_IMMEDIATE, 1), "immediate flag is the LAST write")

    # spawn 2: rotation skips the reward entry -> entry 2
    _check(sp.spawn_species_id(0x5555, female=True), "second spawn arms next slot")
    ent2 = sched + 2 * mk.SCHED_STRIDE
    _check(m.read_i32(ent2 + mk.ENT_SPECIES) == 0x5555, "rotation skipped reward slot")
    _check(m.read_bytes(ent2 + mk.ENT_FEMALE, 1) == b"\x01", "female flag written when requested")
    _check(m.read_bytes(sched + mk.SCHED_STRIDE + mk.ENT_SPECIES, 4) == struct.pack("<i", 0x1001),
           "reward entry untouched")

    # research-key surface: one mapped + one unmapped
    _check(sp.spawn_keys(["zebra", "dodo"]) == 1, "spawn_keys arms only resolvable species")

    # guards: wrong mode, empty schedule
    m.write_bytes(mgr + mk.OFF_MGR_MODE, b"\x01")
    _check(not sp.spawn_species_id(0x1111), "non-scenario mode refuses to spawn")
    m.write_bytes(mgr + mk.OFF_MGR_MODE, b"\x00")
    m.write_bytes(mgr + mk.OFF_MGR_SCHED_COUNT, struct.pack("<q", 0))
    _check(not sp.spawn_species_id(0x1111), "empty schedule refuses to spawn")
    _check(sp.schedule_entries() == [], "empty schedule dumps empty")


def main() -> None:
    test_hash_set()
    test_registry()
    test_schedule_spawner()
    print("\nAll market-gate tests passed.")


if __name__ == "__main__":
    main()
