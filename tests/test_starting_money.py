"""The room's starting_money (slot_data) is applied to the park's cash, overriding the scenario's
default starting cash. Game-free: drive PZContext._apply_starting_money with a fake applier.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.client import PZContext  # noqa: E402


class FakeAnchors:
    def __init__(self):
        self.writes = []

    def write(self, scanner, name, value):
        self.writes.append((name, value))
        return True


class FakeApplier:
    def __init__(self, anchors):
        self.anchors = anchors
        self.scanner = object()


def _ctx(slot_data, applier):
    # _apply_starting_money only reads self.slot_data and self.applier, so a namespace suffices
    # (avoids building the full AP context + event loop).
    return types.SimpleNamespace(slot_data=slot_data, applier=applier)


def test_starting_money_written_to_cash_anchor():
    anchors = FakeAnchors()
    ctx = _ctx({"starting_money": 50000}, FakeApplier(anchors))
    PZContext._apply_starting_money(ctx)
    assert anchors.writes == [("cash", 50000)], "writes room starting_money to the cash anchor (dollars)"


def test_no_starting_money_in_slot_data_is_noop():
    anchors = FakeAnchors()
    ctx = _ctx({}, FakeApplier(anchors))
    PZContext._apply_starting_money(ctx)
    assert anchors.writes == [], "no starting_money -> no write"


def test_console_applier_is_noop():
    # ConsoleEffectApplier has no .anchors/.scanner -> must not raise, must not write.
    ctx = _ctx({"starting_money": 50000}, types.SimpleNamespace())
    PZContext._apply_starting_money(ctx)  # should be a clean no-op


def test_applied_in_fresh_reset_branch():
    """starting_money is set the first time a fresh park is handled (folded into _maybe_fresh_reset)."""
    import inspect
    src = inspect.getsource(PZContext._maybe_fresh_reset)
    assert "_apply_starting_money" in src, "fresh-park handling calls _apply_starting_money"


# --- ordering: starting money must be set BEFORE the cash ledger lands on top (the $50k-clobber bug) --

class _OrderAnchors:
    """Records every cash write in order. The baseline write is absolute (first, smaller than baked);
    the LEDGER write is current+delta. Distinguished by whether the value builds on the current cash."""
    def __init__(self, baked):
        self.cash = baked
        self.order = []  # ("baseline", amt) | ("ledger", cash_after)

    def read(self, scanner, name):
        return self.cash if name == "cash" else 0.0

    def write(self, scanner, name, value):
        if name != "cash":
            return True   # cc writes accepted silently (not under test)
        kind = "ledger" if value > self.cash else "baseline"   # ledger only ever ADDS to current
        self.cash = value
        self.order.append((kind, value))
        return True


class _OrderApplier:
    """Real MemoryEffectApplier contract: cash items acknowledge only (the ledger applies money)."""
    def __init__(self, anchors):
        self.anchors = anchors
        self.scanner = object()

    def apply(self, item):
        return True   # acknowledge; money flows through _reconcile_cumulative


class _State:
    def __init__(self):
        self.mark = 0
        self._pending = False
        self.granted = {}

    def get_granted(self, seed, slot, kind):
        return self.granted.get(kind, 0.0)

    def set_granted(self, seed, slot, kind, total):
        self.granted[kind] = total

    def reset_granted(self, seed, slot):
        self.granted = {}
        self._pending = False

    def get(self, seed, slot):
        return self.mark

    def set(self, seed, slot, v):
        self.mark = v

    def get_fresh_pending(self, seed, slot):
        return self._pending

    def set_fresh_pending(self, seed, slot, v):
        self._pending = v


def _ni(item_id):
    return types.SimpleNamespace(item=item_id)


def _apply_ctx(park_years_value, cash_items=(2000, 2000)):
    """A namespace driving the REAL _apply_new_items (incl. the real cumulative LEDGER) with controllable
    park age. cash_items = per-item amounts; each is a Cash Injection. Baked starting cash = 150000."""
    anchors = _OrderAnchors(baked=150000)
    items = [types.SimpleNamespace(id=1000 + i, amount=amt, effect_type="cash",
                                   effect_args={"amount": amt}, name=f"Cash {amt}")
             for i, amt in enumerate(cash_items)]
    ctx = types.SimpleNamespace(
        state=_State(), slot=1, seed_name="seed", slot_data={"starting_money": 50000},
        applier=_OrderApplier(anchors),
        items_received=[_ni(it.id) for it in items],
        game_data=types.SimpleNamespace(item_by_id={it.id: it for it in items}),
        _initial_applied=None, _fresh_reset_done=False, _paused_at_idx=None,
        _cum_warned=set(), _CUM_ANCHOR=PZContext._CUM_ANCHOR,
        _FILLER_OPTION=PZContext._FILLER_OPTION, _FILLER_MULT=PZContext._FILLER_MULT,
        BAKED_STARTING_CASH=PZContext.BAKED_STARTING_CASH,   # anchors bake 150000 = the fingerprint
        _fresh_wait_ticks=0, FRESH_WAIT_MAX=PZContext.FRESH_WAIT_MAX,
        _park_age=object(),                       # park-age feature present (memory mode)
        _park_years_value=park_years_value,
    )
    ctx._park_years = lambda: ctx._park_years_value
    ctx._reconcile_permits = lambda: None
    for name in ("_fresh_signal_pending", "_maybe_fresh_reset", "_apply_starting_money",
                 "_apply_new_items", "_reconcile_cumulative", "_cumulative_targets", "_current_cash",
                 "_money_amount"):
        setattr(ctx, name, types.MethodType(getattr(PZContext, name), ctx))
    return ctx, anchors


def test_room_option_scales_money_by_size():
    """The room's filler_amounts_cash (slot_data) is the MEDIUM amount; Small = half, Large = double.
    data.json's amount is only the fallback when the room lacks the option."""
    ctx, _ = _apply_ctx(park_years_value=0)
    mk = lambda size, amt: types.SimpleNamespace(effect_type="cash",
                                                 effect_args={"size": size, "amount": amt})
    ctx.slot_data = {"filler_amounts_cash": 1000}
    assert ctx._money_amount(mk("small", 250)) == 500     # 1000/2 - option wins over fallback
    assert ctx._money_amount(mk("medium", 500)) == 1000
    assert ctx._money_amount(mk("large", 999)) == 2000
    ctx.slot_data = {}                                     # room without the option -> fallback amount
    assert ctx._money_amount(mk("small", 250)) == 250
    assert ctx._money_amount(mk(None, 123)) == 123         # legacy item without a size tag
    cc = types.SimpleNamespace(effect_type="cc", effect_args={"size": "large", "amount": 400})
    ctx.slot_data = {"filler_amounts_conservation": 300}
    assert ctx._money_amount(cc) == 600                    # cc uses its own option key


