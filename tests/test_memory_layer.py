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
    _check(set(table.unfilled()) == set(table.anchors), "all anchors unfilled in shipped table")
    _check("research" in table.entity_offsets and "species_birth" in table.entity_offsets,
           "entity_offset groups parsed")

    # --- resolve / read are safe when not attached + unfilled ---------------
    scanner = MemoryScanner(table.process_name)
    _check(not scanner.attached, "scanner not attached initially")
    _check(table.anchors["cash"].resolve(scanner) is None, "unresolved anchor -> None")
    _check(table.read(scanner, "cash") is None, "read of unresolved anchor -> None")

    # --- applier on unfilled table: cumulative stalls (False), no crash -----
    applier = MemoryEffectApplier(scanner, table)
    gd = pz_data.load()
    cash_item = gd.item_by_id[1009]
    # attach() will fail (game not running) -> _ensure_attached False -> apply False.
    _check(applier.apply(cash_item) is False, "cash apply returns False when no game/anchor")

    # --- trigger source polls harmlessly with nothing attached --------------
    fired = []
    ts = MemoryTriggerSource(scanner, table, gd, report_check=fired.append)
    result = ts.poll(already_checked=set())
    _check(result == [] and fired == [], "trigger poll no-ops when not attached")

    print("\nALL MEMORY-LAYER TESTS PASSED")


if __name__ == "__main__":
    main()
