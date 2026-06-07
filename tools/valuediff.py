"""valuediff — unknown-value changed/unchanged differential, coordinated with player actions.

The CE "unknown initial value" hunt: snapshot all i32s, then repeatedly keep only addresses that
CHANGED (or stayed UNCHANGED) since the last snapshot, narrowing to a variable that tracks some
in-game state (e.g. the current terrain tool/action id). scanstep needs a known value; this doesn't.

Runs as ONE long-lived process (so the big first snapshot stays in RAM) driven by a control file so
the player can act between steps:
    python -m tools.valuediff            # (run in background) takes the initial snapshot
then write a word to tools/.valuediff_cmd (e.g. `echo changed > tools/.valuediff_cmd`):
    changed    keep addrs whose value changed since the last scan (run AFTER switching tool)
    unchanged  keep addrs that stayed the same (run after doing nothing — kills churn)
    list       print up to 40 survivors with their last-scan values
    now        re-read + print survivors' CURRENT values (to record a tool's id)
    quit       restore nothing (read-only tool) and exit
Tip: PAUSE the game so only the tool selection changes between snapshots — far less churn.
Scans <=1MB writable regions (the UI/input state lives in small heaps), i32 view.
"""
from __future__ import annotations
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools.memscan import iter_regions  # noqa: E402

CTRL = Path(__file__).resolve().parent / ".valuediff_cmd"
CAP = 1024 * 1024


def _readreg(s, b, sz):
    """Read a region as a copied little-endian i32 array, or None on failure."""
    try:
        d = s.read_bytes(b, sz)
        n = (len(d) // 4) * 4
        return np.frombuffer(d[:n], dtype="<i4").copy()
    except Exception:
        return None


def _read_i32(s, a):
    """Read one i32 at `a`, or the string '?' on failure (for display)."""
    try:
        return struct.unpack("<i", s.read_bytes(a, 4))[0]
    except Exception:
        return "?"


def _reduce_full(s, regs, snap, want_changed):
    """First reduction: per region, compare snapshot vs current and keep addr->value for the i32 slots
    that changed (or stayed unchanged)."""
    surv = {}
    for b, sz in regs:
        old = snap.get(b)
        cur = _readreg(s, b, sz)
        if old is None or cur is None or len(old) != len(cur):
            continue
        idx = np.nonzero(old != cur)[0] if want_changed else np.nonzero(old == cur)[0]
        for i in idx:
            surv[b + int(i) * 4] = int(cur[i])
    return surv


def _reduce_surv(s, survivors, want_changed):
    """Subsequent reduction: re-read each survivor and keep those whose changed-ness matches."""
    surv = {}
    for addr, old in survivors.items():
        cur = _read_i32(s, addr)
        if cur != "?" and (cur != old) == want_changed:
            surv[addr] = cur
    return surv


def _do_reduce(s, regs, snap, survivors, want_changed):
    """Run the full reduction on the first pass (then drop the snapshot) or the survivor-only reduction
    thereafter. Returns the updated (snap, survivors)."""
    survivors = _reduce_full(s, regs, snap, want_changed) if survivors is None else \
        _reduce_surv(s, survivors, want_changed)
    if snap is not None:
        snap = None
    return snap, survivors


def _print_survivors(s, survivors, now):
    """Print up to 40 survivors — their last-scan values, or freshly re-read current values when `now`."""
    if not survivors:
        print("(no survivors yet — run 'changed' first)", flush=True)
        return
    for a in list(survivors)[:40]:
        v = _read_i32(s, a) if now else survivors[a]
        print("  0x%X = %s" % (a, v), flush=True)
    print("(%d total)" % len(survivors), flush=True)


def _read_cmd():
    """Return the pending control-file command (lowercased), consuming the file, or None if absent."""
    if not CTRL.exists():
        return None
    cmd = CTRL.read_text().strip().lower()
    try:
        CTRL.unlink()
    except Exception:
        pass
    return cmd


def _dispatch(s, regs, snap, survivors, cmd):
    """Handle one command; return the updated (snap, survivors, should_quit)."""
    if cmd == "quit":
        print("quit", flush=True)
        return snap, survivors, True
    if cmd in ("changed", "unchanged"):
        snap, survivors = _do_reduce(s, regs, snap, survivors, cmd == "changed")
        print("after %s: %d survivors" % (cmd, len(survivors)), flush=True)
    elif cmd in ("list", "now"):
        _print_survivors(s, survivors, cmd == "now")
    else:
        print("unknown cmd %r" % cmd, flush=True)
    return snap, survivors, False


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    regs = [(b, sz) for b, sz in iter_regions(s.pm.process_handle, writable_only=True) if sz <= CAP]
    snap = {b: _readreg(s, b, sz) for b, sz in regs}   # per-region arrays until first reduce
    survivors = None                                    # dict addr->last value after first reduce
    if CTRL.exists():
        CTRL.unlink()
    print("snapshot over %d regions (<=1MB). Switch tool, then: echo changed > %s" % (len(regs), CTRL), flush=True)

    while True:
        cmd = _read_cmd()
        if cmd is None:
            time.sleep(0.4); continue
        snap, survivors, done = _dispatch(s, regs, snap, survivors, cmd)
        if done:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
