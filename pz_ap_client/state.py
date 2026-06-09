"""Local, persistent client state for idempotent item application (A3).

Why this exists
---------------
Several effects are *cumulative* and non-idempotent: ``cash`` and ``cc`` add money,
``staff_training`` bumps a level. Archipelago re-sends the player's **entire**
received-items list on every (re)connect and after any ``Sync``. If we naively
applied ``items_received`` each time, a reconnect would re-grant every cash item
and the player would end up rich.

AP guarantees ``items_received`` is an ordered, append-only list for a given
(seed, slot): index *i* always refers to the same received item forever. So the
fix is a **high-water mark**: persist how many list positions we've already
applied. On reconnect we replay only ``items_received[applied_count:]``.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".client_state"


def _slot_key(seed_name: str, slot: int, save_id: "str | None" = None) -> str:
    base = f"{seed_name}:{slot}"
    return f"{base}:{save_id}" if save_id else base


@dataclass
class ClientState:
    """Per-(seed, slot) applied high-water mark + fresh-save latch, backed by a JSON file."""

    path: Path
    # slot_key -> number of received-item list positions already applied
    applied_count: Dict[str, int] = field(default_factory=dict)
    # (seed:slot) -> True once a fresh zoo has had its cumulative items handled; re-armed (False) when the
    # zoo matures past the fresh sim-time threshold, so a LATER brand-new save re-awards but a reconnect to
    # the same young zoo does not. Survives client restarts (that's the whole point - it's persisted).
    fresh_pending: Dict[str, bool] = field(default_factory=dict)

    @classmethod
    def load(cls, seed_name: str, slot: int, state_dir: "str | Path | None" = None) -> "ClientState":
        state_dir = Path(state_dir or DEFAULT_STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        # One file per seed keeps things readable; slots live as keys inside it.
        safe_seed = "".join(c if c.isalnum() or c in "-_." else "_" for c in seed_name)
        path = state_dir / f"{safe_seed or 'seed'}.json"
        applied: Dict[str, int] = {}
        pending: Dict[str, bool] = {}
        if path.exists():
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                applied = blob.get("applied_count", {})
                pending = blob.get("fresh_pending", {})
            except (json.JSONDecodeError, OSError):
                # Corrupt state: start fresh rather than crash. Worst case is a
                # re-grant, which the high-water mark will then re-establish.
                applied, pending = {}, {}
        return cls(path=path, applied_count=applied, fresh_pending=pending)

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

    def _flush(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"applied_count": self.applied_count, "fresh_pending": self.fresh_pending}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # atomic on Windows + POSIX
