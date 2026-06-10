"""apsession_probe - live AP-session detection state (and optionally plant the park-name marker).

Reads, from the running game: every park-info instance (vtable scan), each instance's native park
name (+0x1E8 refcounted string), the exchange-manager mode byte, and the resulting
ApSessionDetector verdict.

    python -m tools.apsession_probe                 # report only
    python -m tools.apsession_probe --plant-marker  # write the ARCHIPELAGO ZOO marker into the live
                                                    # park-info (same field Scenario_AP_Script's
                                                    # SetParkName fills once the v18 ovl ships); the
                                                    # name then persists into the next save.

--plant-marker allocates a refcounted string in the game process (refcount parked high so the
engine's decref-on-swap/teardown can never free OUR buffer with ITS allocator) and fills +0x1E8 only
where it is currently NULL. Refuses to overwrite an existing name.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory import signatures as sig  # noqa: E402
from pz_ap_client.memory.session import AP_PARK_NAME, ApSessionDetector, read_refcounted_string  # noqa: E402
from pz_ap_client.memory.market import ScheduleSpawner  # noqa: E402


def plant_marker(s, instances) -> None:
    import pymem.memory  # local import; only the write path needs it
    name = AP_PARK_NAME.encode()
    targets = [a for a in instances if not s.read_qword(a + sig.PARKINFO_NAME_OFF)]
    if not targets:
        print("no NULL name slots - marker (or a real name) already present; not overwriting")
        return
    buf = pymem.memory.allocate_memory(s.pm.process_handle, 0x40)
    blob = struct.pack("<qqi", len(name), len(name), 64) + name + b"\x00"
    s.pm.write_bytes(buf, blob, len(blob))
    for a in targets:
        s.pm.write_bytes(a + sig.PARKINFO_NAME_OFF, struct.pack("<Q", buf), 8)
        print("planted marker @park-info 0x%X +0x%X -> str 0x%X" % (a, sig.PARKINFO_NAME_OFF, buf))
    if len(targets) > 1:
        print("NOTE: marker planted on EVERY null-name instance incl. the static template (no robust "
              "live-vs-template discriminator). A marked template can false-positive a DIFFERENT park "
              "loaded later in this game session - don't switch parks before restarting the game.")


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    target = s.module_base + sig.PARKINFO_VTABLE_RVA
    instances = s.scan_heap_for_qword(target, max_hits=64)
    print("park-info instances: %s" % (", ".join("0x%X" % a for a in instances) or "none"))
    for a in instances:
        ptr = s.read_qword(a + sig.PARKINFO_NAME_OFF)
        print("  0x%X  name ptr=0x%X  name=%r  years(+0x1C8)=%d"
              % (a, ptr, read_refcounted_string(s, ptr),
                 struct.unpack("<q", s.read_bytes(a + sig.PARKINFO_PERIODS_OFF, 8))[0]))

    if "--plant-marker" in sys.argv:
        plant_marker(s, instances)

    spawner = ScheduleSpawner(s)
    det = ApSessionDetector(s, mode_check=spawner.scenario_mode)
    print("scenario market mode: %s" % spawner.scenario_mode())
    print("AP session detected:  %s" % det.is_ap_session())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
