"""release_cache_probe - faithful end-to-end validation of cr_<species> detection, standalone.

Drives the REAL client detectors (BirthDetector + ReleaseDetector + ResearchReader, with token_to_key
built from data.json) and the race-free insert-cache, with NO Archipelago server and NO conservation
gate (the release gate is opened here). It mirrors exactly what MemoryTriggerSource does:

  * each tick, drain births -> populates births.handle_species {handle -> species_key}
    (this is the race-free record, filled when an animal ENTERS the zoo), and
  * on a new release, look the released handle up in that cache -> the cr_<species> key.

    python -m tools.release_cache_probe [seconds=180]

In the loaded AP zoo: BUY or MOVE an animal into a habitat (-> it gets cached, printed live), then
RELEASE it. Expect "RELEASE handle 0x.. -> CACHE HIT: <species_key>". That is the exact attribution
the client will report as a conservation_release check.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client import data as gamedata                       # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner           # noqa: E402
from pz_ap_client.memory.registry import RegistryResolver       # noqa: E402
from pz_ap_client.memory.research import ResearchReader         # noqa: E402
from pz_ap_client.memory.births import BirthDetector            # noqa: E402
from pz_ap_client.memory.releases import ReleaseDetector        # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    gd = gamedata.load()
    token_to_key = {s.engine_token: s.key for s in gd.species if s.engine_token}
    print("data.json: %d species, %d with engine_token" % (len(gd.species), len(token_to_key)))

    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached")
        return 1
    research = ResearchReader(s, registry=RegistryResolver(s), token_to_key=token_to_key)
    births = BirthDetector(s, research=research)
    releases = ReleaseDetector(s)
    if not releases.ensure_installed():
        print("FAIL: release detector could not install")
        return 1
    releases.set_locked(False)   # open the conservation gate so releases proceed (no AP item needed)
    births.ensure_installed()
    print("detectors installed; gate OPEN; species capture=%s" % (releases.sp_scratch is not None))
    print("BUY or MOVE an animal into a habitat (it gets cached), then RELEASE it (%ds)..." % secs)

    last_count = releases.count()
    seen_cache = set()
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            born, acq = births.poll_events()      # drains inserts -> fills births.handle_species
            for h, k in list(births.handle_species.items()):
                if h not in seen_cache:
                    seen_cache.add(h)
                    print("  cached: handle 0x%X -> %s" % (h, k))
            cnt = releases.count()
            if cnt > last_count:
                last_count = cnt
                handle = releases.last_released_handle()
                if not handle:
                    print("RELEASE #%d but no handle captured (species hook not firing?)" % cnt)
                else:
                    key = births.handle_species.get(handle)
                    if key:
                        print("RELEASE #%d handle 0x%X -> CACHE HIT: %s   <== cr_%s would fire"
                              % (cnt, handle, key, key))
                    else:
                        print("RELEASE #%d handle 0x%X -> not in cache (was it inserted while attached? "
                              "moving it into a habitat first populates the cache)" % (cnt, handle))
            time.sleep(0.1)
    finally:
        releases.shutdown()
        births.shutdown()
        print("detectors restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
