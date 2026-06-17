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
