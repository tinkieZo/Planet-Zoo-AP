"""Generic memory access for the hooking client - pymem wrapper.

No game-specific knowledge lives here. It provides exactly the primitives the
A2 spike needs once anchors are known:

  * attach to the running process by name,
  * AOB / signature scan (IDA-style "48 8B 05 ?? ?? ?? ??" patterns), so we
    don't depend on absolute addresses that move every patch,
  * resolve a static **pointer chain** (base module + offsets) to a final
    address,
  * typed read/write helpers.

It degrades gracefully when ``pymem`` isn't available or the game isn't running,
so the rest of the client (and the A1 console) keeps working with no game.
"""

from __future__ import annotations

import ctypes
import logging
import re
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Sequence

logger = logging.getLogger("PZClient")

# VirtualQueryEx plumbing for heap region enumeration (scan_heap_for_qword). x64 MBI layout.
_MEM_COMMIT = 0x1000
_PAGE_GUARD = 0x100
_WRITABLE_PROT = {0x04, 0x08, 0x40, 0x80}  # READWRITE / WRITECOPY / EXEC_READWRITE / EXEC_WRITECOPY


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", ctypes.c_ulong),
        ("__alignment1", ctypes.c_ulong),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("__alignment2", ctypes.c_ulong),
    ]

if TYPE_CHECKING:
    # Unconditional name for annotations; never imported at runtime.
    from pymem import Pymem

try:
    import pymem
    import pymem.pattern
    import pymem.process
except Exception:  # pragma: no cover - import guard
    pymem = None  # type: ignore[assignment]


class MemoryAccessError(Exception):
    """Raised on attach / scan / read / write failures we want callers to see."""


_TOKEN = re.compile(r"^(\?\?|[0-9A-Fa-f]{2})$")


def parse_aob(signature: str) -> bytes:
    """Convert an IDA/Cheat-Engine style AOB string to a pymem regex pattern.

    ``"48 8B 05 ?? ?? ?? ?? 90"`` -> ``b"\\x48\\x8b\\x05....\\x90"``

    Wildcards (``??`` or ``?``) become ``.`` (any byte; pymem scans with
    ``re.DOTALL``). Raises on malformed tokens so a typo in the table fails
    loudly rather than scanning for garbage.
    """
    out = bytearray()
    for tok in signature.split():
        if tok in ("??", "?"):
            out += b"."
            continue
        if not _TOKEN.match(tok):
            raise MemoryAccessError(f"bad AOB token {tok!r} in signature {signature!r}")
        byte = int(tok, 16)
        # Escape regex-special bytes so the pattern matches literally.
        if byte in b".^$*+?()[]{}|\\":
            out += b"\\"
        out.append(byte)
    return bytes(out)


