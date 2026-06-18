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
    keys_meta = "A,B,C"
    ap.add_argument("--apply", metavar=keys_meta, help="APPLY allow-list for these species names (WRITES)")
    ap.add_argument("--schedule", action="store_true", help="dump the scenario schedule array")
    ap.add_argument("--live", action="store_true", help="dump the live listings (species ids)")
    ap.add_argument("--pool", action="store_true", help="dump the autofill candidate pool (what the gate filters)")
    ap.add_argument("--spawn", metavar=keys_meta, help="ARM schedule slots for these species keys (WRITES)")
    ap.add_argument("--expire", metavar=keys_meta, help="EXPIRE live listings NOT in these species keys (WRITES)")
    ap.add_argument("--disable", action="store_true", help="turn the gate OFF (unrestricted market) (WRITES)")
    args = ap.parse_args()

    s = MemoryScanner(PROCESS_NAME)
    if not s.attach():
        print("not attached (is Planet Zoo running, in a loaded zoo?)")
        return
    print(f"attached, module base 0x{s.module_base:X}")

    reg = RegistryResolver(s)
    print(f"registry: {len(reg.build_name_map())} interned names")
    _run_registry_cmds(reg, args)

    # Build the gate's ResearchReader WITH token_to_key (from data.json) - same as the real client -
    # so species_key -> handle resolution covers every species (the bare reader only knows the ~11
    # captured welfare ids). Without this the probe can't resolve uncaptured species like 'aardvark'.
    from pz_ap_client import data as gamedata
    from pz_ap_client.memory.research import ResearchReader
    gd = gamedata.load()
    token_to_key = {sp.engine_token: sp.key for sp in gd.species if sp.engine_token}
    research = ResearchReader(s, registry=reg, token_to_key=token_to_key)
    gate = mk.SpeciesMarketGate(s, research=research, registry=reg)
    mgr = gate.exchange_mgr()
    if mgr is None:
        print("exchange manager UNRESOLVED (not in a loaded zoo, or PARK chain stale - re-derive)")
        return
    _dump_mgr_fields(s, mgr)
    _run_market_cmds(s, reg, gate, mgr, args)


def _run_registry_cmds(reg: RegistryResolver, args) -> None:
    if args.registry:
        for name, sid in sorted(reg.build_name_map().items())[:args.registry]:
            print(f"  {sid:6d}  {name}")
    if args.resolve:
        print(f"resolve {args.resolve!r} -> {reg.name_to_id(args.resolve)}")


def _print_species(label: str, ids, reg: RegistryResolver, distinct: bool = False) -> None:
    shown = sorted(set(ids)) if distinct else ids
    extra = f", {len(set(ids))} distinct" if distinct else ""
    print(f"\n{label}: {len(ids)} entries{extra}")
    for sid in shown:
        print(f"  species=0x{sid:X} ({reg.id_to_name(sid) or '?'})")


def _run_market_cmds(s: MemoryScanner, reg: RegistryResolver, gate, mgr: int, args) -> None:
    if args.apply:
        keys = [k.strip() for k in args.apply.split(",") if k.strip()]
        print(f"\nAPPLYING allow-list for {keys} (research-map resolution) ...")
        print(f"apply_unlocked_keys -> {gate.apply_unlocked_keys(keys)}")
        _dump_set(s, mgr + mk.OFF_MGR_DEFAULT_WHITELIST + mk.WL_INCLUDE_SET, "include-set (after)")
        print("now unpause the game so the pool rebuilds, then re-run with --pool / --live.")
    spawner = mk.ScheduleSpawner(s, research=gate.research)
    spawner._mgr_cache = mgr
    if args.schedule:
        _dump_schedule(s, reg, spawner, mgr)
    if args.live:
        _print_species("live listings", spawner.live_species(), reg)
    if args.pool:
        _print_species("autofill candidate pool", gate.pool_species(), reg, distinct=True)
    if args.spawn:
        keys = [k.strip() for k in args.spawn.split(",") if k.strip()]
        print(f"\nARMING schedule slots for {keys} (research-map resolution) ...")
        print(f"spawn_keys -> {spawner.spawn_keys(keys)} slot(s) armed; unpause, then re-run with --live")
    if args.expire:
        keys = [k.strip() for k in args.expire.split(",") if k.strip()]
        allowed = gate._resolve_handles(keys)
        print(f"\nEXPIRING live listings NOT in {keys} (allowed ids {[hex(i) for i in allowed]}) ...")
        print(f"expire_blocked_listings -> {gate.expire_blocked_listings(allowed)} marked; unpause, "
              "then check the market UI / --live")
    if args.disable:
        print(f"\nDISABLING gate (unrestricted market) -> {gate.disable()}; unpause to rebuild the full pool")


def _dump_mgr_fields(s: MemoryScanner, mgr: int) -> None:
    print(f"exchange_mgr = 0x{mgr:X}")
    for off, name in [(mk.OFF_MGR_MODE, "mode(+0x41C)"), (mk.OFF_MGR_ACTIVATION, "activation(+0x210)"),
                      (mk.OFF_MGR_POOL_DIRTY, "pool-dirty(+0x211)")]:
        try:
            print(f"  {name} = {s.read_bytes(mgr + off, 1)[0]}")
        except Exception as e:
            print(f"  {name}: unreadable ({e})")
    try:
        print(f"  active-wl-id scen(+0x3B8)={s.read_i32(mgr + mk.OFF_MGR_ACTIVE_WL_ID_SCEN)} "
              f"sandbox(+0x418)={s.read_i32(mgr + mk.OFF_MGR_ACTIVE_WL_ID_SANDBOX)} "
              f"(0/miss -> default whitelist is used by the rebuild)")
    except Exception:
        pass
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
