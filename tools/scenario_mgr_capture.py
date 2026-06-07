"""scenario_mgr_capture — capture the scenario-manager object that the terrain-greying predicate reads.

FUN_14041e020 is the reflection wrapper for property #0x14 (one of the Is*Disabled predicates). It
unwraps self (the scenario manager) into rdi, then calls the property-getter
`vtable[0](self+8, 0x14)`. We hook right after rdi is set (0x14041E062, ORIG `mov edx,0xffffffff`) and
ring-capture rdi = the scenario-manager object. Read-only (runs the original, jmps back). With the object
pinned + Goodwin House's known state (terrain disabled=true), we can dump its fields / disasm the getter
to find the per-tool source byte, then write it + re-enter terrain mode to confirm.

    python -m tools.scenario_mgr_capture [seconds=90]
Re-enter terrain edit mode (switch tool away + back) within the window.
"""
from __future__ import annotations
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools._capture import install_ring_capture, poll_ring  # noqa: E402

RVA = 0x41E062
ORIG = bytes.fromhex("baffffffff")  # mov edx, 0xffffffff (5 bytes)


def _report_obj(s, v, n) -> None:
    """Print a captured object pointer and walk its property-getter chain (self+8 -> vtable -> vtable[0])."""
    print("  obj=0x%X  (x%d)" % (v, n), flush=True)
    try:
        sub = struct.unpack("<Q", s.read_bytes(v + 8, 8))[0]      # self+8 sub-object
        vt = struct.unpack("<Q", s.read_bytes(sub, 8))[0]          # its vtable
        getter = struct.unpack("<Q", s.read_bytes(vt, 8))[0]       # vtable[0] = property getter
        print("    self+8=0x%X  vtable=0x%X  getter(vtable[0])=0x%X" % (sub, vt, getter), flush=True)
    except Exception as e:
        print("    (deref failed: %s)" % e, flush=True)


def main() -> int:
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    inst = install_ring_capture("smc", RVA, ORIG, 0x41E067, "rdi")
    if inst is None:
        return 1
    s, hm, scratch = inst
    print("INSTALLED scenario-mgr capture @0x%X" % (s.module_base + RVA), flush=True)
    print(">>> Re-enter terrain edit mode (switch tool away + back). Watching %ds..." % secs, flush=True)
    seen: dict = {}
    cnt = 0
    try:
        end = time.time() + secs
        while time.time() < end:
            poll_ring(s, scratch, seen)
            time.sleep(0.2)
        cnt = struct.unpack("<I", s.read_bytes(scratch, 4))[0]
    finally:
        hm.restore_all()
        print("RESTORED. fires=%d distinct objs=%d" % (cnt, len(seen)), flush=True)
    if not seen:
        print("no capture (wrapper didn't fire — re-enter terrain mode during the window)."); return 0
    print("=== captured scenario-manager object pointer(s) ===", flush=True)
    for v, n in sorted(seen.items(), key=lambda kv: -kv[1]):
        _report_obj(s, v, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
