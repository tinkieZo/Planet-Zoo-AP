"""research_gate_probe — validate the research-state gate mechanism SAFELY (no code hook).

Three things to learn before wiring ResearchGate:
  1. Does forcing a record's status byte to 0 (NotStarted) make it un-researchable in the UI?
  2. Does the game OVERWRITE our 0 back to 1 (so the gate must re-force each poll tick)?
  3. Is there a separate PROGRESS field in the 0x58 record that persists (so status alone
     isn't enough)? -> we diff the full record bytes over time while you interact.

Usage:
    python -m tools.research_gate_probe list [cat]          # list occupied records of category
    python -m tools.research_gate_probe watch <item_hex> [secs]   # dump record bytes over time
    python -m tools.research_gate_probe force <item_hex> <status> [secs]
        # set status, then read it back every 2s for <secs> (does the game restore it?),
        # then RESTORE the original status. Watch the in-game research panel meanwhile.
cat: 7 = animal/welfare (research_centre), 3 = mechanic (workshop). Default 7.
"""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.research import ResearchReader, REC_STRIDE, REC_STATUS  # noqa: E402

STATUS = {0: "NotStarted", 1: "Researchable", 2: "Researching", 3: "Researched(uncollected)", 4: "Completed"}


def _find(reader, item_hex):
    item = int(item_hex, 16)
    for it, level, status, cat, status_addr in reader.scan_records():
        if it == item:
            rec_addr = status_addr - REC_STATUS
            return it, level, status, cat, rec_addr
    return None


def _cmd_list(s, r) -> int:
    cat = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    recs = [x for x in r.scan_records() if x[3] == cat]
    print("category %d: %d occupied records (showing status>=1 first)" % (cat, len(recs)))
    recs.sort(key=lambda x: (-x[2], x[0]))
    for it, level, status, _, _ in recs[:40]:
        print("  item 0x%-5X lvl %-3d status %d (%s)" % (it, level, status, STATUS.get(status, "?")))
    return 0


def _cmd_watch(s, r) -> int:
    found = _find(r, sys.argv[2])
    secs = int(sys.argv[3]) if len(sys.argv) > 3 else 30
    if not found:
        print("item not found"); return 1
    it, _, _, cat, rec_addr = found
    print("watching item 0x%X (cat %d) record @0x%X for %ds — change the research in-game"
          % (it, cat, rec_addr, secs))
    prev = None
    end = time.time() + secs
    while time.time() < end:
        rec = s.read_bytes(rec_addr, REC_STRIDE)
        if rec != prev:
            print("  status=%d  bytes=%s" % (rec[REC_STATUS], rec.hex()))
            prev = rec
        time.sleep(1.0)
    return 0


def _cmd_force(s, r) -> int:
    found = _find(r, sys.argv[2])
    newval = int(sys.argv[3])
    secs = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    if not found:
        print("item not found"); return 1
    it, _, orig, cat, rec_addr = found
    status_addr = rec_addr + REC_STATUS
    print("item 0x%X (cat %d): status %d (%s) -> forcing %d. Watch the in-game panel."
          % (it, cat, orig, STATUS.get(orig, "?"), newval))
    s.write_bytes(status_addr, bytes([newval]))
    end = time.time() + secs
    try:
        while time.time() < end:
            cur = s.read_bytes(status_addr, 1)[0]
            print("  read-back status = %d (%s)%s" % (cur, STATUS.get(cur, "?"),
                  "  <-- GAME RESTORED IT" if cur != newval else ""))
            time.sleep(2.0)
    finally:
        s.write_bytes(status_addr, bytes([orig]))
        print("restored original status %d." % orig)
    return 0


_COMMANDS = {"list": _cmd_list, "watch": _cmd_watch, "force": _cmd_force}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    handler = _COMMANDS.get(cmd)
    if handler is None:
        print("unknown command %r (use: %s)" % (cmd, ", ".join(_COMMANDS))); return 1
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    return handler(s, ResearchReader(s))


if __name__ == "__main__":
    raise SystemExit(main())
