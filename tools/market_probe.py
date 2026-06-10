"""market_probe - live validation harness for the animal-market species gate (the §5a
GATE RECIPE). Run with Planet Zoo open in a LOADED zoo (ideally the AP scenario). It is
read-only by default; --apply performs the actual gate write (do this knowingly).

  python -m tools.market_probe                 # resolve mgr + dump whitelist/set fields (read-only)
  python -m tools.market_probe --registry N    # also dump the first N resolved registry names
  python -m tools.market_probe --resolve NAME  # resolve one species name -> symbol id
  python -m tools.market_probe --apply a,b,c   # APPLY the allow-list for species names a,b,c (writes!)
  python -m tools.market_probe --schedule      # dump the scenario SCHEDULE array (read-only)
  python -m tools.market_probe --live          # dump the LIVE listings (read-only)
  python -m tools.market_probe --spawn a,b     # ARM schedule slots for species keys a,b (writes!)
                                               # unpause the game; verify with --live / the market UI

--spawn drives the ScheduleSpawner boot-validation (market.py end-notes): schedule dumps sane ->
spawn one key -> live listing of that species appears, priced right, purchasable -> 12+ spawns to
prove slot re-arming -> purchase/expiry leaves the schedule entry intact.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.registry import RegistryResolver  # noqa: E402
from pz_ap_client.memory import market as mk  # noqa: E402

PROCESS_NAME = "PlanetZoo.exe"


def _dump_set(s: MemoryScanner, set_addr: int, label: str) -> None:
    try:
        count = s.read_i64(set_addr + mk.SET_COUNT)
        cap = s.read_i64(set_addr + mk.SET_CAP)
        buf = s.read_qword(set_addr + mk.SET_BUFFER)
    except Exception as e:
        print(f"  {label}: unreadable ({e})")
        return
    print(f"  {label} @0x{set_addr:X}: count(+0x08)={count}  cap(+0x10)={cap}  buffer(+0x18)=0x{buf or 0:X}")
    if buf and 0 < cap <= (1 << 20) and (cap & (cap - 1)) == 0:
        bitvec_bytes = ((cap >> 3) + 7) & ~7
        try:
            blob = s.read_bytes(buf, bitvec_bytes + cap * 4)
        except Exception:
            return
        members = []
        for slot in range(cap):
            word = struct.unpack_from("<Q", blob, (slot >> 6) * 8)[0]
            if (word >> (slot & 0x3F)) & 1:
                members.append(struct.unpack_from("<i", blob, bitvec_bytes + slot * 4)[0])
        print(f"    members ({len(members)}): {[hex(k) for k in members[:32]]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", type=int, default=0, metavar="N", help="dump first N registry names")
    ap.add_argument("--resolve", metavar="NAME", help="resolve a species name -> id")
    ap.add_argument("--apply", metavar="A,B,C", help="APPLY allow-list for these species names (WRITES)")
    ap.add_argument("--schedule", action="store_true", help="dump the scenario schedule array")
    ap.add_argument("--live", action="store_true", help="dump the live listings (species ids)")
    ap.add_argument("--spawn", metavar="A,B,C", help="ARM schedule slots for these species keys (WRITES)")
    args = ap.parse_args()

    s = MemoryScanner(PROCESS_NAME)
    if not s.attach():
        print("not attached (is Planet Zoo running, in a loaded zoo?)")
        return
    print(f"attached, module base 0x{s.module_base:X}")

    reg = RegistryResolver(s)
    nm = reg.build_name_map()
    print(f"registry: {len(nm)} interned names")
    if args.registry:
        for name, sid in sorted(nm.items())[:args.registry]:
            print(f"  {sid:6d}  {name}")
    if args.resolve:
        print(f"resolve {args.resolve!r} -> {reg.name_to_id(args.resolve)}")

    gate = mk.SpeciesMarketGate(s, registry=reg)
    mgr = gate.exchange_mgr()
    if mgr is None:
        print("exchange manager UNRESOLVED (not in a loaded zoo, or PARK chain stale - re-derive)")
        return
    _dump_mgr_fields(s, mgr)

    if args.apply:
        keys = [k.strip() for k in args.apply.split(",") if k.strip()]
        print(f"\nAPPLYING allow-list for {keys} (research-map resolution) ...")
        ok = gate.apply_unlocked_keys(keys)
        print(f"apply_unlocked_keys -> {ok}")
        _dump_set(s, mgr + mk.OFF_MGR_DEFAULT_WHITELIST + mk.WL_INCLUDE_SET, "include-set (after)")
        print("now run the script whitelist(\"\") activation (or --activate) and watch the market UI.")

    spawner = mk.ScheduleSpawner(s, research=gate.research)
    spawner._mgr_cache = mgr
    if args.schedule:
        _dump_schedule(s, reg, spawner, mgr)
    if args.live:
        ids = spawner.live_species()
        print(f"\nlive listings: {len(ids)}")
        for sid in ids:
            print(f"  species=0x{sid:X} ({reg.id_to_name(sid) or '?'})")
    if args.spawn:
        keys = [k.strip() for k in args.spawn.split(",") if k.strip()]
        print(f"\nARMING schedule slots for {keys} (research-map resolution) ...")
        armed = spawner.spawn_keys(keys)
        print(f"spawn_keys -> {armed} slot(s) armed; unpause the game, then re-run with --live")


def _dump_mgr_fields(s: MemoryScanner, mgr: int) -> None:
    print(f"exchange_mgr = 0x{mgr:X}")
    for off, name in [(mk.OFF_MGR_MODE, "mode(+0x41C)"), (mk.OFF_MGR_ACTIVATION, "activation(+0x210)"),
                      (mk.OFF_MGR_POOL_DIRTY, "pool-dirty(+0x211)")]:
        try:
            print(f"  {name} = {s.read_bytes(mgr + off, 1)[0]}")
        except Exception as e:
            print(f"  {name}: unreadable ({e})")
    wl = mgr + mk.OFF_MGR_DEFAULT_WHITELIST
    try:
        print(f"  default-whitelist @0x{wl:X}  include-active(+0x28)={s.read_bytes(wl + mk.WL_INCLUDE_ACTIVE, 1)[0]}")
    except Exception:
        pass
    _dump_set(s, wl + mk.WL_INCLUDE_SET, "include-set")


def _dump_schedule(s: MemoryScanner, reg: RegistryResolver, spawner, mgr: int) -> None:
    entries = spawner.schedule_entries()
    print(f"\nschedule: {len(entries)} entries @0x{s.read_qword(mgr + mk.OFF_MGR_SCHED_DATA) or 0:X}"
          f"  (rewards-enabled(+0x270)={s.read_bytes(mgr + mk.OFF_MGR_REWARDS_ENABLED, 1)[0]})")
    for e in entries:
        name = reg.id_to_name(e["species_id"]) or "?"
        print(f"  [{e['index']:2d}] species=0x{e['species_id']:X} ({name})  tag={e['tag']!r}"
              f"  spawned={e['spawned']} immediate={e['immediate']} gen_mode={e['gen_mode']}"
              f"  female={e['female']} reward={e['reward']}")


if __name__ == "__main__":
    main()
