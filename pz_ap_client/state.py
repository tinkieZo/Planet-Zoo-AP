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

The state file is keyed by (seed_name, slot) so multiple seeds / slots on one
machine don't clobber each other. It is written atomically (temp + replace) so a
crash mid-write can't corrupt it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".client_state"


def _slot_key(seed_name: str, slot: int) -> str:
    return f"{seed_name}:{slot}"


@dataclass
class ClientState:
    """Per-(seed, slot) applied high-water mark, backed by a JSON file."""

    path: Path
    # slot_key -> number of received-item list positions already applied
    applied_count: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, seed_name: str, slot: int, state_dir: "str | Path | None" = None) -> "ClientState":
        state_dir = Path(state_dir or DEFAULT_STATE_DIR)
        state_dir.mkdir(parents=True, exist_ok=True)
        # One file per seed keeps things readable; slots live as keys inside it.
        safe_seed = "".join(c if c.isalnum() or c in "-_." else "_" for c in seed_name)
        path = state_dir / f"{safe_seed or 'seed'}.json"
        applied: Dict[str, int] = {}
        if path.exists():
            try:
                applied = json.loads(path.read_text(encoding="utf-8")).get("applied_count", {})
            except (json.JSONDecodeError, OSError):
                # Corrupt state: start fresh rather than crash. Worst case is a
                # re-grant, which the high-water mark will then re-establish.
                applied = {}
        return cls(path=path, applied_count=applied)

    def get(self, seed_name: str, slot: int) -> int:
        return self.applied_count.get(_slot_key(seed_name, slot), 0)

    def set(self, seed_name: str, slot: int, count: int) -> None:
        self.applied_count[_slot_key(seed_name, slot)] = count
        self._flush()

    def _flush(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"applied_count": self.applied_count}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)  # atomic on Windows + POSIX
