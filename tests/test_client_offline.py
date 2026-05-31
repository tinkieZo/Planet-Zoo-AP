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
    ctx.on_package("Connected", {"slot_data": {
        "goal": {"type": "chain", "args": {
            "required_research": ["welfare_giant_panda"],
            "required_breed": ["giant_panda"],
        }}
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
        # --- first connect: receive 3 items (cash 1009, climate 1001, panda permit 1008)
        ctx = _make_ctx(applied_log, tmp)
        received = [(1009, 2000, 2, 0), (1001, 2001, 3, 0), (1008, 2002, 4, 0)]
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
        ctx2.items_received.append(NetworkItem(1014, 2003, 5, 0))  # Petty Cash filler
        ctx2.on_package("ReceivedItems", {"index": 3})
        _check(len([e for e in applied_log if e[0] == "apply"]) == 1, "new 4th item applied once")
        _check(ctx2.applied_high_water() == 4, "high-water mark advanced to 4")

        # --- goal detection: check the two goal locations
        # welfare_giant_panda research = 2009, first_breed giant_panda = 2016
        _check(set(ctx2._goal_location_ids) == {2009, 2016}, "goal resolved to locations 2009 + 2016")
        ctx2.report_check(2009)
        await _drain(ctx2)  # let the stubbed check task run
        _check(not ctx2.finished_game, "goal not complete with only 1 of 2 locations")
        ctx2.report_check(2016)
        await _drain(ctx2)
        _check(ctx2.finished_game, "goal COMPLETE after both locations checked")
        _check(("status", 30) in applied_log, "CLIENT_GOAL (30) status sent")

        print("\nALL OFFLINE TESTS PASSED")
    finally:
        effects.ConsoleEffectApplier._log = real_log


if __name__ == "__main__":
    asyncio.run(main())