def test_holds_items_while_park_age_unresolved():
    """While the fresh signal (park age) is None, item application is held so the baseline can land
    first - nothing is applied and the wait counter advances."""
    ctx, anchors = _apply_ctx(park_years_value=None)
    ctx._apply_new_items()
    assert anchors.order == [], "no items applied while park age is unresolved"
    assert ctx._fresh_wait_ticks == 1, "wait counter advanced"
    assert ctx.state.mark == 0, "high-water mark untouched while holding"


def test_starting_money_set_before_ledger_on_fresh_save():
    """Once park age resolves to a fresh zoo, the $50k baseline is written BEFORE the money ledger adds
    the received sum on top - so final cash = 50000 + injections, never the clobbered $50k."""
    ctx, anchors = _apply_ctx(park_years_value=0)          # fresh (Year 1)
    ctx._apply_new_items()
    assert anchors.order[0] == ("baseline", 50000), f"baseline written first (got {anchors.order})"
    assert [o[0] for o in anchors.order] == ["baseline", "ledger"], \
        "the ledger applies the full received sum as ONE delta AFTER the baseline"
    assert anchors.cash == 50000 + 2000 + 2000, f"final cash = baseline + injections (got {anchors.cash})"
    assert ctx.state.granted.get("cash") == 4000, "ledger persisted the granted total"


def test_ledger_is_idempotent_across_reconnects():
    """A second apply pass (reconnect: AP re-sends the whole item list) must NOT re-grant: the ledger
    delta is 0, so cash is untouched."""
    ctx, anchors = _apply_ctx(park_years_value=0)
    ctx._apply_new_items()
    cash_after = anchors.cash
    ctx._apply_new_items()                                  # same items again (reconnect)
    assert anchors.cash == cash_after, "no double-grant on reconnect (ledger delta 0)"


def test_ledger_grants_only_the_missing_delta():
    """Items that arrive later grant only their increment, on top of whatever the player now has
    (spending is never overwritten - the ledger only ADDS the missing delta)."""
    ctx, anchors = _apply_ctx(park_years_value=0, cash_items=(2000, 2000))
    ctx._apply_new_items()                                  # grants 4000 on the 50k baseline
    anchors.cash -= 30000                                   # player spends
    new_item = types.SimpleNamespace(id=1002, amount=500, effect_type="cash",
                                     effect_args={"amount": 500}, name="Cash 500")
    ctx.game_data.item_by_id[1002] = new_item
    ctx.items_received.append(_ni(1002))
    ctx._apply_new_items()
    assert anchors.cash == 50000 + 4000 - 30000 + 500, "only the +500 delta added onto current cash"
    assert ctx.state.granted.get("cash") == 4500


def test_falls_back_to_applying_after_wait_budget():
    """A disabled/broken park-age anchor (park_years stays None) must not block items forever: after
    FRESH_WAIT_MAX holds, the ledger applies anyway (degraded: no baseline guarantee, old behaviour)."""
    ctx, anchors = _apply_ctx(park_years_value=None)
    for _ in range(PZContext.FRESH_WAIT_MAX):
        ctx._apply_new_items()
    assert anchors.order == [], "still holding right up to the budget"
    ctx._apply_new_items()                                  # budget exhausted -> apply anyway
    assert [o[0] for o in anchors.order] == ["ledger"], "ledger applied on the baked cash (no baseline)"
    assert anchors.cash == 150000 + 4000


def test_fresh_signal_not_pending_without_park_age():
    """Console/no-memory mode (no park-age reader) has no signal to wait for -> never holds."""
    ctx = types.SimpleNamespace(_park_age=None)
    ctx._park_years = lambda: None
    assert PZContext._fresh_signal_pending(ctx) is False
