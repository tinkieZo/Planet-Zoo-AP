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


# --- ordering: starting money must be set BEFORE cash items land on top (the $50k-clobber bug) ------

class _OrderAnchors:
    """Records every cash write in order; cash items add, the baseline write overwrites."""
    def __init__(self, baked):
        self.cash = baked
        self.order = []  # ("baseline", amt) | ("item", id, cash_after)

    def write(self, scanner, name, value):
        if name != "cash":
            return False
        self.cash = value
        self.order.append(("baseline", value))
        return True


class _OrderApplier:
    def __init__(self, anchors):
        self.anchors = anchors
        self.scanner = object()

    def apply(self, item):
        self.anchors.cash += item.amount          # a Cash Injection adds on top of current cash
        self.anchors.order.append(("item", item.id, self.anchors.cash))
        return True


class _State:
    def __init__(self):
        self.mark = 0
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
    """A namespace driving the REAL _apply_new_items with controllable park age. cash_items = the
    per-item amounts; each is a Cash Injection. Baked starting cash = 150000 (scenario default)."""
    anchors = _OrderAnchors(baked=150000)
    items = [types.SimpleNamespace(id=1000 + i, amount=amt) for i, amt in enumerate(cash_items)]
    ctx = types.SimpleNamespace(
        state=_State(), slot=1, seed_name="seed", slot_data={"starting_money": 50000},
        applier=_OrderApplier(anchors),
        items_received=[_ni(it.id) for it in items],
        game_data=types.SimpleNamespace(item_by_id={it.id: it for it in items}),
        _initial_applied=None, _fresh_reset_done=False,
        _fresh_wait_ticks=0, FRESH_WAIT_MAX=PZContext.FRESH_WAIT_MAX,
        _park_age=object(),                       # park-age feature present (memory mode)
        _park_years_value=park_years_value,
    )
    ctx._park_years = lambda: ctx._park_years_value
    ctx._reconcile_permits = lambda: None
    for name in ("_fresh_signal_pending", "_maybe_fresh_reset", "_apply_starting_money", "_apply_new_items"):
        setattr(ctx, name, types.MethodType(getattr(PZContext, name), ctx))
    return ctx, anchors


def test_holds_items_while_park_age_unresolved():
    """While the fresh signal (park age) is None, item application is held so the baseline can land
    first - nothing is applied and the wait counter advances."""
    ctx, anchors = _apply_ctx(park_years_value=None)
    ctx._apply_new_items()
    assert anchors.order == [], "no items applied while park age is unresolved"
    assert ctx._fresh_wait_ticks == 1, "wait counter advanced"
    assert ctx.state.mark == 0, "high-water mark untouched while holding"


def test_starting_money_set_before_items_on_fresh_save():
    """Once park age resolves to a fresh zoo, the $50k baseline is written BEFORE the cash items add
    on top - so final cash = 50000 + injections, never the clobbered $50k."""
    ctx, anchors = _apply_ctx(park_years_value=0)          # fresh (Year 1)
    ctx._apply_new_items()
    assert anchors.order[0] == ("baseline", 50000), f"baseline written first (got {anchors.order})"
    assert [o[0] for o in anchors.order] == ["baseline", "item", "item"], "items apply AFTER the baseline"
    assert anchors.cash == 50000 + 2000 + 2000, f"final cash = baseline + injections (got {anchors.cash})"


def test_falls_back_to_applying_after_wait_budget():
    """A disabled/broken park-age anchor (park_years stays None) must not block items forever: after
    FRESH_WAIT_MAX holds, items apply anyway (degraded: no baseline guarantee, old behaviour)."""
    ctx, anchors = _apply_ctx(park_years_value=None)
    for _ in range(PZContext.FRESH_WAIT_MAX):
        ctx._apply_new_items()
    assert anchors.order == [], "still holding right up to the budget"
    ctx._apply_new_items()                                  # budget exhausted -> apply anyway
    assert [o[0] for o in anchors.order] == ["item", "item"], "items applied on the baked cash (no baseline)"


def test_fresh_signal_not_pending_without_park_age():
    """Console/no-memory mode (no park-age reader) has no signal to wait for -> never holds."""
    ctx = types.SimpleNamespace(_park_age=None)
    ctx._park_years = lambda: None
    assert PZContext._fresh_signal_pending(ctx) is False
