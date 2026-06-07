"""find_writer — the CE "find out what writes this address" equivalent.

Attaches as a debugger to PlanetZoo.exe, arms **hardware write-breakpoints**
(debug registers DR0..DR3) on up to 4 target addresses, and logs the instruction
(RIP) of whatever writes each one. Used to locate the exact instruction that
bumps a species count on birth (roster+0x630 for zebra) so we can hook *that* —
ground truth, unlike the give-birth call we mis-guessed from the decompile.

    python -m tools.find_writer 0x<addr1> [0x<addr2> ...] [seconds]

SAFETY: a hardware breakpoint left armed with no debugger attached CRASHES the
game (the debug exception goes unhandled). So this tool:
  * clears DR0..DR7 on every thread and detaches in a finally block,
  * sets DebugSetProcessKillOnExit(FALSE) so even a hard error leaves the game up,
  * is SELF-TERMINATING: it auto-cleans after capturing a few hits or after a
    deadline. DO NOT hard-kill it (that skips cleanup and leaves live BPs).

While attached we continuously pump debug events (DBG_CONTINUE), so the game runs
normally and only traps at the writes we care about.
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.memory.scanner import MemoryScanner  # noqa: E402

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
EXCEPTION_DEBUG_EVENT = 1
CREATE_PROCESS_DEBUG_EVENT = 3
CREATE_THREAD_DEBUG_EVENT = 2
EXIT_PROCESS_DEBUG_EVENT = 5
EXCEPTION_SINGLE_STEP = 0x80000004
EXCEPTION_BREAKPOINT = 0x80000003
TH32CS_SNAPTHREAD = 0x4
THREAD_ALL_ACCESS = 0x1FFFFF
CONTEXT_AMD64 = 0x100000
CONTEXT_CONTROL = CONTEXT_AMD64 | 0x1
CONTEXT_DEBUG_REGISTERS = CONTEXT_AMD64 | 0x10


# RW/LEN for the data breakpoints. Default: write, 4 bytes. ACCESS mode (set by main when the
# argv contains "access") = RW 11 (data read OR write) + LEN 00 (1 byte) — for "find what READS x".
_RW = 0b01
_LEN = 0b11


def _build_dr7(n: int) -> int:
    """DR7 enabling data breakpoints (mode in _RW/_LEN) for the first n debug registers.
    Per-DRk: local-enable bit 2k; RW field at bit 16+4k; LEN at bit 18+4k."""
    dr7 = 0
    for k in range(n):
        dr7 |= (1 << (2 * k))
        dr7 |= (_RW << (16 + 4 * k))
        dr7 |= (_LEN << (18 + 4 * k))
    return dr7


class CONTEXT(ctypes.Structure):
    _fields_ = [("P1Home", ctypes.c_uint64), ("P2Home", ctypes.c_uint64),
                ("P3Home", ctypes.c_uint64), ("P4Home", ctypes.c_uint64),
                ("P5Home", ctypes.c_uint64), ("P6Home", ctypes.c_uint64),
                ("ContextFlags", wintypes.DWORD), ("MxCsr", wintypes.DWORD),
                ("SegCs", wintypes.WORD), ("SegDs", wintypes.WORD),
                ("SegEs", wintypes.WORD), ("SegFs", wintypes.WORD),
                ("SegGs", wintypes.WORD), ("SegSs", wintypes.WORD),
                ("EFlags", wintypes.DWORD),
                ("Dr0", ctypes.c_uint64), ("Dr1", ctypes.c_uint64),
                ("Dr2", ctypes.c_uint64), ("Dr3", ctypes.c_uint64),
                ("Dr6", ctypes.c_uint64), ("Dr7", ctypes.c_uint64),
                ("Rax", ctypes.c_uint64), ("Rcx", ctypes.c_uint64),
                ("Rdx", ctypes.c_uint64), ("Rbx", ctypes.c_uint64),
                ("Rsp", ctypes.c_uint64), ("Rbp", ctypes.c_uint64),
                ("Rsi", ctypes.c_uint64), ("Rdi", ctypes.c_uint64),
                ("R8", ctypes.c_uint64), ("R9", ctypes.c_uint64),
                ("R10", ctypes.c_uint64), ("R11", ctypes.c_uint64),
                ("R12", ctypes.c_uint64), ("R13", ctypes.c_uint64),
                ("R14", ctypes.c_uint64), ("R15", ctypes.c_uint64),
                ("Rip", ctypes.c_uint64),
                ("_tail", ctypes.c_byte * 976)]


class ExceptionRecord(ctypes.Structure):
    _fields_ = [("ExceptionCode", wintypes.DWORD), ("ExceptionFlags", wintypes.DWORD),
                ("ExceptionRecord", ctypes.c_void_p), ("ExceptionAddress", ctypes.c_void_p),
                ("NumberParameters", wintypes.DWORD), ("ExceptionInformation", ctypes.c_uint64 * 15)]


class ExceptionDebugInfo(ctypes.Structure):
    _fields_ = [("ExceptionRecord", ExceptionRecord), ("dwFirstChance", wintypes.DWORD)]


class DebugEventU(ctypes.Union):
    _fields_ = [("Exception", ExceptionDebugInfo), ("raw", ctypes.c_byte * 160)]


class DebugEvent(ctypes.Structure):
    _fields_ = [("dwDebugEventCode", wintypes.DWORD), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD), ("u", DebugEventU)]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                ("dwFlags", wintypes.DWORD)]


def _ctx(flags: int) -> CONTEXT:
    c = CONTEXT(); c.ContextFlags = flags; return c


def _thread_ids(pid: int) -> "list[int]":
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    te = THREADENTRY32(); te.dwSize = ctypes.sizeof(THREADENTRY32)
    out = []
    if k32.Thread32First(snap, ctypes.byref(te)):
        while True:
            if te.th32OwnerProcessID == pid:
                out.append(te.th32ThreadID)
            if not k32.Thread32Next(snap, ctypes.byref(te)):
                break
    k32.CloseHandle(snap)
    return out


def _set_drs(tid: int, addrs: "list[int]") -> None:
    """Arm DR0..DR3 with the given addresses (empty list clears all). SuspendThread
    to be safe when called outside a debug-event freeze."""
    h = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
    if not h:
        return
    try:
        k32.SuspendThread(h)
        ctx = _ctx(CONTEXT_DEBUG_REGISTERS)
        if k32.GetThreadContext(h, ctypes.byref(ctx)):
            regs = [0, 0, 0, 0]
            for i, a in enumerate(addrs[:4]):
                regs[i] = a
            ctx.Dr0, ctx.Dr1, ctx.Dr2, ctx.Dr3 = regs
            ctx.Dr7 = _build_dr7(len(addrs[:4]))
            ctx.Dr6 = 0
            ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS
            k32.SetThreadContext(h, ctypes.byref(ctx))
        k32.ResumeThread(h)
    finally:
        k32.CloseHandle(h)


def _read_hit(tid: int) -> "tuple[int, int, int]":
    """Return (rip, dr6, rsp) for the trapped thread."""
    h = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
    if not h:
        return 0, 0, 0
    try:
        ctx = _ctx(CONTEXT_CONTROL | CONTEXT_DEBUG_REGISTERS)
        if k32.GetThreadContext(h, ctypes.byref(ctx)):
            return int(ctx.Rip), int(ctx.Dr6), int(ctx.Rsp)
    finally:
        k32.CloseHandle(h)
    return 0, 0, 0


def _is_return_addr(scanner, addr: int) -> bool:
    """True if ``addr`` is a plausible return address: the bytes just before it must be a CALL.
    Filters the stack scan down to REAL return addresses (an accurate call trace) instead of any
    stale module-range qword. Recognizes E8 rel32 (call), FF /2 forms (call r/m: FF 15 rip-rel,
    FF D0-D7 reg, FF 50/90/.. mem)."""
    try:
        b = scanner.read_bytes(addr - 7, 7)   # bytes preceding the candidate
    except Exception:
        return False
    if b[2] == 0xE8:                                   # E8 rel32 -> 5-byte call, ret at addr
        return True
    if b[1] == 0xFF and b[2] == 0x15:                  # FF 15 disp32 -> 6-byte call [rip+x]
        return True
    # FF /2 register/memory indirect calls (2-7 bytes): an FF whose modrm reg field == 2 (/2),
    # near the end of the window. Cheap check: an FF in the last few bytes with a /2 modrm next.
    for j in (5, 4, 3, 1, 0):
        if b[j] == 0xFF and j + 1 < 7 and ((b[j + 1] >> 3) & 7) == 2:
            return True
    return False


def _callers(scanner, rsp: int, modrange) -> "list[int]":
    """Walk the first 96 stack qwords and return VALIDATED return addresses (each preceded by a
    call) inside the module — an accurate caller chain, not a raw scan."""
    lo, hi = modrange
    try:
        data = scanner.read_bytes(rsp, 768)
    except Exception:
        return []
    out = []
    for i in range(0, len(data), 8):
        q = int.from_bytes(data[i:i + 8], "little")
        if lo <= q < hi and _is_return_addr(scanner, q):
            out.append(q)
        if len(out) >= 10:
            break
    return out


def _disasm(scanner: MemoryScanner, rip: int) -> str:
    """Disassemble the few instructions ending just before rip (a data BP traps
    AFTER the writing instruction, so the writer is just before rip)."""
    try:
        import capstone
        data = scanner.read_bytes(rip - 16, 32)
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        lines = [f"0x{i.address:X}: {i.mnemonic} {i.op_str}"
                 for i in md.disasm(data, rip - 16) if i.address < rip]
        return " | ".join(lines[-3:])
    except Exception as e:  # pragma: no cover
        return f"(disasm failed: {e})"


def _get_dr7(tid: int) -> int:
    h = k32.OpenThread(THREAD_ALL_ACCESS, False, tid)
    if not h:
        return 0
    try:
        ctx = _ctx(CONTEXT_DEBUG_REGISTERS)
        if k32.GetThreadContext(h, ctypes.byref(ctx)):
            return int(ctx.Dr7)
    finally:
        k32.CloseHandle(h)
    return 0


def _clear_all(pid: int) -> bool:
    """Clear DR0..DR7 on every thread and VERIFY none retain an armed DR7. A
    leftover hardware breakpoint with no debugger attached crashes the game, so we
    re-enumerate and retry until every thread reads DR7==0 (or we give up loudly)."""
    for attempt in range(6):
        tids = _thread_ids(pid)
        for tid in tids:
            _set_drs(tid, [])
        dirty = [tid for tid in _thread_ids(pid) if _get_dr7(tid) != 0]
        if not dirty:
            return True
        print("cleanup attempt %d: %d thread(s) still armed, retrying..." % (attempt + 1, len(dirty)), flush=True)
    print("WARNING: could not confirm all breakpoints cleared; DO NOT write those addresses.", flush=True)
    return False


def _read_i32(scanner, addr: int):
    try:
        return int.from_bytes(scanner.read_bytes(addr, 4), "little", signed=True)
    except Exception:
        return None


def _handle_exception(ev, scanner, base, addrs, modrange, seen, last) -> int:
    """Log each write with its delta (new - last) and the caller chain. +delta =
    birth/buy (the caller chain tells which), -delta = sell/release."""
    code = ev.u.Exception.ExceptionRecord.ExceptionCode
    if code == EXCEPTION_BREAKPOINT:
        return DBG_CONTINUE
    if code != EXCEPTION_SINGLE_STEP:
        return DBG_EXCEPTION_NOT_HANDLED
    rip, dr6, rsp = _read_hit(ev.dwThreadId)
    if not rip:
        return DBG_CONTINUE
    for i in range(min(4, len(addrs))):
        if not (dr6 & (1 << i)):
            continue
        a = addrs[i]
        new = _read_i32(scanner, a)
        prev = last.get(a)
        delta = (new - prev) if (new is not None and prev is not None) else None
        last[a] = new
        callers = _callers(scanner, rsp, modrange)
        key = (rip, (delta or 0) > 0, tuple(callers[:2]))
        if key not in seen:
            seen.add(key)
            ds = ("%+d" % delta) if delta is not None else "?"
            chain = " <- ".join("0x%X(RVA 0x%X)" % (c, c - base) for c in callers)
            print("WRITE [0x%X]=%s (delta %s)  rip=0x%X (RVA 0x%X)  %s\n    callers: %s"
                  % (a, new, ds, rip, rip - base, _disasm(scanner, rip), chain), flush=True)
    return DBG_CONTINUE


def _dispatch(ev, pid, addrs, scanner, base, modrange, seen, last) -> "tuple[int, bool]":
    """Handle one debug event. Returns (continue_status, should_stop)."""
    code = ev.dwDebugEventCode
    if code == CREATE_PROCESS_DEBUG_EVENT:
        for tid in _thread_ids(pid):
            _set_drs(tid, addrs)
        print("armed write-BPs on %s; do ONE birth then ONE buy. logs everything, "
              "self-cleans at deadline (do NOT kill me)."
              % ", ".join("0x%X" % a for a in addrs), flush=True)
    elif code == CREATE_THREAD_DEBUG_EVENT:
        _set_drs(ev.dwThreadId, addrs)
    elif code == EXCEPTION_DEBUG_EVENT:
        return _handle_exception(ev, scanner, base, addrs, modrange, seen, last), False
    elif code == EXIT_PROCESS_DEBUG_EVENT:
        return DBG_CONTINUE, True
    return DBG_CONTINUE, False


def _event_loop(pid: int, addrs, scanner, base: int, deadline: float) -> None:
    ev = DebugEvent()
    seen: set = set()
    last = {a: _read_i32(scanner, a) for a in addrs}
    modrange = (base, base + (scanner.module_size or 0x10000000))
    while time.time() <= deadline and len(seen) < 40:
        if not k32.WaitForDebugEvent(ctypes.byref(ev), 200):
            continue
        status, stop = _dispatch(ev, pid, addrs, scanner, base, modrange, seen, last)
        k32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status)
        if stop:
            return


def main(argv=None) -> int:
    global _RW, _LEN
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m tools.find_writer 0x<addr1> [0x<addr2> ...] [seconds] [access]"); return 2
    if "access" in [a.lower() for a in argv]:
        argv = [a for a in argv if a.lower() != "access"]
        _RW, _LEN = 0b11, 0b00   # break on data read OR write, 1 byte (find what READS x)
        print("ACCESS mode: watching for READS or writes (1 byte each)")
    timeout = 300
    if not argv[-1].lower().startswith("0x"):
        timeout = int(argv.pop())
    addrs = [int(a, 16) for a in argv][:4]

    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("not attached"); return 1
    pid, base = s.pm.process_id, s.module_base

    if not k32.DebugActiveProcess(pid):
        print("DebugActiveProcess failed err=%d (already debugged?)" % ctypes.get_last_error()); return 1
    k32.DebugSetProcessKillOnExit(False)
    try:
        _event_loop(pid, addrs, s, base, time.time() + timeout)
    finally:
        _clear_all(pid)
        k32.DebugActiveProcessStop(pid)
        print("detached; breakpoints cleared.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
