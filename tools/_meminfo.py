"""_meminfo - shared Win32 committed-region enumeration for the RE tools.

Several tools walk the target process's committed memory via VirtualQueryEx, filtered by page
protection (executable / readable / writable) and sometimes capped by region size or address range.
This centralizes the _MemoryBasicInformation struct, the protection-flag sets, and the walk loop, so
each tool just picks a protection set instead of re-declaring the struct + loop.

    from tools._meminfo import enum_regions, EXEC, READABLE, WRITABLE
    for base, size in enum_regions(handle, READABLE, max_size=0x10000000):
        ...
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
MAX_ADDR = 0x7FFFFFFFFFFF

# page-protection low-byte (Protect & 0xFF) sets:
EXEC = {0x10, 0x20, 0x40, 0x80}                    # EXECUTE / _READ / _READWRITE / WRITECOPY-exec
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}    # READONLY / READWRITE / WRITECOPY / + the exec-read variants
WRITABLE = {0x04, 0x08, 0x40, 0x80}                # READWRITE / WRITECOPY / EXEC_READWRITE / EXEC_WRITECOPY


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]


def enum_regions(handle, protect, lo=0, hi=MAX_ADDR, max_size=None):
    """Return [(base, size), ...] for every committed, non-guard region in [lo, hi) whose page
    protection (Protect & 0xFF) is in `protect`. If max_size is given, regions >= max_size are skipped
    (used to drop multi-hundred-MB texture/mesh buffers)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    mbi = _MemoryBasicInformation()
    addr = lo
    out = []
    while addr < hi:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base = mbi.BaseAddress or 0
        if (mbi.State == MEM_COMMIT and not (mbi.Protect & PAGE_GUARD)
                and (mbi.Protect & 0xFF) in protect
                and (max_size is None or mbi.RegionSize < max_size)):
            out.append((base, mbi.RegionSize))
        addr = base + mbi.RegionSize
    return out
