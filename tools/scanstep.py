"""scanstep - a *stateless-per-invocation* front-end to memscan, so a scan can
span pauses while the player acts in-game.

memscan's REPL keeps candidate addresses in process memory; that's fine for a
human at a keyboard but not for an automated driver that must hand control back
to the player between every narrowing step. scanstep persists the candidate set
(and value type) to ``tools/.scanstate.json`` and re-attaches on each call, so
each command is an independent process invocation:

    python -m tools.scanstep type double
    python -m tools.scanstep new 75000          # full first scan, saved to disk
    #   (player spends money -> 60000)
    python -m tools.scanstep next 60000          # narrows the saved candidates
    python -m tools.scanstep list
    python -m tools.scanstep write 0x... 65000
    python -m tools.scanstep ptrscan 0x...
    python -m tools.scanstep save cash 0x...

All scanning/pointer/anchor logic is reused from tools.memscan; this module only
adds the persistence + argv dispatch around it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools import memscan  # noqa: E402

STATE_PATH = Path(__file__).resolve().parent / ".scanstate.json"


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"type": "i32", "aligned": True, "candidates": {}}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _session_from_state(scanner: MemoryScanner, state: dict) -> memscan.Session:
    sess = memscan.Session(scanner=scanner, type_=state.get("type", "i32"))
    sess.aligned = state.get("aligned", True)
    # JSON keys are strings; restore int addresses.
    sess.candidates = {int(a): v for a, v in state.get("candidates", {}).items()}
    return sess


def _state_from_session(sess: memscan.Session) -> dict:
    return {
        "type": sess.type_,
        "aligned": sess.aligned,
        "candidates": {str(a): v for a, v in sess.candidates.items()},
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m tools.scanstep <command> [args...]   (see memscan help)")
        return 2
    cmd, args = argv[0].lower(), argv[1:]

    state = _load_state()

    # Commands that don't need the game attached.
    if cmd in ("state", "show"):
        print(json.dumps({k: (v if k != "candidates" else f"{len(v)} addrs")
                          for k, v in state.items()}, indent=2))
        return 0

    scanner = MemoryScanner("PlanetZoo.exe")
    if not scanner.attach():
        print("Could not attach to PlanetZoo.exe. Is the game running?")
        return 1

    sess = _session_from_state(scanner, state)

    handler = memscan.COMMANDS.get(cmd)
    if handler is None:
        print(f"unknown command {cmd!r}; see memscan help")
        return 2
    try:
        handler(sess, args)
    except Exception as e:
        print(f"error: {e}")
        return 1

    _save_state(_state_from_session(sess))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
