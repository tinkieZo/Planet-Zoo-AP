"""Game-free tests for the applied-items high-water mark + fresh-save re-award (state.ClientState +
client._maybe_fresh_reset).

Fresh detection is the BAKED-BALANCE FINGERPRINT: a park below the age threshold whose cash reads
EXACTLY the scenario's baked starting balance has never been handled by the client (the handling
itself - starting-money write + ledger grant - immediately changes the balance). So it fires once per
new save, never on a reconnect to an already-handled young zoo, and it correctly fires on EVERY
repeated Year-1 scenario restart (the case that permanently jammed the old fresh_pending maturity
latch). The optional save_id scope must keep distinct saves' marks independent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types  # noqa: E402

from pz_ap_client.client import PZContext  # noqa: E402
from pz_ap_client.state import ClientState  # noqa: E402
from pz_ap_client.memory.zoodate import FRESH_YEARS  # noqa: E402

FRESH, OLD = 0, FRESH_YEARS          # Year 1 == 0 years open (fresh); >= FRESH_YEARS == matured
BAKED = PZContext.BAKED_STARTING_CASH


def _client(state, cash=BAKED, slot=1):
    """Minimal stand-in carrying only what _maybe_fresh_reset touches (it's a plain method).
    ``cash`` = what the cash anchor reads (BAKED == untouched fresh park). _apply_starting_money is
    stubbed no-op (its own behaviour is covered in test_starting_money)."""
    return types.SimpleNamespace(state=state, slot=slot, _fresh_reset_done=False,
                                 BAKED_STARTING_CASH=BAKED,
                                 _current_cash=lambda: cash,
                                 _apply_starting_money=lambda: None)


def test_fresh_reset_zeroes_mark_and_ledger(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)                                            # 5 items applied to a prior save
    st.set_granted("seed", 1, "cash", 4000.0)                       # prior save's money ledger
    applied = PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)  # fresh zoo, baked balance
    assert applied == 0 and st.get("seed", 1) == 0                  # mark zeroed -> everything re-awards
    assert st.get_granted("seed", 1, "cash") == 0.0                 # ledger zeroed -> full money re-grant


def test_no_reset_on_reconnect_while_fresh(tmp_path):
    """Reconnect to an already-handled young zoo: the balance is no longer the untouched baked value
    (starting money + grants changed it), so the fingerprint is absent -> no re-award."""
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    st.set_granted("seed", 1, "cash", 4000.0)
    applied = PZContext._maybe_fresh_reset(_client(st, cash=54000.0), "seed", 5, FRESH)
    assert applied == 5 and st.get("seed", 1) == 5
    assert st.get_granted("seed", 1, "cash") == 4000.0              # ledger untouched -> no double-grant


def test_repeated_year1_restart_fires_every_time(tmp_path):
    """The case the old maturity latch got permanently stuck on: restarting the scenario (a NEW Year-1
    save) after a previous fresh save was handled. Every restart reads the baked balance again, so each
    one is detected and re-awarded."""
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)     # restart 1: handled
    st.set("seed", 1, 5)                                            # items re-applied on that save
    st.set_granted("seed", 1, "cash", 4000.0)
    applied = PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)  # restart 2: baked again
    assert applied == 0 and st.get("seed", 1) == 0
    assert st.get_granted("seed", 1, "cash") == 0.0


def test_no_refire_within_episode(tmp_path):
    """If the handling is a visible no-op (room starting_money == baked, no money items yet), the
    fingerprint persists - the episode guard stops it refiring every tick on the SAME client."""
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    ctx = _client(st)
    assert PZContext._maybe_fresh_reset(ctx, "seed", 5, FRESH) == 0     # fires
    st.set("seed", 1, 3)                                                # some items re-applied
    assert PZContext._maybe_fresh_reset(ctx, "seed", 3, FRESH) == 3     # same episode -> no refire
    assert st.get("seed", 1) == 3


def test_no_reset_when_age_unknown_or_matured(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    assert PZContext._maybe_fresh_reset(_client(st), "seed", 5, None) == 5   # age unknown -> fail safe
    assert PZContext._maybe_fresh_reset(_client(st), "seed", 5, OLD) == 5    # matured -> not fresh
    assert st.get("seed", 1) == 5


def test_no_reset_when_cash_unreadable_or_played(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    # cash anchor unresolved (mid-load) -> fail safe
    assert PZContext._maybe_fresh_reset(_client(st, cash=None), "seed", 5, FRESH) == 5
    # new scenario PLAYED before first connect (spent money) -> fingerprint missed (documented limitation)
    assert PZContext._maybe_fresh_reset(_client(st, cash=142500.0), "seed", 5, FRESH) == 5
    assert st.get("seed", 1) == 5


def test_state_scopes_by_save_id(tmp_path):
    st = ClientState.load("seedX", 1, state_dir=tmp_path)
    st.set("seedX", 1, 5)             # legacy (no save id)
    st.set("seedX", 1, 2, "saveA")
    st.set("seedX", 1, 7, "saveB")
    assert st.get("seedX", 1) == 5
    assert st.get("seedX", 1, "saveA") == 2
    assert st.get("seedX", 1, "saveB") == 7
    # a brand-new zoo's id has no mark -> 0 => every cumulative item re-awards
    assert st.get("seedX", 1, "saveC") == 0
    # and that's persisted across client restarts
    st2 = ClientState.load("seedX", 1, state_dir=tmp_path)
    assert st2.get("seedX", 1, "saveA") == 2
    assert st2.get("seedX", 1, "saveC") == 0
    assert st2.get("seedX", 1) == 5


def test_granted_ledger_persists_and_scopes(tmp_path):
    st = ClientState.load("seedY", 1, state_dir=tmp_path)
    st.set_granted("seedY", 1, "cash", 12000.0)
    st.set_granted("seedY", 1, "cc", 3000.0)
    st2 = ClientState.load("seedY", 1, state_dir=tmp_path)          # client restart
    assert st2.get_granted("seedY", 1, "cash") == 12000.0
    assert st2.get_granted("seedY", 1, "cc") == 3000.0
    st2.reset_granted("seedY", 1)                                    # fresh save
    assert st2.get_granted("seedY", 1, "cash") == 0.0
    st3 = ClientState.load("seedY", 1, state_dir=tmp_path)
    assert st3.get_granted("seedY", 1, "cc") == 0.0
