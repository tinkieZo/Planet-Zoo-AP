"""Game-free tests for the memory layer scaffold.

Covers the parts that don't need a running game: AOB parsing, anchor-table
loading/validation, and graceful "unfilled / not attached" behaviour (resolve
returns None, appliers/triggers no-op rather than crash).

Run:  python -m tests.test_memory_layer
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client import data as pz_data  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner, parse_aob, MemoryAccessError  # noqa: E402
from pz_ap_client.memory.anchors import AnchorTable  # noqa: E402
from pz_ap_client.memory.applier import MemoryEffectApplier  # noqa: E402
from pz_ap_client.memory.triggers import MemoryTriggerSource  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    print(("PASS" if cond else "FAIL"), "-", msg)
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    # --- parse_aob ----------------------------------------------------------
    _check(parse_aob("48 8B 05") == b"\x48\x8b\x05", "parse_aob plain bytes")
    _check(parse_aob("48 ?? 05") == b"\x48.\x05", "parse_aob single wildcard")
    _check(parse_aob("90 ?? ?? ?? 90") == b"\x90...\x90", "parse_aob multi wildcard")
    # A byte that is regex-special (0x2e == '.') must be escaped to match literally.
    _check(parse_aob("2E") == b"\\\x2e", "parse_aob escapes regex-special byte 0x2e")
    try:
        parse_aob("ZZ")
        _check(False, "parse_aob should reject bad token")
    except MemoryAccessError:
        _check(True, "parse_aob rejects bad token")

    # --- anchor table loads + ships unfilled --------------------------------
    table = AnchorTable.load()
    _check(table.process_name == "PlanetZoo.exe", "anchors process name")
    expected = {"cash", "conservation_credits", "zoo_rating", "guest_count",
                "conservation_release_count", "research_state_base",
                "species_roster_base", "birth_event_counter"}
    _check(expected <= set(table.anchors), "all expected anchors present")
    # As the A2 spike fills anchors, unfilled() shrinks. Invariant: unfilled() lists exactly the
    # not-yet-resolvable, NON-superseded anchors; everything else is either filled or superseded
    # (superseded = solved via a code hook / reader, intentionally never data-filled).
    _check(all(not table.anchors[n].filled and not table.anchors[n].superseded for n in table.unfilled()),
           "unfilled() lists exactly the not-yet-resolvable, non-superseded anchors")
    _check(all(table.anchors[n].filled or table.anchors[n].superseded
               for n in set(table.anchors) - set(table.unfilled())),
           "anchors not in unfilled() are filled or superseded")
    _check("research" in table.entity_offsets and "species_birth" in table.entity_offsets,
           "entity_offset groups parsed")

    # --- resolve / read are safe when not attached --------------------------
    scanner = MemoryScanner(table.process_name)
    _check(not scanner.attached, "scanner not attached initially")
    # resolve() guards on attachment first, so even a filled anchor -> None here.
    _check(table.anchors["cash"].resolve(scanner) is None, "resolve -> None when not attached")
    _check(table.read(scanner, "cash") is None, "read -> None when not attached")
    # Any still-unfilled anchor must resolve to None even once attached.
    _unfilled = table.unfilled()
    if _unfilled:
        _check(not table.anchors[_unfilled[0]].filled, "unfilled anchor reports filled=False")

    # --- applier on unfilled table: cumulative stalls (False), no crash -----
    # This is a game-free test. If a real game IS running on a dev box, attaching
    # to it and applying a (now-filled) cash item would mutate the player's live
    # save, so skip the live-write assertions when a game is actually attachable.
    applier = MemoryEffectApplier(scanner, table)
    gd = pz_data.load()
    # Resolve by EFFECT, not a hardcoded id (ids are the APWorld's and reflow across versions).
    # NB the old hardcoded 1009 had drifted onto a research_reward item; cash itself is
    # ledger-acknowledged (on_cash returns True regardless of attach), so use one of each.
    cash_item = next(i for i in gd.items if i.effect_type == "cash")
    reward_item = next(i for i in gd.items if i.effect_type == "research_reward")
    _game_running = MemoryScanner(table.process_name).attach()
    if _game_running:
        print("SKIP - apply/trigger live-write checks (a game process is attached)")
    else:
        # attach() fails (no game): memory-writing effects return False (retry later); cash is
        # acknowledge-only (the granted-ledger reconcile applies it) so it returns True even here.
        _check(applier.apply(reward_item) is False, "research_reward apply returns False when no game")
        _check(applier.apply(cash_item) is True, "cash apply is ledger-acknowledged even when no game")

        # --- trigger source polls harmlessly with nothing attached ----------
        fired = []
        ts = MemoryTriggerSource(scanner, table, gd, report_check=fired.append)
        result = ts.poll(already_checked=set())
        _check(result == [] and fired == [], "trigger poll no-ops when not attached")

    print("\nALL MEMORY-LAYER TESTS PASSED")


def test_memory_layer() -> None:
    """pytest entry point - runs the memory-layer checks (each asserts via _check)."""
    main()


def test_first_research_snapshot_diagnostic(caplog) -> None:
    """Foreign report 20260812_Ollieberry: a fresh AP park read ALL 57 mechanic research locations
    complete on the first snapshot (engine fell back to the pre-completed `default` mechanic
    config) and sent them all as checks. The one-shot diagnostic must log the mechanic/welfare
    completeness split and ERROR on the 100%-mechanic-complete signature - and stay quiet on a
    healthy (all-locked) first read."""
    table = AnchorTable.load()
    scanner = MemoryScanner(table.process_name)
    gd = pz_data.load()
    ts = MemoryTriggerSource(scanner, table, gd, report_check=lambda _i: None)

    # Fallback signature: every mechanic key reads complete, welfare stays locked.
    ts.research.is_research_complete = lambda key, snap=None, level=None: not key.startswith("welfare_")
    with caplog.at_level("INFO", logger="PZClient"):
        ts._log_first_research_snapshot(({}, {}))
    assert any("first research snapshot" in r.getMessage() for r in caplog.records)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and "read COMPLETE on the first research snapshot" in errors[0].getMessage()

    # Healthy fresh park: nothing complete -> summary only, no ERROR.
    caplog.clear()
    ts.research.is_research_complete = lambda key, snap=None, level=None: False
    with caplog.at_level("INFO", logger="PZClient"):
        ts._log_first_research_snapshot(({}, {}))
    assert any("first research snapshot" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_research_quarantine_guard(caplog) -> None:
    """Research quarantine guard (report 20260812_Ollieberry): first-snapshot research checks HOLD
    until the fresh-park signal resolves; a FRESH park reading 100% mechanic complete quarantines
    mechanic checks for the session (welfare still fires); an established park releases everything;
    an unresolved signal fails OPEN after the time bound; park unload re-arms the guard; and with
    no probe wired (legacy callers) the guard is off entirely."""
    import time as _time
    table = AnchorTable.load()
    gd = pz_data.load()
    research_locs = gd.locations_by_trigger("research_complete")
    mech_ids = {l.id for l in research_locs
                if not (l.trigger_args.get("research_key") or "").startswith("welfare_")}
    assert len(mech_ids) >= 10          # below the guard floor the signature is meaningless
    verdict = {"v": None}

    def make_ts():
        ts = MemoryTriggerSource(MemoryScanner(table.process_name), table, gd,
                                 report_check=lambda _i: None,
                                 fresh_park_verdict=lambda: verdict["v"])
        # The Ollieberry fallback signature: every mechanic key complete, welfare locked.
        ts.research.is_research_complete = \
            lambda key, snap=None, level=None: not key.startswith("welfare_")
        ts.research._snapshot = lambda: ({}, {})
        return ts

    # HOLD: signal unresolved -> nothing reported, detection retries next tick.
    ts = make_ts()
    with caplog.at_level("INFO", logger="PZClient"):
        assert ts._poll_research(set()) == []
    assert ts._research_gate == "hold"
    assert any("holding research checks" in r.getMessage() for r in caplog.records)

    # QUARANTINE: fresh park + 100% mechanic -> mechanic suppressed, ERROR logged...
    caplog.clear()
    verdict["v"] = True
    with caplog.at_level("INFO", logger="PZClient"):
        assert ts._poll_research(set()) == []
    assert ts._research_gate == "quarantine"
    assert any("QUARANTINE" in r.getMessage() for r in caplog.records
               if r.levelname == "ERROR")
    # ...but welfare checks still fire through the quarantine.
    wkey = next((l.trigger_args.get("research_key") or "") for l in research_locs
                if (l.trigger_args.get("research_key") or "").startswith("welfare_"))
    ts.research.is_research_complete = \
        lambda key, snap=None, level=None: not key.startswith("welfare_") or key == wkey
    fired = ts._poll_research(set())
    assert fired and not (set(fired) & mech_ids)

    # RELEASE: established park -> everything fires (retroactive checks are legit).
    verdict["v"] = False
    ts2 = make_ts()
    assert set(ts2._poll_research(set())) == mech_ids
    assert ts2._research_gate == "open"

    # FAIL OPEN: signal never resolves -> release unguarded after the bound (with a warning).
    caplog.clear()
    verdict["v"] = None
    ts3 = make_ts()
    ts3._research_hold_t0 = _time.monotonic() - 999
    with caplog.at_level("INFO", logger="PZClient"):
        assert set(ts3._poll_research(set())) == mech_ids
    assert any(r.levelname == "WARNING" and "unresolved" in r.getMessage()
               for r in caplog.records)

    # RESET: park unload re-arms the hold for the next park.
    ts3.reset_park_session()
    assert ts3._research_gate == "hold" and ts3._research_snap_logged is False
    assert ts3._poll_research(set()) == []

    # GUARD OFF: no probe wired (legacy/test callers) -> original immediate behaviour.
    ts4 = MemoryTriggerSource(MemoryScanner(table.process_name), table, gd,
                              report_check=lambda _i: None)
    ts4.research.is_research_complete = \
        lambda key, snap=None, level=None: not key.startswith("welfare_")
    ts4.research._snapshot = lambda: ({}, {})
    assert set(ts4._poll_research(set())) == mech_ids


if __name__ == "__main__":
    main()
