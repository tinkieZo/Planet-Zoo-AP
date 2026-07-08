"""enrichment_unlock_probe - DIAGNOSTIC (read-only): why exhibit-enrichment decoupling doesn't gate.

reconcile_progressive_levels("exhibit_enrichment", N) is supposed to set each <Species>EnrichmentL<k>
content's unlocked byte = (k <= N) for EVERY exhibit species. Symptom (live 2026-07-08): with N=1 a
FULLY-RESEARCHED animal shows ALL levels unlocked (L2/L3 not re-locked) and an UN-researched animal
shows none - i.e. the gate isn't acting. Two candidate causes this probe distinguishes:

  A) NAMING: the live intern tokens for exhibit enrichment don't match r'enrichmentl(\\d+)', so the
     family level-map is EMPTY and reconcile no-ops (returns True without writing). We dump every
     intern name containing 'enrich' with its id + whether the regex matches.
  B) MATERIALIZATION: an un-researched species' enrichment records don't exist in the rs+0x148 unlock
     map until its research tree loads, so they CAN'T be pre-unlocked by a byte flip. We dump every
     type-1 (enrichment) unlock-map record: cid, reverse-name, unlocked flag, regex match.

Then we print exactly what reconcile_progressive_levels would compute (level map + want_unlocked for
N=1) so the mismatch is explicit.

    python -m tools.enrichment_unlock_probe

Run in the loaded AP zoo, ideally with one exhibit animal researched and one not.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner              # noqa: E402
from pz_ap_client.memory.research import ResearchReader            # noqa: E402
from pz_ap_client.memory.registry import RegistryResolver          # noqa: E402
from pz_ap_client.memory.rewards import (                          # noqa: E402
    InternRegistry, UnlockMap, FAMILY_LEVEL_RE, FAMILY_TYPE,
)

ENRICH_RE = FAMILY_LEVEL_RE["exhibit_enrichment"]     # r'enrichmentl(\d+)'
TYPE_ENRICH = FAMILY_TYPE["exhibit_enrichment"]       # 1


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1

    rr = ResearchReader(s, registry=RegistryResolver(s))
    rs = rr._research_system()
    print("research system: %s" % ("0x%X" % rs if rs else "None (no zoo / not resolvable)"))

    try:
        reg = InternRegistry(s)
    except Exception as e:
        print("FAIL: intern registry: %s" % e)
        return 1
    index = reg.build_index()   # {lowercased name: id}
    print("intern index: %d names" % len(index))

    # (A) NAMING - every intern token containing 'enrich', with regex verdict.
    enrich_names = sorted(n for n in index if "enrich" in n)
    matched = [(n, ENRICH_RE.search(n)) for n in enrich_names]
    n_match = sum(1 for _, mo in matched if mo)
    print("\n=== (A) intern names containing 'enrich': %d total, %d match r'%s' ==="
          % (len(enrich_names), n_match, ENRICH_RE.pattern))
    for n, mo in matched[:80]:
        print("   %-48s id=0x%-6X  regex=%s" % (n, index[n], ("L%s" % mo.group(1)) if mo else "NO MATCH"))
    if len(matched) > 80:
        print("   ... (%d more)" % (len(matched) - 80))

    # The family level-map reconcile would build (cid -> level), and want_unlocked for N=1.
    levels = {cid: int(mo.group(1)) for name, cid in index.items() if (mo := ENRICH_RE.search(name))}
    print("\nfamily level-map size (what reconcile sees): %d" % len(levels))
    if not levels:
        print("   >>> EMPTY -> reconcile_progressive_levels returns True WITHOUT writing = cause (A).")

    # (B) MATERIALIZATION - type-1 records in the live unlock map.
    if not rs:
        print("\n(skip B: no research system)")
        return 0
    try:
        m = UnlockMap(s, rs)
    except Exception as e:
        print("\nFAIL: unlock map: %s" % e)
        return 1
    print("\n=== (B) rs+0x148 unlock records of type %d (enrichment) ===" % TYPE_ENRICH)
    n_rec = n_unlocked = n_regex = 0
    for rec, cid, typ, flag in m.iter_records():
        if typ != TYPE_ENRICH:
            continue
        n_rec += 1
        n_unlocked += 1 if flag else 0
        name = reg._name(cid) or "<unnamed>"
        mo = ENRICH_RE.search(name.lower())
        if mo:
            n_regex += 1
        if n_rec <= 80:
            print("   cid=0x%-6X unlocked=%d  %-44s regex=%s"
                  % (cid, flag, name, ("L%s" % mo.group(1)) if mo else "NO MATCH"))
    print("   type-1 records: %d total, %d unlocked, %d match the enrichment regex" % (n_rec, n_unlocked, n_regex))
    print("\nInterpretation:")
    print("  - many type-1 records but few/none regex-match  -> cause (A) naming: fix FAMILY_LEVEL_RE.")
    print("  - regex matches only the RESEARCHED animal's records -> cause (B): un-researched species'")
    print("    records don't exist yet; a byte flip can't pre-unlock them (need a different mechanism).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
