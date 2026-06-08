"""Game-free tests for the water_tools species-purchase proxy (item 1003).

The water tool's real in-game button lives in the data-driven Cobra UI we can't reach, so water_tools
is enforced as a species-PURCHASE block: the aquatic species it gates can't be bought until it (and any
co-required permit) arrives. This tests the pure gate logic in data.py + the client's reconcile, with no
game and no AP server.

Item ids (data.json): 1003 water_tools (tool_unlock) | 1002 snow_leopard | 1004 bengal_tiger |
1006 saltwater_croc | 1007 lowland_gorilla | 1008 giant_panda (species_unlock permits).

Run:  python -m tests.test_water_tools_proxy
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client import data as pz_data  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


def test_purchase_tokens(gd) -> None:
    sp = gd.species_by_key

    def toks(key):
        return gd.species_purchase_tokens(sp[key])

    _check(toks("nile_hippo") == ("water_tools",),
           f"nile_hippo purchase tokens = (water_tools,) (got {toks('nile_hippo')})")
    _check(set(toks("saltwater_croc")) == {"water_tools", "permit_saltwater_croc"},
           f"saltwater_croc tokens = water_tools + permit (got {toks('saltwater_croc')})")
    _check(toks("bengal_tiger") == ("permit_bengal_tiger",),
           f"bengal_tiger tokens = (permit_bengal_tiger,) (got {toks('bengal_tiger')})")
    _check(toks("african_elephant") == (),
           f"african_elephant (start) has NO purchase tokens (got {toks('african_elephant')})")
    _check(toks("lowland_gorilla") == ("permit_lowland_gorilla",),
           f"lowland_gorilla tokens = (permit_lowland_gorilla,) (got {toks('lowland_gorilla')})")
    # compound gate: a non-purchase token (conservation_program) is NOT purchase-enforced - its own gate handles it
    _check(toks("giant_panda") == ("permit_giant_panda",),
           f"giant_panda tokens exclude conservation_program (got {toks('giant_panda')})")


def test_blocked_sets(gd) -> None:
    # nothing received -> every gated species blocked, never african_elephant
    none_blocked = gd.purchase_blocked_species([])
    _check({"nile_hippo", "saltwater_croc", "bengal_tiger"} <= none_blocked,
           "with no items: aquatic + permit species are blocked")
    _check("african_elephant" not in none_blocked, "african_elephant (start) never blocked")
    _check(none_blocked == gd.purchase_universe(),
           "with no items, blocked set == full purchase universe")

    # water_tools only -> nile_hippo frees; saltwater_croc still needs its permit
    w = gd.purchase_blocked_species([1003])
    _check("nile_hippo" not in w, "water_tools unblocks nile_hippo")
    _check("saltwater_croc" in w, "water_tools alone does NOT unblock saltwater_croc (AND-gate)")
    _check("bengal_tiger" in w, "water_tools doesn't touch bengal_tiger")

    # permit only (no water_tools) -> saltwater_croc STILL blocked (needs the tool too)
    p = gd.purchase_blocked_species([1006])
    _check("saltwater_croc" in p, "permit alone does NOT unblock saltwater_croc (still needs water_tools)")
    _check("nile_hippo" in p, "permit for croc doesn't unblock nile_hippo")

    # both -> saltwater_croc frees
    b = gd.purchase_blocked_species([1003, 1006])
    _check("saltwater_croc" not in b, "water_tools + permit unblocks saltwater_croc")
    _check("nile_hippo" not in b, "and nile_hippo too")

    # an unrelated permit frees only its species
    t = gd.purchase_blocked_species([1004])
    _check("bengal_tiger" not in t, "bengal_tiger permit unblocks bengal_tiger")
    _check("nile_hippo" in t, "and leaves water-gated species blocked")


def test_client_reconcile(gd) -> None:
    """The client's _reconcile_permits must hand the gate exactly the SATISFIED species
    (universe - blocked), so the gate blocks the right set."""
    try:
        from pz_ap_client.client import PZContext
        from NetUtils import NetworkItem
    except Exception as e:  # vendored Archipelago tree not present in this checkout
        print(f"SKIP - client reconcile test (Archipelago vendor tree not importable: {e})")
        return

    class FakeGate:
        def __init__(self):
            self.gated = None
            self.last_reconcile = None

        def set_gated(self, keys):
            self.gated = set(keys)

        def reconcile(self, unlocked):
            self.last_reconcile = set(unlocked)
            return True

    async def _run():
        # PZContext (AP CommonContext) schedules a keep-alive task on construction, so it must be
        # built inside a running event loop; cancel that task afterward so the loop closes cleanly.
        ctx = PZContext(None, None)
        try:
            ctx.permit_gate = FakeGate()
            ctx.permit_gate.set_gated(gd.purchase_universe())

            def received(ids):
                ctx.items_received = [NetworkItem(i, 9000 + n, 1, 0) for n, i in enumerate(ids)]
                ctx._reconcile_permits()
                # blocked = universe - satisfied(reconcile arg)
                return ctx.permit_gate.gated - ctx.permit_gate.last_reconcile

            _check("nile_hippo" in received([]) and "saltwater_croc" in received([]),
                   "client reconcile: nothing received -> aquatic species blocked")
            _check("nile_hippo" not in received([1003]), "client reconcile: water_tools frees nile_hippo")
            _check("saltwater_croc" in received([1003]),
                   "client reconcile: water_tools alone keeps saltwater_croc blocked")
            _check("saltwater_croc" not in received([1003, 1006]),
                   "client reconcile: water_tools + permit frees saltwater_croc")
        finally:
            task = ctx.keep_alive_task
            if task is not None:
                task.cancel()
                # Drain the cancelled keep-alive task; return_exceptions swallows its own
                # CancelledError as a result without masking a cancellation of THIS coroutine.
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_run())


def test_tool_unlock_applies(gd) -> None:
    """on_tool_unlock must ACKNOWLEDGE (return True) - previously it stalled the whole item
    queue by returning False. Enforcement is the purchase reconcile, not this handler."""
    try:
        from pz_ap_client.memory.applier import MemoryEffectApplier
    except Exception as e:  # memory deps (pymem) unavailable
        print(f"SKIP - on_tool_unlock test (memory deps unavailable: {e})")
        return

    class FakeScanner:
        attached = True

        def attach(self):
            return True

    class FakeGate:
        def ensure_installed(self):
            return True

    applier = MemoryEffectApplier(FakeScanner(), anchors=None, permit_gate=FakeGate())
    water = gd.item_by_id[1003]
    _check(applier.on_tool_unlock(water) is True,
           "on_tool_unlock(water_tools) acknowledges (True) instead of stalling")


def main() -> None:
    gd = pz_data.load()
    test_purchase_tokens(gd)
    test_blocked_sets(gd)
    test_client_reconcile(gd)
    test_tool_unlock_applies(gd)
    print("\nALL WATER_TOOLS PROXY TESTS PASSED")


if __name__ == "__main__":
    main()
