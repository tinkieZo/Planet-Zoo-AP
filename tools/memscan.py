"""memscan — an interactive memory scanner that replaces Cheat Engine for the
A2 spike. You operate the game and report values; this tool finds the addresses,
derives a patch-stable pointer chain, tests writes, and saves entries into
``pz_ap_client/memory/anchors.json``.

It reproduces CE's "known value" scan loop with pymem:

    new double 75000     # first scan: all places holding 75000.0
    # (spend money in-game so cash becomes 60000)
    next 60000           # narrow to addresses that now hold 60000.0
    # ... repeat until 1 candidate ...
    list
    ptrscan 0x<addr>     # find module_base + offsets path(s) to it
    write 0x<addr> 65000 # confirm the HUD changes (you watch the game)
    save cash 0x<addr>   # write a resolved anchor into anchors.json

Run (game must be running):
    python -m tools.memscan
    python -m tools.memscan --process PlanetZoo.exe

Type ``help`` inside for the full command list.

Scope note: pointer-scan finds *static* pointer chains (base module + offsets),
which survive restarts/patches better than absolute addresses. It does not do
CE's hardware-breakpoint "find what accesses" (pymem can't); for the rare anchor
that needs a code signature, fall back to CE for that one step.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from pz_ap_client.memory.anchors import DEFAULT_ANCHORS_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# value (un)packing
# ---------------------------------------------------------------------------

_FMT = {"i32": "<i", "u32": "<I", "i64": "<q", "u64": "<Q", "float": "<f", "double": "<d"}
_SIZE = {k: struct.calcsize(v) for k, v in _FMT.items()}


def pack(type_: str, value: str) -> bytes:
    fmt = _FMT[type_]
    num = float(value) if type_ in ("float", "double") else int(value, 0)
    return struct.pack(fmt, num)


def unpack(type_: str, data: bytes):
    return struct.unpack(_FMT[type_], data[: _SIZE[type_]])[0]


def values_equal(type_: str, a, b) -> bool:
    if type_ in ("float", "double"):
        return abs(a - b) <= max(1e-4, abs(b) * 1e-6)
    return a == b


# ---------------------------------------------------------------------------
# memory region enumeration (VirtualQueryEx)
# ---------------------------------------------------------------------------

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
_READABLE = {0x02, 0x04, 0x20, 0x40}        # READONLY, READWRITE, EXECUTE_READ, EXECUTE_READWRITE
_WRITABLE = {0x04, 0x40, 0x80}              # READWRITE, EXECUTE_READWRITE, WRITECOPY


class MemoryBasicInformation(ctypes.Structure):
    """Mirror of the Win32 ``MEMORY_BASIC_INFORMATION`` struct (for VirtualQueryEx)."""
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def iter_regions(handle: int, writable_only: bool = False) -> List[Tuple[int, int]]:
    """Return [(base, size)] of committed, readable (or writable) regions."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    virtual_query_ex = kernel32.VirtualQueryEx
    virtual_query_ex.restype = ctypes.c_size_t
    mbi = MemoryBasicInformation()
    addr = 0
    out: List[Tuple[int, int]] = []
    max_addr = 0x7FFFFFFFFFFF
    while addr < max_addr:
        ret = virtual_query_ex(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        size = mbi.RegionSize
        base = mbi.BaseAddress or 0
        prot = mbi.Protect
        if (mbi.State == MEM_COMMIT and not (prot & PAGE_GUARD) and prot != PAGE_NOACCESS):
            allowed = _WRITABLE if writable_only else _READABLE
            if prot in allowed:
                out.append((base, size))
        addr = base + size
    return out


# ---------------------------------------------------------------------------
# scan session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    scanner: MemoryScanner
    type_: str = "i32"
    candidates: Dict[int, object] = field(default_factory=dict)  # addr -> last value
    aligned: bool = True

    @property
    def handle(self) -> int:
        assert self.scanner.pm is not None and self.scanner.pm.process_handle is not None
        return self.scanner.pm.process_handle

    def _scan_regions_for(self, needle: bytes, writable_only: bool = True) -> List[int]:
        hits: List[int] = []
        step = _SIZE[self.type_] if self.aligned else 1
        for base, size in iter_regions(self.handle, writable_only=writable_only):
            try:
                buf = self.scanner.read_bytes(base, size)
            except Exception:
                continue
            start = 0
            while True:
                idx = buf.find(needle, start)
                if idx == -1:
                    break
                if (idx % step) == 0 or not self.aligned:
                    hits.append(base + idx)
                start = idx + 1
        return hits

    def first_scan(self, value: str, writable_only: bool = True) -> int:
        needle = pack(self.type_, value)
        addrs = self._scan_regions_for(needle, writable_only=writable_only)
        v: object = unpack(self.type_, needle)
        self.candidates = dict.fromkeys(addrs, v)
        return len(self.candidates)

    def next_scan_value(self, value: str) -> int:
        target = unpack(self.type_, pack(self.type_, value))
        survivors: Dict[int, object] = {}
        for addr in self.candidates:
            cur = self._read(addr)
            if cur is not None and values_equal(self.type_, cur, target):
                survivors[addr] = cur
        self.candidates = survivors
        return len(self.candidates)

    def next_scan_changed(self, changed: bool) -> int:
        survivors: Dict[int, object] = {}
        for addr, old in self.candidates.items():
            cur = self._read(addr)
            if cur is None:
                continue
            same = values_equal(self.type_, cur, old)
            if (not same) if changed else same:
                survivors[addr] = cur
        self.candidates = survivors
        return len(self.candidates)

    def next_scan_direction(self, increased: bool) -> int:
        survivors: Dict[int, object] = {}
        for addr, old in self.candidates.items():
            cur = self._read(addr)
            if cur is None:
                continue
            if (cur > old) if increased else (cur < old):
                survivors[addr] = cur
        self.candidates = survivors
        return len(self.candidates)

    def _read(self, addr: int):
        try:
            data = self.scanner.read_bytes(addr, _SIZE[self.type_])
            return unpack(self.type_, data)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# pointer scan (find a static module_base + offsets path to target)
# ---------------------------------------------------------------------------

@dataclass
class _PtrScanCtx:
    """Shared state for the pointer-scan recursion (keeps arg lists small)."""
    scanner: MemoryScanner
    regions: List[Tuple[int, int]]
    mod_lo: int
    mod_hi: int
    max_offset: int
    max_depth: int
    max_results: int
    results: List[List[int]] = field(default_factory=list)

    @property
    def full(self) -> bool:
        return len(self.results) >= self.max_results


def _scan_pointers(scanner: MemoryScanner, regions: List[Tuple[int, int]],
                   addr_lo: int, addr_hi: int) -> List[Tuple[int, int]]:
    """Return [(pointer_location, value)] for 8-aligned values in [addr_lo, addr_hi]."""
    found: List[Tuple[int, int]] = []
    for base, size in regions:
        try:
            buf = scanner.read_bytes(base, size)
        except Exception:
            continue
        for i in range(0, len(buf) - 8, 8):
            val = int.from_bytes(buf[i:i + 8], "little")
            if addr_lo <= val <= addr_hi:
                found.append((base + i, val))
    return found


def _collect_chains(ctx: _PtrScanCtx, target_addr: int, suffix: List[int], depth: int) -> None:
    """Recursively gather static pointer chains reaching ``target_addr`` into ctx.results."""
    if ctx.full:
        return
    for ploc, pval in _scan_pointers(ctx.scanner, ctx.regions, target_addr - ctx.max_offset, target_addr):
        chain = [target_addr - pval] + suffix
        if ctx.mod_lo <= ploc < ctx.mod_hi:
            ctx.results.append([ploc - ctx.mod_lo] + chain)  # static base offset + chain
        elif depth < ctx.max_depth:
            _collect_chains(ctx, ploc, chain, depth + 1)
        if ctx.full:
            return


def _dedupe_chains(chains: List[List[int]]) -> List[List[int]]:
    seen = set()
    uniq: List[List[int]] = []
    for chain in chains:
        key = tuple(chain)
        if key not in seen:
            seen.add(key)
            uniq.append(chain)
    return uniq


def pointer_scan(scanner: MemoryScanner, target: int, max_offset: int = 0x1000,
                 max_depth: int = 2, max_results: int = 20) -> List[List[int]]:
    """Find pointer paths [off0, off1, ...] such that walking from module_base
    (deref each offset except the last, which is added) reaches `target`.

    Breadth-limited: scans writable regions for 8-byte values pointing into
    [target-max_offset, target]. A pointer that itself lives in the main module
    yields a static chain. Otherwise we recurse on that pointer's address.
    """
    assert scanner.pm is not None and scanner.module_base is not None
    assert scanner.pm.process_handle is not None
    mod_base = scanner.module_base
    mod_size = scanner.module_size or 0x4000000
    ctx = _PtrScanCtx(
        scanner=scanner,
        regions=iter_regions(scanner.pm.process_handle, writable_only=True),
        mod_lo=mod_base,
        mod_hi=mod_base + mod_size,
        max_offset=max_offset,
        max_depth=max_depth,
        max_results=max_results,
    )
    _collect_chains(ctx, target, [], 0)
    return _dedupe_chains(ctx.results)


# ---------------------------------------------------------------------------
# anchors.json writer
# ---------------------------------------------------------------------------

def save_anchor(name: str, type_: str, offsets: List[int], notes: str,
                path: Optional[Path] = None) -> None:
    path = Path(path or DEFAULT_ANCHORS_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("anchors", {})[name] = {
        "kind": "module_offset",
        "type": type_,
        "offsets": offsets,
        "signature": None,
        "rip": None,
        "module_only": True,
        "notes": notes or f"resolved via memscan pointer-scan ({type_})",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

HELP = """commands:
  type <i32|u32|i64|u64|float|double>   set value type for scans
  align <on|off>                        align scans to type size (default on)
  new <value> [allmem]                  first scan for value (writable mem; 'allmem' = all readable)
  next <value>                          narrow to addresses now == value
  next changed | unchanged              narrow by whether value changed since last scan
  next inc | dec                        narrow by increased / decreased
  list [n]                              show up to n candidates (default 20)
  count                                 number of remaining candidates
  read <addr>                           read current value at addr (hex ok: 0x..)
  write <addr> <value>                  write value at addr (watch the game HUD to confirm)
  ptrscan <addr> [maxoff] [depth]       find module_base + offsets path(s) to addr
  save <name> <addr> [notes...]         pointer-scan addr, save best chain into anchors.json
  reset                                 clear current scan
  base                                  print module base/size
  help | quit
notes: 'addr' accepts hex (0x..) or decimal. After 'new', change the value in-game, then 'next <newvalue>'."""


def _addr(tok: str) -> int:
    return int(tok, 0)


# Each command is a small handler taking (session, args). Returning True exits
# the REPL. This dispatch table replaces a large if/elif chain.

def _cmd_help(session: Session, args: List[str]) -> None:
    print(HELP)


def _cmd_quit(session: Session, args: List[str]) -> bool:
    return True


def _cmd_type(session: Session, args: List[str]) -> None:
    if args and args[0] in _FMT:
        session.type_ = args[0]
        session.candidates.clear()
        print(f"type = {session.type_} ({_SIZE[session.type_]} bytes); scan reset")
    else:
        print(f"types: {', '.join(_FMT)}")


def _cmd_align(session: Session, args: List[str]) -> None:
    session.aligned = (not args) or args[0].lower() in ("on", "1", "true", "yes")
    print(f"aligned = {session.aligned}")


def _cmd_new(session: Session, args: List[str]) -> None:
    if not args:
        print("usage: new <value> [allmem]")
        return
    writable = not (len(args) > 1 and args[1].lower() == "allmem")
    n = session.first_scan(args[0], writable_only=writable)
    print(f"{n} candidates (type {session.type_})")


# 'next' subcommand -> (Session method, arg) so the handler stays flat.
_NEXT_SUB = {
    "changed": (Session.next_scan_changed, True),
    "unchanged": (Session.next_scan_changed, False),
    "inc": (Session.next_scan_direction, True),
    "increased": (Session.next_scan_direction, True),
    "dec": (Session.next_scan_direction, False),
    "decreased": (Session.next_scan_direction, False),
}


def _cmd_next(session: Session, args: List[str]) -> None:
    if not session.candidates:
        print("no active scan; use 'new' first")
        return
    if not args:
        print("usage: next <value|changed|unchanged|inc|dec>")
        return
    sub = _NEXT_SUB.get(args[0].lower())
    if sub is not None:
        method, flag = sub
        n = method(session, flag)
    else:
        n = session.next_scan_value(args[0])
    print(f"{n} candidates remain")


def _cmd_list(session: Session, args: List[str]) -> None:
    limit = int(args[0]) if args else 20
    for i, (addr, val) in enumerate(session.candidates.items()):
        if i >= limit:
            print(f"  ... and {len(session.candidates) - limit} more")
            break
        print(f"  0x{addr:X} = {val}")


def _cmd_count(session: Session, args: List[str]) -> None:
    print(len(session.candidates))


def _cmd_read(session: Session, args: List[str]) -> None:
    addr = _addr(args[0])
    print(f"0x{addr:X} = {session._read(addr)}")


def _cmd_write(session: Session, args: List[str]) -> None:
    addr = _addr(args[0])
    session.scanner.write_bytes(addr, pack(session.type_, args[1]))
    print(f"wrote {args[1]} ({session.type_}) to 0x{addr:X} — check the game HUD")


def _cmd_ptrscan(session: Session, args: List[str]) -> None:
    addr = _addr(args[0])
    maxoff = _addr(args[1]) if len(args) > 1 else 0x1000
    depth = int(args[2]) if len(args) > 2 else 2
    print(f"scanning for pointer chains to 0x{addr:X} (maxoff 0x{maxoff:X}, depth {depth})...")
    chains = pointer_scan(session.scanner, addr, maxoff, depth)
    if not chains:
        print("  no static pointer chain found (try larger maxoff/depth, or use a code signature in CE)")
    for ch in chains:
        print("  base + " + " -> ".join(f"0x{o:X}" for o in ch))


def _cmd_save(session: Session, args: List[str]) -> None:
    if len(args) < 2:
        print("usage: save <name> <addr> [notes...]")
        return
    name, addr, notes = args[0], _addr(args[1]), " ".join(args[2:])
    chains = pointer_scan(session.scanner, addr)
    if not chains:
        print("  no pointer chain found — not saved. Resolve manually (CE code signature).")
        return
    best = min(chains, key=len)  # shortest chain = most robust
    save_anchor(name, session.type_, best, notes)
    print(f"  saved anchor '{name}': base + {' -> '.join(f'0x{o:X}' for o in best)} "
          f"({session.type_}) into anchors.json")


def _cmd_reset(session: Session, args: List[str]) -> None:
    session.candidates.clear()
    print("scan reset")


def _cmd_base(session: Session, args: List[str]) -> None:
    print(f"module_base = 0x{session.scanner.module_base:X}, size = {session.scanner.module_size}")


COMMANDS: Dict[str, Callable[[Session, List[str]], Optional[bool]]] = {
    "help": _cmd_help,
    "quit": _cmd_quit, "exit": _cmd_quit, "q": _cmd_quit,
    "type": _cmd_type, "align": _cmd_align,
    "new": _cmd_new, "next": _cmd_next,
    "list": _cmd_list, "count": _cmd_count,
    "read": _cmd_read, "write": _cmd_write,
    "ptrscan": _cmd_ptrscan, "save": _cmd_save,
    "reset": _cmd_reset, "base": _cmd_base,
}


def repl(session: Session) -> None:
    print(HELP)
    while True:
        try:
            line = input("memscan> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        parts = line.split()
        handler = COMMANDS.get(parts[0].lower())
        if handler is None:
            print(f"unknown command {parts[0]!r}; type 'help'")
            continue
        try:
            if handler(session, parts[1:]):
                return
        except Exception as e:
            print(f"error: {e}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Interactive memory scanner for the A2 spike.")
    parser.add_argument("--process", default="PlanetZoo.exe", help="Target process name.")
    parser.add_argument("--type", default="i32", choices=list(_FMT), help="Initial value type.")
    args = parser.parse_args(argv)

    scanner = MemoryScanner(args.process)
    if not scanner.attach():
        print(f"Could not attach to {args.process!r}. Is the game running?")
        sys.exit(1)
    print(f"Attached to {args.process} (base 0x{scanner.module_base:X}).")
    repl(Session(scanner=scanner, type_=args.type))


if __name__ == "__main__":
    main()
