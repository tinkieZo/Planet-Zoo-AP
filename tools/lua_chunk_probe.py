"""lua_chunk_probe - find serialized Lua 5.3 chunks in the live VM heap (read-only).

The Cobra VM keeps each loaded script module's SERIALIZED chunk (custom "\\x1bLua" 5.3 header,
see tools.luaparse) in writable heap memory - proven by TerrainGate/main.2, whose code array is
the loaded-verbatim serialized form. This probe sweeps all writable regions for the chunk
signature, prints each chunk's SOURCE NAME (the main proto's source string, sitting right after
the 0x1C header + 1 upvalue-count byte), and - with a filter - parses matching chunks to get
their true byte length and dumps them to .lua.bin files for tools.luaparse.

A module's chunk is only present after the game has loaded it (main.2 appears only once the
terrain menu was opened) - so open the UI you are hunting (e.g. an exhibit's animal panel)
before running.

    python -m tools.lua_chunk_probe                    # list every chunk's source name + address
    python -m tools.lua_chunk_probe exhibit            # ...and dump chunks whose source matches
    python -m tools.lua_chunk_probe exhibit -o DIR     # dump directory (default: tools/_luadump)

Read-only against the game; dumps go to local files only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner    # noqa: E402
from pz_ap_client.memory.terrain import TerrainGate      # noqa: E402
from tools.luaparse import R, read_function, HEADER_LEN  # noqa: E402

SIG = b"\x1bLua\x53\x02"        # sig + version 0x53 + format 02 (custom Frontier header)
NAME_WINDOW = 0x800             # enough to cover header + main proto's source string
PARSE_CAP = 0x400000            # max serialized chunk size we attempt to parse (4 MB)


def _source_of(buf: bytes) -> "str | None":
    """The main proto's source string (module name), or None if unreadable."""
    try:
        r = R(buf)
        r.p = HEADER_LEN
        r.u8()                  # main-proto upvalue count
        return r.string()
    except Exception:
        return None


def _parse_len(buf: bytes) -> "tuple[int, int] | None":
    """Fully parse a chunk; return (consumed_bytes, n_protos) or None on parse failure."""
    try:
        r = R(buf)
        r.p = HEADER_LEN
        r.u8()
        protos: list = []
        read_function(r, protos, "main")
        return r.p, len(protos)
    except Exception:
        return None


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    args = [a for a in sys.argv[1:]]
    outdir = Path(__file__).resolve().parent / "_luadump"
    if "-o" in args:
        i = args.index("-o")
        outdir = Path(args[i + 1])
        del args[i:i + 2]
    kws = [a.lower() for a in args]

    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached (is PlanetZoo.exe running?)")
        return 1
    regions = TerrainGate(s)._regions()
    print("scanning %d writable regions for %r chunks..." % (len(regions), SIG))

    seen: dict = {}       # source -> [addrs]
    dumped = 0
    for rb, rs in regions:
        try:
            data = s.read_bytes(rb, rs)
        except Exception:
            continue
        i = data.find(SIG)
        while i != -1:
            addr = rb + i
            src = _source_of(data[i:i + NAME_WINDOW]) or "<unreadable>"
            seen.setdefault(src, []).append(addr)
            if kws and any(k in src.lower() for k in kws):
                res = _parse_len(data[i:min(i + PARSE_CAP, rs)])
                if res is None:
                    print("   %-52s @0x%X  PARSE FAILED (truncated by region end?)" % (src, addr))
                else:
                    consumed, nfn = res
                    outdir.mkdir(parents=True, exist_ok=True)
                    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in src)[:80]
                    out = outdir / ("%s_%X.lua.bin" % (safe, addr))
                    out.write_bytes(data[i:i + consumed])
                    dumped += 1
                    print("   %-52s @0x%X  %d bytes, %d fns -> %s" % (src, addr, consumed, nfn, out))
            i = data.find(SIG, i + 1)

    print("\n%d distinct chunk sources (%d instances):" % (
        len(seen), sum(len(v) for v in seen.values())))
    for src in sorted(seen):
        print("   %-64s x%d  @%s" % (src, len(seen[src]),
                                     ",".join("0x%X" % a for a in seen[src][:4])))
    if kws:
        print("\ndumped %d chunk(s) matching %s to %s" % (dumped, kws, outdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
