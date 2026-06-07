"""Game-free unit tests for the A3 memory modules (research/permits) — exercises the
real parsing + the restart-stable design (key off the content-stable research-item id,
resolve the volatile species handle from the map at runtime). No live game needed.

Run:  python -m tests.test_a3_modules
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.research import ResearchReader, RESEARCH_CHAIN, ITEMS_MAP_OFF  # noqa: E402
from pz_ap_client.memory.permits import PermitGate  # noqa: E402

# test welfare item ids (stable) and the per-session handles they resolve to (volatile)
WELFARE_ITEMS = {"plains_zebra": 0xDAC, "common_warthog": 0x640, "giant_panda": 0xF00,
                 "saltwater_croc": 0xE00, "lowland_gorilla": 0xE10}
H_ZEBRA, H_WARTHOG, H_PANDA, H_CROC, H_GORILLA = 0x111, 0x222, 0x333, 0x444, 0x555


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


class FakeMem:
    """Sparse byte-addressable memory with a module_base; read_bytes zero-fills gaps."""
    def __init__(self, module_base: int = 0x140000000):
        self.module_base = module_base
        self.attached = True
        self._b: dict = {}

    def write(self, addr: int, data: bytes) -> None:
        for i, byte in enumerate(data):
            self._b[addr + i] = byte

    def write_bytes(self, addr: int, data: bytes) -> None:  # scanner-API alias
        self.write(addr, data)

    def wq(self, addr: int, val: int) -> None:
        self.write(addr, struct.pack("<Q", val))

    def read_bytes(self, addr: int, size: int) -> bytes:
        return bytes(self._b.get(addr + i, 0) for i in range(size))

    def read_qword(self, addr: int):
        """Mirror MemoryScanner.read_qword (safe unsigned 8-byte read) so the fake is a faithful scanner."""
        try:
            return struct.unpack("<Q", self.read_bytes(addr, 8))[0]
        except Exception:
            return None


def _build_research_mem() -> FakeMem:
    """Chain base -> research_system, its +0xF8 items map (open-addressing bitmap + stride-
    0x58 records keyed by the stable research-item id +0x00, with the volatile handle +0x10).
      zebra   (H_ZEBRA):   welfare items 0xDAC..0xDB1 status 4 + advanced (lvl 10) status 1 -> COMPLETE
      warthog (H_WARTHOG): 0x640..0x644 status 4 + level-5 item status 2 (researching)      -> NOT
      panda   (H_PANDA):   item 0xF00 level 0 status 0 (unstarted)                          -> NOT
      croc/gorilla:        one record each (for permit handle resolution)
    plus a cat-3 (non-animal) record that must be ignored.
    """
    m = FakeMem()
    base = m.module_base
    p0, p1, rs, bk = 0x1500_0000, 0x1600_0000, 0x1700_0000, 0x1800_0000
    m.wq(base + RESEARCH_CHAIN[0], p0)
    m.wq(p0 + RESEARCH_CHAIN[1], p1)
    m.wq(p1 + RESEARCH_CHAIN[2], rs)
    cap = 64
    m.wq(rs + ITEMS_MAP_OFF + 0x10, cap)
    m.wq(rs + ITEMS_MAP_OFF + 0x18, bk)
    bm = ((cap >> 3) + 7) & ~7
    slot = [0]

    def put(item_id, level, handle, status, category=7):
        i = slot[0]; slot[0] += 1
        rec = bk + bm + i * 0x58
        m.write(rec + 0x00, struct.pack("<I", item_id))   # REC_ITEMID (stable key)
        m.write(rec + 0x0C, struct.pack("<I", level))     # REC_LEVEL
        m.write(rec + 0x10, struct.pack("<I", handle))    # REC_SPECIES (volatile handle)
        m.write(rec + 0x3C, bytes([category]))            # REC_CATEGORY
        m.write(rec + 0x49, bytes([status]))              # REC_STATUS
        m._b[bk + (i >> 3)] = m._b.get(bk + (i >> 3), 0) | (1 << (i & 7))

    for lvl in range(6):
        put(0xDAC + lvl, lvl, H_ZEBRA, 4)        # zebra standard 0-5 complete
    put(0xDB7, 10, H_ZEBRA, 1)                   # zebra advanced (ignored by the rule)
    for lvl in range(5):
        put(0x640 + lvl, lvl, H_WARTHOG, 4)      # warthog 0-4 complete
    put(0x645, 5, H_WARTHOG, 2)                  # warthog level 5 researching -> incomplete
    put(0xF00, 0, H_PANDA, 0)                    # panda unstarted
    put(0xE00, 0, H_CROC, 0)                     # croc (for permit handle resolution)
    put(0xE10, 0, H_GORILLA, 0)                  # gorilla
    # cat-3 mechanic research: one record per research, handle 0, complete <=> status 4
    put(0xB01, 0, 0, 4, category=3)              # mechanic research COMPLETE
    put(0xB02, 0, 0, 2, category=3)              # mechanic research researching (not complete)
    return m


def main() -> None:
    # --- ResearchReader: stable item id -> current handle -> welfare-complete rule ---
    rr = ResearchReader(_build_research_mem(), welfare_items=WELFARE_ITEMS,
                        research_items={"habitat_advanced_barriers": 0xB01, "drink_shops": 0xB02})
    _check(rr.current_handle("plains_zebra") == H_ZEBRA, "research: item id resolves to current (volatile) handle")
    _check(rr.is_welfare_complete("plains_zebra"), "research: zebra complete (std levels status 4, advanced ignored)")
    _check(not rr.is_welfare_complete("common_warthog"), "research: warthog NOT complete (level 5 researching)")
    _check(not rr.is_welfare_complete("giant_panda"), "research: panda NOT complete (unstarted)")
    _check(not rr.is_welfare_complete("totally_unknown_key"), "research: unmapped key -> False (no crash)")
    # is_research_complete dispatch: welfare keys + mechanic (cat-3, status==4) keys
    _check(rr.is_research_complete("welfare_plains_zebra"), "dispatch: welfare_plains_zebra -> complete")
    _check(not rr.is_research_complete("welfare_common_warthog"), "dispatch: welfare_common_warthog -> not complete")
    _check(rr.is_research_complete("habitat_advanced_barriers"), "dispatch: mechanic (status 4) -> complete")
    _check(not rr.is_research_complete("drink_shops"), "dispatch: mechanic (status 2) -> not complete")
    _check(not rr.is_research_complete("unmapped_key"), "dispatch: unmapped research key -> False")
    _check(ResearchReader(FakeMem()).is_welfare_complete("plains_zebra") is False,
           "research: unreadable chain -> False (no false positives)")

    # --- PermitGate: gated species' CURRENT handles resolved via the research map ---
    g = PermitGate(_build_research_mem(), research=ResearchReader(_build_research_mem(), welfare_items=WELFARE_ITEMS))
    g.set_gated(["saltwater_croc", "lowland_gorilla", "american_bison"])  # bison has no welfare item -> unresolvable
    _check(sorted(g._blocked_handles()) == sorted([H_CROC, H_GORILLA]),
           "permit: gated species' current handles resolved; unresolvable (bison) skipped; got %s"
           % [hex(b) for b in sorted(g._blocked_handles())])
    g.unlocked = {"saltwater_croc"}
    _check(sorted(g._blocked_handles()) == [H_GORILLA], "permit: unlocking croc leaves only gorilla blocked")
    g.unlocked = {"saltwater_croc", "lowland_gorilla"}
    _check(g._blocked_handles() == [], "permit: all gated unlocked -> nothing blocked")

    # --- MemoryEffectApplier.on_program_unlock: opens the conservation release gate ---
    from pz_ap_client.memory.applier import MemoryEffectApplier

    class FakeGate:
        def __init__(self):
            self.locked = True
        def set_locked(self, locked):
            self.locked = locked

    class FakeItem:
        def __init__(self, program_key):
            self.id, self.name = 1005, "Conservation Program"
            self.effect_type, self.effect_args = "program_unlock", {"program_key": program_key}

    gate = FakeGate()
    ap = MemoryEffectApplier(FakeMem(), anchors=None, release_gate=gate)
    _check(ap.on_program_unlock(FakeItem("conservation")) and not gate.locked,
           "program_unlock(conservation): applier opens the release gate, returns True")
    gate2 = FakeGate()
    ap2 = MemoryEffectApplier(FakeMem(), anchors=None, release_gate=gate2)
    _check(not ap2.on_program_unlock(FakeItem("some_dlc_program")) and gate2.locked,
           "program_unlock(other): unhandled key -> False, gate stays locked")
    ap3 = MemoryEffectApplier(FakeMem(), anchors=None, release_gate=None)
    _check(not ap3.on_program_unlock(FakeItem("conservation")),
           "program_unlock without a wired gate -> False (stalls, no silent advance)")

    # --- FacilityGate: blocked def-id set = (gated - unlocked), mapped via FACILITY_DEFID ---
    from pz_ap_client.memory.facilities import FacilityGate

    DEFIDS = {"research_centre": 0xA1, "workshop": 0xA2, "trade_centre": 0xA3, "vet_surgery": 0xA4}
    fg = FacilityGate(FakeMem(), facility_defids=DEFIDS)
    fg.set_gated(["research_centre", "workshop", "trade_centre", "vet_surgery", "unmapped_fac"])
    _check(sorted(fg._blocked_ids()) == [0xA1, 0xA2, 0xA3, 0xA4],
           "facility: blocked def-ids resolved; unmapped facility skipped; got %s"
           % [hex(d) for d in sorted(fg._blocked_ids())])
    fg.unlocked = {"workshop", "trade_centre"}
    _check(sorted(fg._blocked_ids()) == [0xA1, 0xA4],
           "facility: unlocking workshop+trade_centre leaves research_centre+vet_surgery blocked")
    fg.unlocked = set(DEFIDS)
    _check(fg._blocked_ids() == [], "facility: all gated unlocked -> nothing blocked")

    # applier.on_facility_unlock delegates to the gate; stalls (False) when no gate wired
    class FakeFacGate:
        def __init__(self): self.unlocked = set()
        def unlock(self, key): self.unlocked.add(key); return True
    fac = FakeFacGate()

    class FacItem:
        def __init__(self, key):
            self.id, self.name = 1002, "Facility"
            self.effect_type, self.effect_args = "facility_unlock", {"facility_key": key}
    # placement facility (trade_centre) -> delegates to the FacilityGate
    apf = MemoryEffectApplier(FakeMem(), anchors=None, facility_gate=fac)
    _check(apf.on_facility_unlock(FacItem("trade_centre")) and "trade_centre" in fac.unlocked,
           "facility_unlock(trade_centre): applier delegates to placement gate, returns True")
    apf2 = MemoryEffectApplier(FakeMem(), anchors=None, facility_gate=None)
    _check(not apf2.on_facility_unlock(FacItem("trade_centre")),
           "facility_unlock without a wired placement gate -> False (stalls)")
    # research facility (research_centre/workshop) -> acknowledged when a ResearchGate is wired
    # (the client reconcile enforces); stalls if none wired
    apf3 = MemoryEffectApplier(FakeMem(), anchors=None, research_gate=object())
    _check(apf3.on_facility_unlock(FacItem("research_centre")),
           "facility_unlock(research_centre): acknowledged (True) when ResearchGate wired")
    _check(not MemoryEffectApplier(FakeMem(), anchors=None).on_facility_unlock(FacItem("workshop")),
           "facility_unlock(workshop) without a ResearchGate -> False (stalls)")

    # --- ResearchGate: category-mapped research-state gate (data writes, no hooks) ---
    from pz_ap_client.memory.research import ResearchGate, ANIMAL_CATEGORY, MECHANIC_CATEGORY

    gr = ResearchGate(scanner=FakeMem())
    _check(gr.gated_categories([]) == {ANIMAL_CATEGORY, MECHANIC_CATEGORY},
           "research gate: nothing received -> both research categories gated")
    _check(gr.gated_categories(["research_centre"]) == {MECHANIC_CATEGORY},
           "research gate: research_centre received -> only mechanic (workshop) gated")
    _check(gr.gated_categories(["research_centre", "workshop"]) == set(),
           "research gate: both received -> nothing gated")

    # _stop_in_progress(): reset gated-category Researching(2) -> Researchable(1), leave others
    rg = _build_research_mem()
    gate = ResearchGate(ResearchReader(rg, welfare_items=WELFARE_ITEMS,
                                       research_items={"a": 0xB01, "b": 0xB02}))
    st_before = {it: st for it, lvl, st, cat, sa in gate.reader.scan_records()}
    cat_of = {it: cat for it, lvl, st, cat, sa in gate.reader.scan_records()}
    mech2 = [it for it in st_before if cat_of[it] == MECHANIC_CATEGORY and st_before[it] == 2]  # 0xB02
    anim2 = [it for it in st_before if cat_of[it] == ANIMAL_CATEGORY and st_before[it] == 2]     # warthog L5
    n = gate._stop_in_progress({MECHANIC_CATEGORY})   # workshop gated -> stop mechanic research
    st_after = {it: st for it, lvl, st, cat, sa in gate.reader.scan_records()}
    _check(n == len(mech2) and all(st_after[it] == 1 for it in mech2)
           and all(st_after[it] == 2 for it in anim2),
           "research gate: mechanic(cat3) Researching reset to Researchable, animal(cat7) untouched (n=%d)" % n)
    # the research-start hook constants are defined (validated live; install needs a real process)
    from pz_ap_client.memory.research import RESEARCH_START_RVA, RESEARCH_START_ORIG
    _check(RESEARCH_START_RVA == 0xE461C6 and RESEARCH_START_ORIG == bytes.fromhex("41c6474902"),
           "research gate: start-hook site/bytes pinned (0x140E461C6 mov [r15+0x49],2)")

    # --- PresenceGate: native greyed-button gate (research_centre / workshop) ---
    from pz_ap_client.memory.presence import (PresenceGate, PRESENCE_RVA, PRESENCE_ORIG,
                                              FACILITY_PRESENCE_MGR_OFF)
    from pz_ap_client.memory.hook import (make_presence_gate, PRESENCE_GATED_COUNT,
                                          PRESENCE_GATED_MGRS)

    def _build_presence_mem():
        """zoo chain base+0x29446A0 -> +0x38 -> zoo; zoo+0x150/+0x168 -> research/workshop mgr;
        each mgr+0x2D4 = slot count, mgr+0x390 -> present-flag bytes (all 1 = built)."""
        m = FakeMem(); base = m.module_base
        p0, zoo, rmgr, wmgr, rarr, warr = (0x1A00_0000, 0x1B00_0000, 0x1C00_0000,
                                           0x1C01_0000, 0x1D00_0000, 0x1D01_0000)
        m.wq(base + 0x29446A0, p0); m.wq(p0 + 0x38, zoo)
        m.wq(zoo + FACILITY_PRESENCE_MGR_OFF["research_centre"], rmgr)
        m.wq(zoo + FACILITY_PRESENCE_MGR_OFF["workshop"], wmgr)
        m.write(rmgr + 0x2D4, struct.pack("<I", 2)); m.wq(rmgr + 0x390, rarr); m.write(rarr, b"\x01\x01")
        m.write(wmgr + 0x2D4, struct.pack("<I", 1)); m.wq(wmgr + 0x390, warr); m.write(warr, b"\x01")
        return m, rmgr, wmgr, rarr, warr

    pm, rmgr, wmgr, rarr, warr = _build_presence_mem()
    pg = PresenceGate(pm)
    _check(pg._manager("research_centre") == rmgr and pg._manager("workshop") == wmgr,
           "presence gate: managers resolved from the zoo chain (research_centre/workshop)")
    pg.installed = True            # stub the live detour install (needs a real process)
    pg.scratch = 0x1E00_0000
    pg.set_gated({"research_centre", "workshop"})

    def _gated_set():
        cnt = struct.unpack("<I", pm.read_bytes(pg.scratch + PRESENCE_GATED_COUNT, 4))[0]
        return {struct.unpack("<Q", pm.read_bytes(pg.scratch + PRESENCE_GATED_MGRS + i * 8, 8))[0]
                for i in range(cnt)}

    pg.reconcile([])  # nothing received -> both gated + already-built flags force-zeroed
    _check(_gated_set() == {rmgr, wmgr} and pm.read_bytes(rarr, 2) == b"\x00\x00"
           and pm.read_bytes(warr, 1) == b"\x00",
           "presence gate: nothing received -> both managers gated + flags force-zeroed")
    pg.reconcile(["research_centre"])  # research_centre received -> only workshop gated
    _check(_gated_set() == {wmgr},
           "presence gate: research_centre received -> only workshop manager gated")
    pg.reconcile(["research_centre", "workshop"])  # both received -> none gated
    _check(_gated_set() == set(),
           "presence gate: both received -> nothing gated")
    _check(PRESENCE_RVA == 0x9E94863 and PRESENCE_ORIG == bytes.fromhex("c6040101488d842480000000"),
           "presence gate: fill-site/bytes pinned (0x149E94863 mov [rcx+rax],1 + lea)")
    tramp = make_presence_gate(0x149E00000, 0x149E00000, 0x149E9486F, PRESENCE_ORIG)
    _check(b"\xC6\x04\x01\x01" in tramp and b"\xC6\x04\x01\x00" in tramp and b"\x48\x3B\x2A" in tramp,
           "presence gate: trampoline has both conditional stores + cmp rbp,[rdx]")

    # --- BirthDetector: species attributed via entity+0x50 handle -> research reverse-map ---
    from pz_ap_client.memory.births import BirthDetector
    bd = BirthDetector(_build_research_mem(),
                       research=ResearchReader(_build_research_mem(), welfare_items=WELFARE_ITEMS))
    h2k = bd._handle_to_key()  # {species_handle -> species_key} from the research map
    _check(h2k.get(H_ZEBRA) == "plains_zebra" and h2k.get(H_PANDA) == "giant_panda"
           and h2k.get(H_CROC) == "saltwater_croc",
           "birth: entity-species-handle reverse-map built from research (handles -> species_keys)")
    _check(BirthDetector(FakeMem())._handle_to_key() == {},
           "birth: reverse-map empty when research map unreadable (no false births)")

    print("\nALL A3-MODULE TESTS PASSED")


def test_a3_modules() -> None:
    """pytest entry point — runs the A3-module checks (each asserts via _check)."""
    main()


if __name__ == "__main__":
    main()
