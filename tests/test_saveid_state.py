"""Game-free tests for the applied-items high-water mark + fresh-save re-award (state.ClientState +
client._maybe_fresh_reset).

The fresh-reset (park-age driven) must zero the mark once per fresh zoo so cumulative items (cash/cc)
re-award, latched so a reconnect to the same young zoo doesn't double-grant. The optional save_id scope
must keep distinct saves' marks independent.
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


def _client(state, slot=1):
    """Minimal stand-in carrying only what _maybe_fresh_reset touches (it's a plain method).
    _apply_starting_money is stubbed no-op (its own behaviour is covered in test_starting_money)."""
    return types.SimpleNamespace(state=state, slot=slot, _fresh_reset_done=False,
                                 _apply_starting_money=lambda: None)


def test_fresh_reset_zeroes_mark_and_latches(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)                                            # 5 items applied to a prior save
    applied = PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)  # fresh zoo (Year 1)
    assert applied == 0 and st.get("seed", 1) == 0                  # mark zeroed -> everything re-awards
    assert st.get_fresh_pending("seed", 1) is True                 # latched


def test_no_double_reset_on_reconnect_while_fresh(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)  # session 1: reset + latch
    st.set("seed", 1, 5)                                              # items re-applied
    # session 2: NEW client (fresh_reset_done=False), still Year 1, latch persisted -> no re-reset
    applied = PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)
    assert applied == 5 and st.get("seed", 1) == 5


def test_rearms_after_maturity_then_new_fresh_save(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    PZContext._maybe_fresh_reset(_client(st), "seed", 5, FRESH)   # latch
    PZContext._maybe_fresh_reset(_client(st), "seed", 5, OLD)     # matured (Year 2+) -> re-arm
    assert st.get_fresh_pending("seed", 1) is False
    st.set("seed", 1, 9)
    applied = PZContext._maybe_fresh_reset(_client(st), "seed", 9, FRESH)  # a brand-new save
    assert applied == 0 and st.get("seed", 1) == 0                              # re-awarded again


def test_no_reset_when_age_unknown(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)
    st.set("seed", 1, 5)
    assert PZContext._maybe_fresh_reset(_client(st), "seed", 5, None) == 5
    assert st.get("seed", 1) == 5 and st.get_fresh_pending("seed", 1) is False


def test_first_connect_zero_applied_latches_without_reset(tmp_path):
    st = ClientState.load("seed", 1, state_dir=tmp_path)            # applied=0 (true first connect)
    applied = PZContext._maybe_fresh_reset(_client(st), "seed", 0, FRESH)
    assert applied == 0                                            # nothing to undo
    assert st.get_fresh_pending("seed", 1) is True                # latched -> reconnect won't re-award


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



