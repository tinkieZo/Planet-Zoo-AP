"""vscan - value scanner for small, COMMON integers (population counts, etc.)
that the per-address ``scanstep`` loop can't handle.

Where scanstep narrows by re-reading each candidate one syscall at a time
(fine for tens of thousands of hits), a population count like 2 matches ~17M
addresses, so re-reading is hopeless. vscan instead does a full **vectorised**
numpy sweep for an exact value each round and **intersects candidate addresses
across rounds**: the field you want is the address that held ``old`` before your
in-game change and ``new`` after.

    python -m tools.vscan new i32 2      # sweep: all 4-aligned i32 == 2
    #   (buy a zebra in-game: population 2 -> 3)
    python -m tools.vscan next 3         # sweep ==3, intersect with previous
    #   (sell one: 3 -> 2) ...
    python -m tools.vscan next 2
    python -m tools.vscan list

State (candidate address array + type) persists to ``tools/.vscan.npy`` /
``.vscan.json`` so a scan spans the pauses while you act in-game. Reuses
``iter_regions`` from memscan; requires numpy.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools.memscan import iter_regions  # noqa: E402

_DTYPE = {"i32": "<i4", "u32": "<u4", "i64": "<i8", "float": "<f4", "double": "<f8"}
_PACK = {"i32": "<i", "u32": "<I", "i64": "<q", "float": "<f", "double": "<d"}

NPY = Path(__file__).resolve().parent / ".vscan.npy"
META = Path(__file__).resolve().parent / ".vscan.json"


def _sweep(scanner: MemoryScanner, type_: str, value) -> np.ndarray:
    """Return a sorted uint64 array of addresses whose typed value == ``value``."""
    dt = np.dtype(_DTYPE[type_])
    isize = dt.itemsize
    is_float = type_ in ("float", "double")
    parts = []
    for base, size in iter_regions(scanner.pm.process_handle, writable_only=True):
        try:
            buf = scanner.read_bytes(base, size)
        except Exception:
            continue
        usable = len(buf) - (len(buf) % isize)
        if usable < isize:
            continue
        arr = np.frombuffer(buf, dtype=dt, count=usable // isize)
        if is_float:
            idx = np.nonzero(np.abs(arr - value) <= max(1e-4, abs(value) * 1e-6))[0]
        else:
            idx = np.nonzero(arr == value)[0]
        if idx.size:
            parts.append(base + idx.astype("u8") * isize)
    if not parts:
        return np.empty(0, dtype="u8")
    return np.unique(np.concatenate(parts))


def _load():
    if NPY.exists() and META.exists():
        return np.load(NPY), json.loads(META.read_text())
    return np.empty(0, dtype="u8"), {}


def _save(addrs: np.ndarray, type_: str) -> None:
    np.save(NPY, addrs)
    META.write_text(json.dumps({"type": type_}))


def _coerce(type_: str, tok: str):
    return float(tok) if type_ in ("float", "double") else int(tok, 0)


def _cmd_new(s: MemoryScanner, argv: list) -> None:
    type_, value = argv[1], _coerce(argv[1], argv[2])
    addrs = _sweep(s, type_, value)
    _save(addrs, type_)
    print(f"{addrs.size} candidates (type {type_} == {value})")


def _cmd_next(s: MemoryScanner, argv: list) -> None:
    addrs, meta = _load()
    type_ = meta.get("type", "i32")
    value = _coerce(type_, argv[1])
    addrs = np.intersect1d(addrs, _sweep(s, type_, value), assume_unique=True)
    _save(addrs, type_)
    print(f"{addrs.size} candidates remain (now == {value})")


def _read_one(s: MemoryScanner, addr: int, fmt: str, isz: int):
    try:
        return struct.unpack(fmt, s.read_bytes(addr, isz))[0]
    except Exception:
        return "?"


def _cmd_list(s: MemoryScanner, argv: list) -> None:
    addrs, meta = _load()
    type_ = meta.get("type", "i32")
    fmt = _PACK[type_]
    isz = struct.calcsize(fmt)
    limit = int(argv[1]) if len(argv) > 1 else 20
    print(f"{addrs.size} candidates")
    for a in addrs[:limit].tolist():
        print(f"  0x{a:X} = {_read_one(s, a, fmt, isz)}")


def _cmd_count(s: MemoryScanner, argv: list) -> None:
    addrs, _ = _load()
    print(f"{addrs.size} candidates")


_COMMANDS = {"new": _cmd_new, "next": _cmd_next, "list": _cmd_list, "count": _cmd_count}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: vscan new <type> <value> | next <value> | list [n] | count")
        return 2
    handler = _COMMANDS.get(argv[0].lower())
    if handler is None:
        print(f"unknown command {argv[0]!r}")
        return 2
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("Could not attach to PlanetZoo.exe.")
        return 1
    handler(s, argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
