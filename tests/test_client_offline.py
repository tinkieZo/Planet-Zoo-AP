"""Offline end-to-end test of the A1 shell + A3 idempotency + goal detection.

No AP server and no game are needed: we construct the context inside an event
loop, stub the network (``send_msgs`` / ``check_locations``), and drive the same
``on_package`` entry points the real server loop would, plus the console's
``report_check``. This exercises:

  * item application via the ConsoleEffectApplier,
  * the idempotent high-water mark across a simulated reconnect,
  * goal detection from slot_data.

Run:  python -m tests.test_client_offline
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.client import PZContext  # noqa: E402

from NetUtils import NetworkItem, NetworkSlot, SlotType  # noqa: E402


SEED = "TESTSEED123"
SLOT = 1


def _load_gd():
    from pz_ap_client import data as pz_data
    return pz_data.load()


def _eid(gd, effect_type: str, **args) -> int:
    """First item id with the given effect (and optional effect_args). data.json is regenerated
    from the APWorld so ids shift - resolve by effect, never hardcode."""
    for it in gd.items:
        if it.effect_type == effect_type and all(it.effect_args.get(k) == v for k, v in args.items()):
            return it.id
    raise KeyError((effect_type, args))


def _make_ctx(applied_log: list, state_dir: Path) -> PZContext:
    # state_dir keeps the idempotency high-water mark file in a temp dir.
    ctx = PZContext(None, None, state_dir=str(state_dir))

    # Stub the network so report_check / goal status don't hit a socket. These
    # stay coroutines (the client awaits / _spawns them); the sleep(0) yields
    # control like a real network round-trip would.
    async def _noop_send(msgs):
        await asyncio.sleep(0)
        for m in msgs:
            if m.get("cmd") == "StatusUpdate":
                applied_log.append(("status", m["status"]))

    async def _fake_check(locations):
        await asyncio.sleep(0)
        locs = set(locations) & ctx.missing_locations
        ctx.checked_locations |= locs
        ctx.missing_locations -= locs
        return locs

    ctx.send_msgs = _noop_send
    ctx.check_locations = _fake_check
    return ctx


def _simulate_connect(ctx: PZContext, received: list) -> None:
    """Drive the packets the server would send, in order, bypassing the socket."""
    ctx.seed_name = SEED
    ctx.slot = SLOT
    ctx.slot_info = {SLOT: NetworkSlot("p1", "Planet Zoo", SlotType.player)}
    # Server tells us the full location pool.
    ctx.missing_locations = {l.id for l in ctx.game_data.locations}
    ctx.checked_locations = set()
    ctx.server_locations = set(ctx.missing_locations)

    ctx.on_package("RoomInfo", {"seed_name": SEED})
    ctx.slot_data = {}
    # Goal: breed two species (a 2-location goal so the partial-completion path is exercised). Each
    # required_breed key resolves to that species' first_breed location.
    ctx.on_package("Connected", {"slot_data": {
        "goal": {"type": "breed", "args": {"required_breed": ["gpanda", "pzebra"]}}
    }})
    ctx.items_received = [NetworkItem(*r) for r in received]
    ctx.on_package("ReceivedItems", {"index": 0})


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


async def _drain(ctx: PZContext) -> None:
    """Await the fire-and-forget tasks the client spawned (check / status sends)."""
    tasks = list(ctx._bg_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pz_state_"))
    applied_log: list = []

    import pz_ap_client.effects as effects
    real_log = effects.ConsoleEffectApplier._log

    def counting_log(self, item, detail):
        applied_log.append(("apply", item.id))
        return real_log(self, item, detail)

    effects.ConsoleEffectApplier._log = counting_log
    try:
        # Resolve concrete item/location ids dynamically from data.json (it's regenerated from the
        # APWorld, so ids shift; never hardcode them). Pick 4 distinct items by effect.
        gd = _load_gd()
        i_cash = _eid(gd, "cash")
        i_fac = _eid(gd, "facility_unlock")
        i_permit = _eid(gd, "species_unlock")
        i_cc = _eid(gd, "cc")
        # goal locations: first_breed for gpanda + pzebra
        fb = {l.trigger_args.get("species_key"): l.id for l in gd.locations_by_trigger("first_breed")}
        goal_ids = {fb["gpanda"], fb["pzebra"]}

        # --- first connect: receive 3 items (cash, a facility, a permit)
        ctx = _make_ctx(applied_log, tmp)
        received = [(i_cash, 2000, 2, 0), (i_fac, 2001, 3, 0), (i_permit, 2002, 4, 0)]
        _simulate_connect(ctx, received)
        applies = [e for e in applied_log if e[0] == "apply"]
        _check(len(applies) == 3, f"applied all 3 received items on first connect (got {len(applies)})")
        _check(ctx.applied_high_water() == 3, "high-water mark advanced to 3")

        # --- simulate reconnect: NEW context, same seed/slot, server resends same items
        applied_log.clear()
        ctx2 = _make_ctx(applied_log, tmp)
        _simulate_connect(ctx2, received)
        reapplies = [e for e in applied_log if e[0] == "apply"]
        _check(len(reapplies) == 0, f"NO re-application after reconnect (got {len(reapplies)})")
        _check(ctx2.applied_high_water() == 3, "high-water mark still 3 after reconnect")

        # --- one more item arrives post-reconnect -> applied exactly once
        ctx2.items_received.append(NetworkItem(i_cc, 2003, 5, 0))  # a filler CC item
        ctx2.on_package("ReceivedItems", {"index": 3})
        _check(len([e for e in applied_log if e[0] == "apply"]) == 1, "new 4th item applied once")
        _check(ctx2.applied_high_water() == 4, "high-water mark advanced to 4")

        # --- goal detection: check the two goal locations (breed gpanda + pzebra)
        _check(set(ctx2._goal_location_ids) == goal_ids,
               f"goal resolved to the two first_breed locations (got {ctx2._goal_location_ids})")
        first, second = sorted(goal_ids)
        ctx2.report_check(first)
        await _drain(ctx2)  # let the stubbed check task run
        _check(not ctx2.finished_game, "goal not complete with only 1 of 2 locations")
        ctx2.report_check(second)
        await _drain(ctx2)
        _check(ctx2.finished_game, "goal COMPLETE after both locations checked")
        _check(("status", 30) in applied_log, "CLIENT_GOAL (30) status sent")

        # --- market reconciler: stocks the scenario market with unlocked species only
        class _FakeResearch:
            def __init__(self, handles): self._h = handles
            def _snapshot(self): return object()
            def current_handle(self, key, _snap): return self._h.get(key)

        class _FakeSpawner:
            def __init__(self, handles):
                self.research = _FakeResearch(handles)
                self.live = []
                self.spawned = []
                self.mode = True
            def scenario_mode(self): return self.mode
            def live_species(self): return list(self.live)
            def spawn_species_id(self, h, female=None): self.spawned.append(h); return True

        # ctx2 holds exactly one permit (i_permit) -> that species is purchase-unblocked; every
        # other species stays blocked, so only it may be offered on the market.
        unblocked_key = gd.item_by_id[i_permit].effect_args["species_key"]
        H = 0x999  # arbitrary fake research handle for that species
        spawner = _FakeSpawner({unblocked_key: H})
        ctx2.market_spawner = spawner
        ctx2._reconcile_market()
        _check(spawner.spawned == [H], "market offers exactly the unlocked species")
        ctx2._reconcile_market()
        _check(spawner.spawned == [H], "respawn cooldown prevents immediate re-arm")
        ctx2._market_last_spawn.clear()
        spawner.live = [H]
        ctx2._reconcile_market()
        _check(spawner.spawned == [H], "no re-arm while a live listing exists")
        ctx2._market_last_spawn.clear()
        spawner.live = []
        spawner.mode = False
        ctx2._reconcile_market()
        _check(spawner.spawned == [H], "no-op outside scenario mode")
        spawner.mode = True
        ctx2._reconcile_market()
        _check(spawner.spawned == [H, H], "purchased/expired listing re-offered after cooldown")

        print("\nALL OFFLINE TESTS PASSED")
    finally:
        effects.ConsoleEffectApplier._log = real_log


def test_client_offline() -> None:
    """pytest entry point - runs the async A1/A3/goal round-trip (asserts via _check)."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