@dataclass
class MemoryScanner:
    process_name: str
    pm: "Optional[Pymem]" = None
    module_base: Optional[int] = None
    module_size: Optional[int] = None

    # -- lifecycle -------------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self.pm is not None

    def attach(self) -> bool:
        """Attach to the process. Returns False (no raise) if not running, so the
        poll loop can keep retrying while the player launches the game."""
        if pymem is None:
            logger.warning("pymem not available; memory layer disabled")
            return False
        try:
            self.pm = pymem.Pymem(self.process_name)
        except Exception as e:
            logger.debug("attach to %s failed: %s", self.process_name, e)
            self.pm = None
            return False
        module = pymem.process.module_from_name(self.pm.process_handle, self.process_name)
        if module is None:
            # Fall back to the first module / base address.
            self.module_base = self.pm.base_address
            self.module_size = None
        else:
            self.module_base = module.lpBaseOfDll
            self.module_size = module.SizeOfImage
        logger.info("Attached to %s (base 0x%X)", self.process_name, self.module_base or 0)
        return True

    def detach(self) -> None:
        if self.pm is not None:
            try:
                self.pm.close_process()
            except Exception:
                pass
        self.pm = None
        self.module_base = None

    def _require(self) -> "Pymem":
        if self.pm is None:
            raise MemoryAccessError("not attached to game process")
        return self.pm

    # -- scanning --------------------------------------------------------------

    def aob_scan(self, signature: str, module_only: bool = True) -> Optional[int]:
        """Scan for an AOB signature. Returns the match address or None.

        ``module_only`` restricts the scan to the main module (fast, typical for
        code signatures). Set False to scan all committed regions (slower; for
        heap data you usually resolve via a pointer chain instead)."""
        pm = self._require()
        assert pymem is not None  # attached implies the import succeeded
        pattern = parse_aob(signature)
        try:
            if module_only and self.module_base is not None:
                module = pymem.process.module_from_name(pm.process_handle, self.process_name)
                hit = pymem.pattern.pattern_scan_module(pm.process_handle, module, pattern)
            else:
                hit = pymem.pattern.pattern_scan_all(pm.process_handle, pattern)
            # We never request return_multiple, so a single address or None is
            # expected; coerce to Optional[int] to satisfy the declared return.
            return hit if isinstance(hit, int) else None
        except Exception as e:
            raise MemoryAccessError(f"AOB scan failed for {signature!r}: {e}") from e

    def resolve_rip_relative(self, instr_addr: int, disp_offset: int, instr_len: int) -> int:
        """Resolve a RIP-relative reference (common in x64).

        Given the address of an instruction like ``mov rax,[rip+disp32]``, the
        4-byte displacement is at ``instr_addr + disp_offset`` and the target is
        ``instr_addr + instr_len + disp32``. Used to turn a code signature into
        the address of the static it references."""
        disp = self.read_i32(instr_addr + disp_offset)
        return instr_addr + instr_len + disp

    def resolve_pointer_chain(self, base: int, offsets: Sequence[int]) -> Optional[int]:
        """Walk a static pointer chain to a final address, or None if the chain breaks.

        Convention (matches Cheat Engine): dereference at each offset except the
        last, which is added to produce the final address::

            addr = base
            for off in offsets[:-1]:
                addr = *(addr + off)
            final = addr + offsets[-1]

        A null or unreadable intermediate deref means the chain isn't live (e.g. the manager isn't
        allocated yet - not in a loaded zoo), so we return None rather than adding the final offset
        to a near-null address and reading there (which raises and would crash the poll loop)."""
        pm = self._require()
        if not offsets:
            return base
        addr = base
        for off in offsets[:-1]:
            try:
                addr = int.from_bytes(pm.read_bytes(addr + off, 8), "little")
            except Exception:
                return None
            if not addr:
                return None
        return addr + offsets[-1]

    def scan_heap_for_qword(self, value: int, max_hits: int = 16,
                            max_region: int = 0x4000000) -> List[int]:
        """Addresses (8-aligned) in committed, writable, non-guard memory whose 8-byte qword == value.

        Used to locate a game object by its vtable pointer when the static pointer CHAINS to it are
        unreliable (the research-system chains miss in some saves; the vtable rva is build-stable). Reads
        each region whole and skips regions >= ``max_region`` (multi-hundred-MB texture/mesh buffers)."""
        pm = self._require()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.VirtualQueryEx.restype = ctypes.c_size_t
        mbi = _MBI()
        needle = struct.pack("<Q", value)
        addr, out = 0, []
        while len(out) < max_hits:
            if not k32.VirtualQueryEx(pm.process_handle, ctypes.c_void_p(addr),
                                      ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize
            if (mbi.State == _MEM_COMMIT and not (mbi.Protect & _PAGE_GUARD)
                    and (mbi.Protect & 0xFF) in _WRITABLE_PROT and 0 < size < max_region):
                self._find_qword(base, size, needle, out, max_hits)
            nxt = base + size
            if nxt <= addr:
                break
            addr = nxt
        return out

    def _find_qword(self, base: int, size: int, needle: bytes, out: List[int], max_hits: int) -> None:
        try:
            blob = self._require().read_bytes(base, size)
        except Exception:
            return
        i = blob.find(needle)
        while i != -1 and len(out) < max_hits:
            if i % 8 == 0:
                out.append(base + i)
            i = blob.find(needle, i + 1)

    # -- typed read/write ------------------------------------------------------

    def read_bytes(self, addr: int, size: int) -> bytes:
        return self._require().read_bytes(addr, size)

    def read_i32(self, addr: int) -> int:
        return struct.unpack("<i", self.read_bytes(addr, 4))[0]

    def read_i64(self, addr: int) -> int:
        return struct.unpack("<q", self.read_bytes(addr, 8))[0]

    def read_qword(self, addr: int) -> Optional[int]:
        """Read an unsigned 8-byte qword, returning None on a failed/invalid read (for pointer walks)."""
        try:
            return struct.unpack("<Q", self.read_bytes(addr, 8))[0]
        except Exception:
            return None

    def read_double(self, addr: int) -> float:
        return struct.unpack("<d", self.read_bytes(addr, 8))[0]

    def read_float(self, addr: int) -> float:
        return struct.unpack("<f", self.read_bytes(addr, 4))[0]

    def write_bytes(self, addr: int, data: bytes) -> None:
        self._require().write_bytes(addr, data, len(data))

    def write_i32(self, addr: int, value: int) -> None:
        self.write_bytes(addr, struct.pack("<i", value))

    def write_i64(self, addr: int, value: int) -> None:
        self.write_bytes(addr, struct.pack("<q", value))

    def write_double(self, addr: int, value: float) -> None:
        self.write_bytes(addr, struct.pack("<d", value))

    def write_float(self, addr: int, value: float) -> None:
        self.write_bytes(addr, struct.pack("<f", value))
