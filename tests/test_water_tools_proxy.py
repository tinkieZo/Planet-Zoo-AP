"""Game-free tests for the water_tools species-purchase proxy.

The water tool's real in-game button lives in the data-driven Cobra UI we can't reach, so water_tools
is enforced as a species-PURCHASE block: the aquatic species it gates can't be bought until it (and any
co-required permit) arrives. This tests the pure gate logic in data.py + the client's reconcile, with no
game and no AP server.

Item ids are resolved by EFFECT (via _eid), never hardcoded, so the tests survive id renumbering when
data.json's id<->name table is realigned to the APWorld (item IDs = 1000 + index of items.json).

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


def _eid(gd, effect_type: str, **args) -> int:
    """Resolve an item id by its EFFECT, so these tests survive id renumbering when data.json is
    realigned to the APWorld (ids are positional there: 1000 + index of items.json)."""
    for it in gd.items:
        if it.effect_type == effect_type and all(it.effect_args.get(k) == v for k, v in args.items()):
            return it.id
    raise KeyError((effect_type, args))


# In the full v1.0 model EVERY species is permit-gated; water-needed species additionally need
# water_tools. Pick representatives by gate composition so these tests don't hardcode stringids.
def _permit_only_species(gd):
    for s in gd.species:
        t = gd.species_purchase_tokens(s)
        if len(t) == 1 and t[0].startswith("permit_"):
            return s.key
    raise AssertionError("no permit-only species in data.json")


def _water_permit_species(gd):
    for s in gd.species:
        t = set(gd.species_purchase_tokens(s))
        if "water_tools" in t and any(x.startswith("permit_") for x in t):
            return s.key
    raise AssertionError("no water+permit species in data.json")


def test_purchase_tokens(gd) -> None:
    sp = gd.species_by_key
    pk = _permit_only_species(gd)
    wk = _water_permit_species(gd)
    _check(gd.species_purchase_tokens(sp[pk]) == ("permit_" + pk,),
           f"{pk}: purchase tokens = (permit_{pk},) (got {gd.species_purchase_tokens(sp[pk])})")
    _check(set(gd.species_purchase_tokens(sp[wk])) == {"water_tools", "permit_" + wk},
           f"{wk}: tokens = water_tools + permit (got {gd.species_purchase_tokens(sp[wk])})")
    # the flagship giant panda's conservation_program token is NOT purchase-enforced (its own gate
    # handles it) - only the permit (+ water if any) shows up as a purchase token.
    panda = sp.get("gpanda")
    if panda is not None:
        _check("conservation_program" not in gd.species_purchase_tokens(panda),
               f"gpanda tokens exclude conservation_program (got {gd.species_purchase_tokens(panda)})")


def test_blocked_sets(gd) -> None:
    water = _eid(gd, "tool_unlock", tool_key="water_tools")
    wk = _water_permit_species(gd)             # needs water_tools AND its permit
    pk = _permit_only_species(gd)              # needs only its permit
    wperm = _eid(gd, "species_unlock", species_key=wk)
    pperm = _eid(gd, "species_unlock", species_key=pk)

    # nothing received -> every gated species blocked == full purchase universe
    none_blocked = gd.purchase_blocked_species([])
    _check({wk, pk} <= none_blocked, "with no items: permit/aquatic species are blocked")
    _check(none_blocked == gd.purchase_universe(),
           "with no items, blocked set == full purchase universe")

    # water_tools only -> the water+permit species STILL needs its permit (AND-gate)
    w = gd.purchase_blocked_species([water])
    _check(wk in w, "water_tools alone does NOT unblock a water+permit species (AND-gate)")
    _check(pk in w, "water_tools doesn't touch a permit-only species")

    # the water species' permit only (no water_tools) -> STILL blocked (needs the tool too)
    p = gd.purchase_blocked_species([wperm])
    _check(wk in p, "permit alone does NOT unblock a water+permit species (still needs water_tools)")

    # both -> the water+permit species frees
    b = gd.purchase_blocked_species([water, wperm])
    _check(wk not in b, "water_tools + permit unblocks the water+permit species")

    # an unrelated permit frees only its species
    t = gd.purchase_blocked_species([pperm])
    _check(pk not in t, "a permit unblocks its own species")
    _check(wk in t, "and leaves water-gated species blocked")


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

    water = _eid(gd, "tool_unlock", tool_key="water_tools")
    wk = _water_permit_species(gd)          # needs water_tools AND its permit
    wperm = _eid(gd, "species_unlock", species_key=wk)

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

            _check(wk in received([]), "client reconcile: nothing received -> water+permit species blocked")
            _check(wk in received([water]),
                   "client reconcile: water_tools alone keeps the water+permit species blocked")
            _check(wk not in received([water, wperm]),
                   "client reconcile: water_tools + permit frees the water+permit species")
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
    water = gd.item_by_id[_eid(gd, "tool_unlock", tool_key="water_tools")]
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
