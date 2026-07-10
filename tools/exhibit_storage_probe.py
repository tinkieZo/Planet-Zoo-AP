"""exhibit_storage_probe - DIAGNOSTIC (read-only): why a STORAGE exhibit release isn't attributed.

The client attributes a storage release via the def-map HOLDER's structures (holder = primary
*(mgr+0xF8), fallback *(*(mgr+0x2F0)+0xD8)): the +0x2A0 owned-id set diff and the *(H+0x358)+0x108
def map (species HANDLE @entry+0x30 through the research map, string-token match as fallback); the
placed-only +0x318 census is the last resort. A storage release that logs "wasn't attributed" means
none of them saw/named the released animal. This probe dumps all the live structures every second
so we can tell which link broke:

  * holder: None means BOTH chains are dead - the id-set/def-map paths can't run at all.
  * id-set (holder+0x2A0): does read_exhibit_ids() return a SET or None? None = wrong layout/holder.
  * census  (mgr+0x318): placed-only; a stored animal should NOT be here (expected).
  * def map: does each id resolve to a species (handle first, name-token fallback)? An id present
    but UNMAPPED means attribution can't name it even when the diff sees it leave.

Run in the loaded AP zoo with ONE exhibit animal sitting IN STORAGE (not placed). Watch a few ticks,
then RELEASE it from storage. The diff lines show whether its id was ever in +0x2A0 and whether it
left the set on release.

    python -m tools.exhibit_storage_probe [seconds=120]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client import data as gamedata                      # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner          # noqa: E402
from pz_ap_client.memory.animals import AnimalResolver         # noqa: E402
from pz_ap_client.memory.research import ResearchReader        # noqa: E402
from pz_ap_client.memory.registry import RegistryResolver      # noqa: E402


def _fmt_ids(ids):
    return "{" + ", ".join("0x%X" % i for i in sorted(ids)) + "}" if ids else "{}"


def _load_token_map():
    """The client's species mapping (engine_token -> key), so we reproduce attribution faithfully."""
    try:
        gd = gamedata.load()
        return {sp.engine_token: sp.key for sp in gd.species if sp.engine_token}
    except Exception as e:
        print("WARN: data.json not loaded (%s) - species keys shown as None" % e)
        return {}


def _handle_key_map(rr):
    try:
        return rr.handle_key_map() or {}
    except Exception:
        return {}


def _roster_summary(res, mgr, h2k):
    """Return (holder, ids, one-line summary). Structures live off the HOLDER (primary *(mgr+0xF8),
    fallback *(*(mgr+0x2F0)+0xD8)) - the exact chain production resolve_exhibit_defmap_holder walks."""
    holder = res.resolve_exhibit_defmap_holder(mgr)
    ids = res.read_exhibit_ids(holder) if holder else None
    census = res.read_exhibit_census(mgr)    # +0x318 (placed only)
    parts = ["mgr=0x%X" % mgr, ("holder=0x%X" % holder) if holder else
             "holder=None  <-- BOTH holder chains dead (storage paths rely on the capture)"]
    if ids is None:
        parts.append("id_set(+0x2A0)=None  <-- read_exhibit_ids REJECTED the structure")
    else:
        parts.append("id_set(+0x2A0)=%d %s" % (len(ids), _fmt_ids(ids)))
    # census (placed-only): expected NOT to contain a stored animal.
    if census is None:
        parts.append("census(+0x318)=None")
    else:
        mapped = {("0x%X" % h): (h2k.get(h) or "UNMAPPED") for h in census}
        parts.append("census(+0x318)=%d %s" % (len(census), mapped))
    return holder, ids, " | ".join(parts)


def _print_species(res, rr, holder, ids, h2k):
    """Per-id species resolution via the def map (what _cache_exhibit_species does): species HANDLE
    @entry+0x30 through the research map first, string-token match as fallback."""
    defs = (res.read_exhibit_defs(holder) or {}) if holder else {}
    for aid in sorted(ids):
        handle, cand = defs.get(aid, (0, ()))
        key = (h2k.get(handle) if handle else None) or \
            next((k for nm in cand if (k := rr.species_key_for_name(nm))), None)
        if key:
            tag = "%s (handle 0x%X)" % (key, handle)
        elif handle or cand:
            tag = "UNMAPPED handle=0x%X names=%s" % (handle, list(cand))
        else:
            tag = "NO def entry"
        print("      id 0x%X -> %s" % (aid, tag))


def _print_diff(prev_ids, ids):
    """Show the set delta vs last tick - the release transition shows here."""
    left, joined = prev_ids - ids, ids - prev_ids
    if left:
        print("      >>> IDs LEFT the set (released/sold/died): %s" % _fmt_ids(left))
    if joined:
        print("      >>> IDs JOINED the set (placed/bought/born): %s" % _fmt_ids(joined))


def _tick(res, rr, prev_ids):
    """One read-only snapshot; returns the id set to carry to the next tick (prev_ids if unresolved)."""
    mgr = res.resolve_exhibit_manager()
    if not mgr:
        print("  [no exhibit manager - zoo not loaded?]")
        return prev_ids
    h2k = _handle_key_map(rr)
    holder, ids, summary = _roster_summary(res, mgr, h2k)
    print("  " + summary)
    if ids:
        _print_species(res, rr, holder, ids, h2k)
    if prev_ids is not None and ids is not None:
        _print_diff(prev_ids, ids)
    return ids if ids is not None else prev_ids


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1
    rr = ResearchReader(s, registry=RegistryResolver(s), token_to_key=_load_token_map())
    res = AnimalResolver(s)
    print("exhibit_storage_probe: put ONE exhibit animal in STORAGE, watch, then RELEASE it (%ds)..." % secs)
    prev_ids = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        prev_ids = _tick(res, rr, prev_ids)
        time.sleep(1.0)
    print("probe done (read-only; nothing patched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
