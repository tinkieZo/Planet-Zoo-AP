"""dump_bindings — resolve Cobra script-function names to their native executors.

Planet Zoo registers script functions via the REAL binding registrar 0x14144BE30 (name in rdx);
each registration site is `... lea <reg>,[rip->HANDLER] ; ... ; lea rdx,[rip->name] ; call 0x14144BE30`
where HANDLER is a 5-byte thunk `jmp <native_executor>`. This scans the live process's executable
regions for calls to the registrar, recovers each name + handler + executor, and prints those whose
name matches a substring filter (case-insensitive). See memory/cobra-script-binding-catalog.md.

    python -m tools.dump_bindings [name_substring ...]

With no filter, prints all resolved bindings (long). Read-only; no patching.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import capstone  # noqa: E402
from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402
from tools._meminfo import enum_regions, EXEC  # noqa: E402

REGISTRAR = 0x14144BE30


def _rip_lea_target(insn):
    """If insn is `lea r64, [rip+disp]`, return the effective address, else None."""
    if insn.mnemonic != "lea":
        return None
    for op in insn.operands:
        if op.type == capstone.x86.X86_OP_MEM and op.mem.base == capstone.x86.X86_REG_RIP:
            return insn.address + insn.size + op.mem.disp
    return None


def _cstr(s, addr, n=96):
    try:
        b = s.read_bytes(addr, n)
    except Exception:
        return None
    end = b.find(b"\x00")
    if end <= 0:
        return None
    try:
        return b[:end].decode("ascii")
    except Exception:
        return None


def _executor_of(s, handler):
    """A binding handler is a 5-byte ``jmp executor`` thunk; return the executor it jumps to, or None."""
    try:
        hb = s.read_bytes(handler, 5)
        if hb[0] == 0xE9:
            return handler + 5 + int.from_bytes(hb[1:5], "little", signed=True)
    except Exception:
        pass
    return None


def _registrar_call_offsets(data, rbase, seen):
    """Yield byte offsets of `call REGISTRAR` sites (E8 rel32 -> REGISTRAR) not already seen."""
    i = data.find(b"\xE8")
    while i != -1:
        call_addr = rbase + i
        rel = int.from_bytes(data[i + 1:i + 5], "little", signed=True)
        if call_addr + 5 + rel == REGISTRAR and call_addr not in seen:
            seen.add(call_addr)
            yield i
        i = data.find(b"\xE8", i + 1)


def _resolve_binding(s, md, data, rbase, i):
    """Recover (name_str, handler) for a `call REGISTRAR` at data[i] by disassembling the preceding
    window: the registrar takes the binding NAME in rdx (lea rdx,[rip->name]) + a HANDLER thunk (lea)."""
    call_addr = rbase + i
    w = max(call_addr - 0x40, rbase)
    name = handler = None
    for insn in md.disasm(data[w - rbase:i + 5], w):
        tgt = _rip_lea_target(insn)
        if tgt is None:
            continue
        if insn.operands[0].reg == capstone.x86.X86_REG_RDX:
            name = tgt
        else:
            handler = tgt
    return (_cstr(s, name) if name else None), handler


def _scan_region(s, md, rbase, data, seen, filters) -> int:
    """Resolve + print every registrar binding in one exec region; return how many matched the filter."""
    found = 0
    for i in _registrar_call_offsets(data, rbase, seen):
        nm, handler = _resolve_binding(s, md, data, rbase, i)
        if not nm or (filters and not any(f in nm.lower() for f in filters)):
            continue
        exe = _executor_of(s, handler) if handler else None
        print("  %-44s handler=0x%-10X executor=%s"
              % (nm, handler or 0, ("0x%X" % exe) if exe else "?"), flush=True)
        found += 1
    return found


def main() -> int:
    filters = [a.lower() for a in sys.argv[1:]]
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    base = s.module_base
    size = getattr(s, "module_size", None) or 0x30000000
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    regions = enum_regions(s.pm.process_handle, EXEC, lo=base, hi=base + size)
    print("scanning %d exec regions for calls to registrar 0x%X..." % (len(regions), REGISTRAR), flush=True)

    seen: set = set()
    found = 0
    for rbase, rsize in regions:
        try:
            data = s.read_bytes(rbase, rsize)
        except Exception:
            continue
        found += _scan_region(s, md, rbase, data, seen, filters)
    print("done — %d matching bindings (%d call sites scanned)" % (found, len(seen)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
