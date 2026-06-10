"""Post-bounce Lua error hunter.

When the AP scenario load bounces back to the main menu, the Lua error string /
traceback that killed the load was formatted into the heap and usually survives
(the process is still alive at the menu). This scans ALL committed readable
memory of PlanetZoo.exe for error-shaped ASCII and dumps context around hits,
so we get the EXACT error instead of bisecting with boot tests.

Run RIGHT AFTER a bounce, while the game sits at the main menu:
    py -3.11 tools/_lua_error_scan.py
    py -3.11 tools/_lua_error_scan.py --needle "custom text"

Output: tools/_lua_error_hits.txt (context hexdump+ascii per hit, deduped).

Noise filter: the engine image contains static format strings ("attempt to
index a %s value"); a FORMATTED instance has actual identifiers spliced in
(e.g. "objectivesettings.scenario_ap_objectives:31: attempt to index ..."),
so hits mentioning our module names or with concrete quoted names are the
signal. We dump generously and judge by eye.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools.memscan import iter_regions  # noqa: E402

NEEDLES = [
    b"attempt to ",          # lua runtime errors (index/call/arith/compare nil...)
    b"stack traceback",      # luaL_traceback output
    b"bad argument",         # luaL_argerror
    b"scenario_ap",          # our module names appearing inside error text
    b"scenarioap",           # our scenario code in dynamic strings
    b"Using save code",      # the code-mismatch assert text (proves that path ran)
    b"APERR:",               # our script's pcall-captured stage errors
    b"APDBG:",               # our script's readback markers (e.g. canhire=...)
]
CTX_BEFORE = 96
CTX_AFTER = 320
OUT = Path(__file__).resolve().parent / "_lua_error_hits.txt"
CHUNK = 4 << 20


def printable(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else ("\\n" if c == 10 else ".") for c in b)


def _scan_buffer(buf: bytes, buf_addr: int, needles, seen_ctx, hits) -> None:
    """Collect (addr, needle, context) for every needle occurrence in one chunk, deduped by context."""
    for needle in needles:
        start = 0
        while True:
            i = buf.find(needle, start)
            if i < 0:
                break
            lo = max(0, i - CTX_BEFORE)
            ctx = buf[lo:i + len(needle) + CTX_AFTER]
            key = ctx[:160]
            if key not in seen_ctx:
                seen_ctx.add(key)
                hits.append((buf_addr + i, needle, ctx))
            start = i + 1


def _scan_regions(sc, regions, needles) -> list:
    hits: list[tuple[int, bytes, bytes]] = []  # (addr, needle, context)
    seen_ctx: set[bytes] = set()
    for base, size in regions:
        off = 0
        while off < size:
            n = min(CHUNK, size - off)
            try:
                buf = sc.pm.read_bytes(base + off, n)
            except Exception:
                off += n
                continue
            _scan_buffer(buf, base + off, needles, seen_ctx, hits)
            # overlap chunk boundaries so needles spanning them aren't missed
            off += n - 64 if n == CHUNK else n
    return hits


def _report(hits) -> None:
    with OUT.open("w", encoding="utf-8", errors="replace") as f:
        for addr, needle, ctx in hits:
            f.write(f"@0x{addr:x}  [{needle.decode(errors='replace')}]\n")
            f.write(printable(ctx) + "\n" + "-" * 100 + "\n")
    print(f"{len(hits)} unique hits -> {OUT}")
    # quick triage to stdout: hits that mention our modules AND error verbiage
    strong = [h for h in hits if (b"scenario_ap" in h[2] or b"scenarioap" in h[2])
              and (b"attempt to" in h[2] or b"traceback" in h[2] or b"bad argument" in h[2])]
    if strong:
        print(f"\n*** {len(strong)} STRONG hits (our modules + error text): ***")
        for addr, _, ctx in strong[:10]:
            print(f"@0x{addr:x}: {printable(ctx[:280])}")
    else:
        print("no combined module+error hits; read the full dump file.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--needle", action="append", default=[], help="extra ASCII needle(s)")
    ap.add_argument("--process", default="PlanetZoo.exe")
    args = ap.parse_args()
    needles = NEEDLES + [n.encode() for n in args.needle]

    sc = MemoryScanner(args.process)
    if not sc.attach():
        print(f"ERROR: {args.process} not running - run this right after the bounce, "
              "while the game is at the main menu.")
        sys.exit(1)
    regions = iter_regions(sc.pm.process_handle, writable_only=False)
    print(f"scanning {len(regions)} regions for {len(needles)} needles...")
    hits = _scan_regions(sc, regions, needles)
    hits.sort(key=lambda h: h[0])
    _report(hits)


if __name__ == "__main__":
    main()
