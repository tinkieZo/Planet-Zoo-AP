"""Local, persistent client state for idempotent item application (A3).

Why this exists
---------------
Archipelago re-sends the player's **entire** received-items list on every (re)connect
and after any ``Sync``, so application must be idempotent. Two mechanisms:

* the **high-water mark** (``applied_count``): AP guarantees ``items_received`` is an
  ordered, append-only list for a given (seed, slot), so persisting how many positions
  were applied lets a reconnect replay only ``items_received[applied_count:]`` (unlock
  items are also reconciled each tick, so this is mostly an ordering/first-apply aid);
* the **granted ledger** (``granted``): the money authority. Cash/cc items are
  acknowledge-only in the applier; the client reconciles game money by the DELTA
  between the sum of received cash/cc amounts and this ledger. A reconnect is a no-op
  (delta 0), a fresh save re-grants the full sum as one addition (``reset_granted``),
  and a player's spending is never overwritten (only the missing delta is ADDED).

The mark is keyed by (seed_name, slot[, **save_id**]). ``save_id`` is an OPTIONAL
per-save scope: if a caller passes one, the mark is scoped to that game save; the
client currently passes None (the (seed, slot) key) and detects a fresh save via
the park-age signal instead (see :mod:`pz_ap_client.memory.zoodate` /
``_maybe_fresh_reset``), which zeroes the mark so cumulative cash/cc re-award on a
brand-new zoo. The ``save_id`` plumbing is retained as a clean optional capability.

The state file is keyed by these so multiple seeds / slots / saves on one machine
don't clobber each other. It is written atomically (temp + replace) so a crash
mid-write can't corrupt it.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

if getattr(sys, "frozen", False):
    # Packaged exe: keep state in a stable per-user dir. A path inside the PyInstaller bundle is wiped
    # when the bundle is replaced (app update / reinstall), which would drop the high-water mark + fresh
    # latch and spuriously re-award cumulative items. %LOCALAPPDATA%\PlanetZooAP survives that.
    DEFAULT_STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PlanetZooAP" / "client_state"
else:
    DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".client_state"


def _slot_key(seed_name: str, slot: int, save_id: "str | None" = None) -> str:
    base = f"{seed_name}:{slot}"
    return f"{base}:{save_id}" if save_id else base


@dataclass
class ClientState:
    """Per-(seed, slot) applied high-water mark + fresh-save latch + cumulative GRANTED ledger,
    backed by a JSON file."""

    path: Path
    # slot_key -> number of received-item list positions already applied
    applied_count: Dict[str, int] = field(default_factory=dict)
    # (seed:slot) -> True once a fresh zoo has had its cumulative items handled; re-armed (False) when the
    # zoo matures past the fresh sim-time threshold, so a LATER brand-new save re-awards but a reconnect to
    # the same young zoo does not. Survives client restarts (that's the whole point - it's persisted).
    fresh_pending: Dict[str, bool] = field(default_factory=dict)
    # slot_key -> {"cash": total, "cc": total} GRANTED so far on this save. The money authority: the client
    # reconciles game money by the DELTA between the sum of received cash/cc items and this ledger, so a
    # reconnect never double-grants (delta 0) and a fresh save re-grants everything as ONE addition
    # (reset_granted -> delta = full sum). Replaces per-item read-modify-write through the high-water mark,
    # whose replay arithmetic broke whenever application stalled mid-list or the mark was zeroed at the
    # wrong moment.
    granted: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, seed_name: str, slot: int, state_dir: "str | Path | None" = None) -> "ClientState":
        state_dir = Path(state_dir or DEFAULT_STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        # One file per seed keeps things readable; slots live as keys inside it.
        safe_seed = "".join(c if c.isalnum() or c in "-_." else "_" for c in seed_name)
        path = state_dir / f"{safe_seed or 'seed'}.json"
        applied: Dict[str, int] = {}
        pending: Dict[str, bool] = {}
        granted: Dict[str, Dict[str, float]] = {}
        if path.exists():
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                applied = blob.get("applied_count", {})
                pending = blob.get("fresh_pending", {})
                granted = blob.get("granted", {})
            except (json.JSONDecodeError, OSError):
                # Corrupt state: start fresh rather than crash. Worst case is a
                # re-grant, which the high-water mark will then re-establish.
                applied, pending, granted = {}, {}, {}
        return cls(path=path, applied_count=applied, fresh_pending=pending, granted=granted)

    def get(self, seed_name: str, slot: int, save_id: "str | None" = None) -> int:
        return self.applied_count.get(_slot_key(seed_name, slot, save_id), 0)

    def set(self, seed_name: str, slot: int, count: int, save_id: "str | None" = None) -> None:
        self.applied_count[_slot_key(seed_name, slot, save_id)] = count
        self._flush()

    def get_fresh_pending(self, seed_name: str, slot: int) -> bool:
        return bool(self.fresh_pending.get(_slot_key(seed_name, slot)))

    def set_fresh_pending(self, seed_name: str, slot: int, value: bool) -> None:
        self.fresh_pending[_slot_key(seed_name, slot)] = value
        self._flush()

    # -- cumulative granted ledger (cash / cc) ------------------------------------------------------

    def get_granted(self, seed_name: str, slot: int, kind: str, save_id: "str | None" = None) -> float:
        return float(self.granted.get(_slot_key(seed_name, slot, save_id), {}).get(kind, 0.0))

    def set_granted(self, seed_name: str, slot: int, kind: str, total: float,
                    save_id: "str | None" = None) -> None:
        self.granted.setdefault(_slot_key(seed_name, slot, save_id), {})[kind] = float(total)
        self._flush()

    def reset_granted(self, seed_name: str, slot: int, save_id: "str | None" = None) -> None:
        """Zero the granted ledger (a FRESH save: everything received re-grants as one delta)."""
        self.granted.pop(_slot_key(seed_name, slot, save_id), None)
        self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"applied_count": self.applied_count, "fresh_pending": self.fresh_pending,
                        "granted": self.granted}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # atomic on Windows + POSIX
