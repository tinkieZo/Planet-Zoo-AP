"""selfcheck - preflight health report for every patch-sensitive address the client depends on.

Run it against the live game (in a loaded zoo) to confirm the whole inventory resolves, or after a
Frontier patch to see EXACTLY which sites broke (so re-RE is targeted). Read-only; installs nothing.

    python -m tools.selfcheck
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.anchors import AnchorTable  # noqa: E402
from pz_ap_client.memory import signatures as sig  # noqa: E402

_MARK = {"ok": "OK  ", "relocated": "MOVED", "leaked": "LEAK", "broken": "FAIL",
         "garbage": "FAIL", "unresolved": "MISS"}


def main() -> int:
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached (is Planet Zoo running?)"); return 1
    try:
        at = AnchorTable.load()
    except Exception as e:
        print("anchor table load failed: %s - checking code sites only" % e); at = None
    results = sig.run_selfcheck(s, at)
    print("=" * 74)
    print("  PZ-AP client self-check  (module base 0x%X)" % s.module_base)
    print("=" * 74)
    cur = None
    for r in results:
        if r.category != cur:
            cur = r.category
            print("-- %s --" % cur)
        print("  [%s] %-18s %s" % (_MARK.get(r.status, r.status), r.name, r.detail))
    ok = sum(1 for r in results if r.status in ("ok", "relocated"))
    bad = [r for r in results if r.status not in ("ok", "relocated")]
    print("=" * 74)
    print("  %d/%d green%s" % (ok, len(results), ("" if not bad else "  -  attention: " + ", ".join(r.name for r in bad))))
    print("=" * 74)
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
