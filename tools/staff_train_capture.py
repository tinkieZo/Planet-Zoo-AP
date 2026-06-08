"""staff_train_capture - capture a live keeper object + inspect its training structure.

GetStaffMemberCurrentTrainingLevel (0x146B1B6F0) getargs the staff member into r15 (set at 0x146B1B76D),
then reads its training: trainingComp = [keeper+0x100]; map @ trainingComp+0x400 (entry+8 = current level
index); per-level defs @ trainingComp+0x450 (0x300-byte records). We hook right after r15 is set
(0x146B1B770, ORIG `mov edx,0xffffffff`), ring-capture r15 = the keeper object (read-only), then dump its
structure + scan for a manager/roster back-pointer to drive enumeration of all keepers for the +N boost.

    python -m tools.staff_train_capture [seconds=90]
Open a KEEPER's info / training panel in-game during the window (that fires the getter).
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools._capture import install_ring_capture, poll_ring, read_qword, LO, HI  # noqa: E402

RVA = 0x6B1B770
ORIG = bytes.fromhex("baffffffff")  # mov edx, 0xffffffff


def _dump_training(s, kp) -> None:
    """Dump the keeper's training component: map header (count/cap/buckets) + per-level defs ptr."""
    comp = read_qword(s, kp + 0x100)
    print("  [keeper+0x100] training component = 0x%X" % (comp or 0), flush=True)
    if not comp:
        return
    mp = comp + 0x400
    print("  trainingComp+0x400 map: [+0]=0x%X [+8]=0x%X [+0x10]=0x%X" % (
        read_qword(s, mp) or 0, read_qword(s, mp + 0x8) or 0, read_qword(s, mp + 0x10) or 0), flush=True)
    print("  trainingComp+0x450 defs ptr = 0x%X" % (read_qword(s, comp + 0x450) or 0), flush=True)


def _scan_header(s, base, kp) -> None:
    """Scan the keeper header (+0x00..+0xF8) for plausible module (vtable) or heap back-pointers."""
    print("  keeper header pointers (+0x00..+0xF8):", flush=True)
    for off in range(0, 0x100, 8):
        v = read_qword(s, kp + off)
        if v and (base <= v < base + 0x10000000 or LO < v < HI):
            tag = "MODULE(vtable?)" if base <= v < base + 0x10000000 else "heap"
            print("    +0x%03X = 0x%X  %s" % (off, v, tag), flush=True)


def _dump_keeper(s, base, kp, n) -> None:
    print("\n=== keeper obj 0x%X (x%d) ===" % (kp, n), flush=True)
    _dump_training(s, kp)
    _scan_header(s, base, kp)


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    inst = install_ring_capture("stc", RVA, ORIG, 0x6B1B775, "r15")
    if inst is None:
        return 1
    s, hm, scratch = inst
    print("INSTALLED staff-training capture @0x%X" % (s.module_base + RVA), flush=True)
    print(">>> Open a KEEPER's info/training panel in-game. Watching %ds..." % secs, flush=True)
    seen: dict = {}
    fires = 0
    t0 = time.time()
    try:
        end = t0 + secs
        while time.time() < end:
            fires = poll_ring(s, scratch, seen)
            time.sleep(0.2)
    finally:
        hm.restore_all()
        dur = max(time.time() - t0, 0.01)
        print("RESTORED. fires=%d (%.0f/sec) distinct keeper objs=%d" % (fires, fires / dur, len(seen)), flush=True)
    for kp, n in sorted(seen.items(), key=lambda kv: -kv[1])[:4]:
        _dump_keeper(s, base, kp, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
