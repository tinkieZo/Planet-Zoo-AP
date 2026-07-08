"""exhibit_storage_probe - DIAGNOSTIC (read-only): why a STORAGE exhibit release isn't attributed.

The client's PRIMARY exhibit-release attribution diffs the exhibit manager's +0x2A0 owned-animal ID
SET (claimed to cover placed AND stored animals); the FALLBACK diffs the placed-only +0x318 census.
A storage release that logs "wasn't attributed" means NEITHER saw the released animal. This probe
dumps all three live structures every second so we can tell which assumption is wrong:

  * id-set (+0x2A0): does read_exhibit_ids() return a SET or None? If None, +0x2A0 isn't the id-set
    layout we assume (it's a scalar on other managers) - the whole primary path is dead.
  * census  (+0x318): placed-only; a stored animal should NOT be here (expected).
  * def-names: does each id map to a species token -> data.json key? An id present but unmapped means
    attribution can't name it even when the diff sees it leave.

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
    """Return (ids, one-line 'mgr | id_set | census' summary). ids is None if +0x2A0 didn't validate."""
    ids = res.read_exhibit_ids(mgr)          # +0x2A0 (placed + stored, per assumption)
    census = res.read_exhibit_census(mgr)    # +0x318 (placed only)
    parts = ["mgr=0x%X" % mgr]
    # id-set: the crux. None => structure/offset assumption is WRONG (primary path dead).
    if ids is None:
        parts.append("id_set(+0x2A0)=None  <-- read_exhibit_ids REJECTED the structure (not a hash-set here)")
    else:
        parts.append("id_set(+0x2A0)=%d %s" % (len(ids), _fmt_ids(ids)))
    # census (placed-only): expected NOT to contain a stored animal.
    if census is None:
        parts.append("census(+0x318)=None")
    else:
        mapped = {("0x%X" % h): (h2k.get(h) or "UNMAPPED") for h in census}
        parts.append("census(+0x318)=%d %s" % (len(census), mapped))
    return ids, " | ".join(parts)


def _print_species(res, rr, mgr, ids):
    """Per-id species resolution via the def map (what _cache_exhibit_species does)."""
    names = res.read_exhibit_def_names(mgr) or {}
    for aid in sorted(ids):
        cand = names.get(aid, ())
        key = next((k for nm in cand if (k := rr.species_key_for_name(nm))), None)
        if key:
            tag = key
        elif cand:
            tag = "UNMAPPED names=%s" % list(cand)
        else:
            tag = "NO def-name entry"
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
    ids, summary = _roster_summary(res, mgr, _handle_key_map(rr))
    print("  " + summary)
    if ids:
        _print_species(res, rr, mgr, ids)
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
