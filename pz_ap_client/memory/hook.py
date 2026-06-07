"""Code-injection / detour primitives for the hooking client.

The read/write anchor layer can't observe dynamically-managed events (animal
births, etc.) — those objects have no restart-stable address (see the A2 spike).
The robust route, proven in tools/inject_poc.py + tools/hook_poc.py, is to detour
a STABLE code instruction: redirect it to an allocated trampoline that records the
event into a scratch region the client polls, then runs the original instruction
and jumps back.

This module wraps the OS plumbing (allocate-near, suspend-during-patch, build the
trampoline, install/restore) so callers only supply: the hook site (resolved from
an AOB signature), the original instruction's length+bytes, and the trampoline
body. It degrades gracefully if pymem / the process is unavailable.

Safety contract: ``HookManager`` verifies the target bytes before patching, patches
under a full process suspend (no mid-write race), and ``restore_all()`` puts every
hooked site back and frees its trampoline (call it on disconnect / in a finally).
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
MEM_FREE = 0x10000
PAGE_EXECUTE_READWRITE = 0x40
_REL32_LIMIT = 0x7FFF0000  # stay safely inside +/-2GB for rel32 reachability

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_nt = ctypes.WinDLL("ntdll")
_k32.VirtualAllocEx.restype = ctypes.c_void_p
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
_k32.VirtualQueryEx.restype = ctypes.c_size_t


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]


def _alloc_near(handle: int, target: int, size: int = 0x1000) -> int:
    """Allocate an RWX page within +/-2GB of ``target`` so a rel32 jmp reaches it."""
    mbi = _MBI()
    addr = max(target - _REL32_LIMIT, 0x10000)
    hi = target + _REL32_LIMIT
    while addr < hi:
        if not _k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        base, region, state = mbi.BaseAddress or 0, mbi.RegionSize, mbi.State
        if state == MEM_FREE and region >= size:
            got = _k32.VirtualAllocEx(handle, ctypes.c_void_p(base), size,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE)
            if got:
                return int(got)
        addr = base + region
    return 0


@dataclass
class _Hook:
    site: int
    original: bytes          # the exact bytes we overwrote (for restore)
    region: int              # allocated trampoline region (to free)


@dataclass
class HookManager:
    """Installs jmp-detour hooks and guarantees clean teardown."""
    scanner: "object"        # MemoryScanner (duck-typed: read_bytes/write_bytes/module_base)
    hooks: Dict[str, _Hook] = field(default_factory=dict)

    @property
    def _handle(self) -> int:
        return self.scanner.pm.process_handle

    def _suspend(self) -> None:
        _nt.NtSuspendProcess(self._handle)

    def _resume(self) -> None:
        _nt.NtResumeProcess(self._handle)

    def install(self, name: str, site: int, original: bytes, make_trampoline) -> bool:
        """Detour ``site`` to a trampoline. ``original`` must match the bytes
        currently at ``site`` (verified); it is replaced with a 5-byte jmp + NOP
        padding. ``make_trampoline(region, scratch, resume_addr)`` returns the
        trampoline bytes (it must run ``original`` and jmp to ``resume_addr``).
        Returns False (no change) on byte-mismatch or allocation failure."""
        if len(original) < 5:
            raise ValueError("need >=5 bytes to place a rel32 jmp")
        cur = self.scanner.read_bytes(site, len(original))
        if cur != original:
            return False
        region = _alloc_near(self._handle, site)
        if not region:
            return False
        scratch, code = region, region + 0x40
        self.scanner.write_bytes(scratch, b"\x00" * 0x40)
        self.scanner.write_bytes(code, make_trampoline(region, scratch, site + len(original)))
        patch = b"\xE9" + struct.pack("<i", code - (site + 5)) + b"\x90" * (len(original) - 5)
        self._suspend()
        try:
            self.scanner.write_bytes(site, patch)
        finally:
            self._resume()
        self.hooks[name] = _Hook(site=site, original=original, region=region)
        return True

    def scratch(self, name: str) -> Optional[int]:
        h = self.hooks.get(name)
        return h.region if h else None

    def restore(self, name: str) -> None:
        h = self.hooks.pop(name, None)
        if not h:
            return
        self._suspend()
        try:
            self.scanner.write_bytes(h.site, h.original)
        finally:
            self._resume()
        _k32.VirtualFreeEx(self._handle, ctypes.c_void_p(h.region), 0, MEM_RELEASE)

    def restore_all(self) -> None:
        # restore() pops from self.hooks, so drain by repeated pop rather than
        # iterating self.hooks (which would RuntimeError: changed size mid-loop).
        while self.hooks:
            self.restore(next(iter(self.hooks)))


# Scratch layout for the permit purchase-block hook (within the 0x40 scratch block):
#   [scratch + 0x00]  u64 exchange_mgr (r15 at the hook; trampoline writes it for the client)
#   [scratch + 0x08]  u32 blocked_count (client writes; number of blocked SPECIES handles)
#   [scratch + 0x10]  u32[PERMIT_BLOCKED_MAX] blocked species handles (client writes)
# Species-level gate: the trampoline compares the listing's species handle [rbx+0x10]
# (e.g. 0x30B0) against this set, so blocking one handle gates EVERY listing of that
# species (many listings share a species). Handles are content-def indices, stable
# across the session; the client maps species_key -> handle once (see permit_hook.py).
PERMIT_BLOCKED_MAX = 12   # fits before code at region+0x40 (0x10 + 12*4 = 0x40)
PERMIT_SCRATCH_MGR = 0x00
PERMIT_SCRATCH_COUNT = 0x08
PERMIT_SCRATCH_IDS = 0x10
PERMIT_LISTING_SPECIES_OFF = 0x10   # [rbx+0x10] = species handle within the listing record
PERMIT_LISTING_ID_OFF = 0x228       # [rbx+0x228] = listing id within the listing record
# Purchase-log slots (region+0x100..; clear of the blocked set at 0x10-0x40 and the
# ~0xa8 bytes of code at 0x40). Records the LAST purchase the hook saw so the client
# can discover species_key -> handle empirically (user buys a known species; read it).
PERMIT_LOG_HANDLE = 0x100   # u32 last species handle seen at the buy site
PERMIT_LOG_ID = 0x104       # u32 last listing id seen
PERMIT_LOG_COUNT = 0x108    # u32 monotonic fire count

# Register save/restore framing the permit detours' scratch work (matched pair):
_PERMIT_SAVE = b"\x9C\x50\x41\x52\x41\x53"     # pushfq; push rax; push r10; push r11
_PERMIT_RESTORE = b"\x41\x5B\x41\x5A\x58\x9D"  # pop r11; pop r10; pop rax; popfq

# Trampoline code is written at region+0x40 (the first 0x40 bytes are the scratch block).
_CODE_OFF = 0x40
_JMP_REL32 = b"\xE9\x00\x00\x00\x00"           # `jmp rel32` placeholder; patch the rel32 later


def _emit_jmp(body: bytearray) -> int:
    """Append a placeholder ``jmp rel32`` to ``body``; return the opcode offset (for _patch_jmp)."""
    off = len(body)
    body += _JMP_REL32
    return off


def _patch_jmp(body: bytearray, off: int, region: int, target: int) -> None:
    """Patch the rel32 of the placeholder jmp at ``off`` (in a region+0x40 trampoline) to
    point at absolute ``target``."""
    code = region + _CODE_OFF
    struct.pack_into("<i", body, off + 1, target - (code + off + 5))


def _final_jmp(body, region: int, target: int) -> bytes:
    """Return ``body`` plus a trailing ``jmp rel32`` to absolute ``target`` (for trampolines that
    end with a single jmp back, computing the rel32 from the final body length)."""
    jmp_end = region + _CODE_OFF + len(body) + 5
    return bytes(body) + b"\xE9" + struct.pack("<i", target - jmp_end)


def make_permit_trampoline(region: int, scratch: int, resume_addr: int, fail_addr: int,
                           original: bytes) -> bytes:
    """Conditional-abort detour for the Animal-Exchange purchase (`FUN_14A089410`
    @ the listing-found site `0x14A0894E5`). At the hook rbx=listing record,
    r15=exchange_mgr. Records r15 (so the client can poll listings), then checks the
    listing's SPECIES handle [rbx+0x10] against the client-written blocked set: if
    blocked → jmp the fail-return (`0x14A0894B7`, purchase fails, no spawn); else run
    the relocated `movzx` and jmp resume. Uses rax/r10/r11 + flags (push/pop);
    preserves rbx/r15/rsi and the frame so the fail-return's epilogue runs correctly.
    rel32 jmps (no register) so eax (movzx result) is intact at resume."""
    body = bytearray()
    body += _PERMIT_SAVE                               # pushfq; push rax; push r10; push r11
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x4C\x89\x38"                             # mov [rax], r15            (exchange_mgr)
    # purchase-log: record this buy's species handle + listing id (r10/r11 are saved)
    body += b"\x44\x8B\x53" + bytes([PERMIT_LISTING_SPECIES_OFF])      # mov r10d, [rbx+0x10] (species)
    body += b"\x44\x89\x90" + struct.pack("<i", PERMIT_LOG_HANDLE)     # mov [rax+0x100], r10d
    body += b"\x44\x8B\x9B" + struct.pack("<i", PERMIT_LISTING_ID_OFF) # mov r11d, [rbx+0x228] (id)
    body += b"\x44\x89\x98" + struct.pack("<i", PERMIT_LOG_ID)         # mov [rax+0x104], r11d
    body += b"\xFF\x80" + struct.pack("<i", PERMIT_LOG_COUNT)          # inc dword [rax+0x108]
    body += b"\x44\x8B\x50" + bytes([PERMIT_SCRATCH_COUNT])   # mov r10d, [rax+8]    (count)
    body += b"\x45\x85\xD2"                             # test r10d, r10d
    jz_allow_at = len(body); body += b"\x74\x00"        # jz allow            (patch rel8)
    body += b"\x4C\x8D\x58" + bytes([PERMIT_SCRATCH_IDS])     # lea r11, [rax+0x10] (blocked set)
    body += b"\x8B\x43" + bytes([PERMIT_LISTING_SPECIES_OFF]) # mov eax, [rbx+0x10] (species handle)
    loop_at = len(body)
    body += b"\x41\x3B\x03"                             # cmp eax, [r11]      (species vs blocked)
    je_block_at = len(body); body += b"\x74\x00"        # je block            (patch rel8)
    body += b"\x49\x83\xC3\x04"                         # add r11, 4
    body += b"\x41\xFF\xCA"                             # dec r10d
    jnz_loop_at = len(body); body += b"\x75\x00"        # jnz loop            (patch rel8)
    allow_at = len(body)
    body += _PERMIT_RESTORE                            # pop r11; pop r10; pop rax; popfq
    body += original                                    # relocated movzx eax,[rbx+0x210]
    jmp_resume_at = _emit_jmp(body)
    block_at = len(body)
    body += _PERMIT_RESTORE                            # pop r11; pop r10; pop rax; popfq
    jmp_fail_at = _emit_jmp(body)
    # patch the rel8 branches
    body[jz_allow_at + 1] = (allow_at - (jz_allow_at + 2)) & 0xFF
    body[je_block_at + 1] = (block_at - (je_block_at + 2)) & 0xFF
    body[jnz_loop_at + 1] = (loop_at - (jnz_loop_at + 2)) & 0xFF
    # patch the rel32 exits (code lives at region+0x40)
    _patch_jmp(body, jmp_resume_at, region, resume_addr)
    _patch_jmp(body, jmp_fail_at, region, fail_addr)
    return bytes(body)


# Scratch layout for the facility PLACEMENT-block hook (mirrors the permit gate). The
# gated id here is a building/blueprint DEFINITION id (a content-def id, expected to be
# content-stable across restarts — unlike the per-session species handle the permit gate
# uses; verify on capture). Layout within the 0x40 scratch block:
#   [scratch + 0x00]  u32 blocked_count (client writes; number of blocked facility def-ids)
#   [scratch + 0x08]  u32[FACILITY_BLOCKED_MAX] blocked facility def-ids (client writes)
#   [scratch + 0x100..] capture log (def-id / fire-count) so the user can map facility->id
FACILITY_BLOCKED_MAX = 12      # 0x08 + 12*4 = 0x38, fits before code at region+0x40
FACILITY_SCRATCH_COUNT = 0x00
FACILITY_SCRATCH_IDS = 0x08
FACILITY_LOG_ID = 0x100        # u32 last building def-id seen at the placement site
FACILITY_LOG_COUNT = 0x108     # u32 monotonic fire count
# --- Executor specifics (TO FILL from Ghidra; see docs/A2_RE_HANDOFF.md "FACILITY GATE") ---
# The register holding the building/blueprint object at the hook site, and the u32 offset
# of its definition id within that object. Defaults assume rbx-> object (as the permit site
# had rbx-> listing); CONFIRM from the disassembly and adjust FACILITY_DEFID_REG/_OFF.
FACILITY_DEFID_OFF = 0x10      # [<reg>+0x10] = building def-id (placeholder; confirm)


def make_facility_gate(region: int, scratch: int, resume_addr: int, fail_addr: int,
                       original: bytes, defid_off: int = FACILITY_DEFID_OFF) -> bytes:
    """Conditional-abort detour for the building-PLACEMENT commit executor — the facility
    gate (research_centre / vet_surgery / workshop / trade_centre). Structurally identical
    to ``make_permit_trampoline``: at the hook a register (assumed rbx) points at the
    building/blueprint object; its def-id at ``[rbx+defid_off]`` is compared against the
    client-written blocked set. Blocked -> jmp ``fail_addr`` (placement aborts, nothing
    built); else log it (capture) + run the relocated ``original`` + jmp resume.

    NOTE: this assumes the same (rbx-base, jmp-fail) shape as the permit site. The exact
    base register, ``defid_off``, ``original`` bytes, and whether a clean ``xor eax,eax;ret``
    abort is possible instead of a fail-return MUST be confirmed from the placement
    executor's disassembly before install (FacilityGate keeps the hook un-installed until
    FACILITY_RVA is filled, so this is never run with placeholder values)."""
    body = bytearray()
    body += _PERMIT_SAVE                                       # pushfq; push rax; push r10; push r11
    body += b"\x48\xB8" + struct.pack("<Q", scratch)            # movabs rax, scratch
    body += b"\x44\x8B\x53" + bytes([defid_off])               # mov r10d, [rbx+off]  (def-id)
    body += b"\x44\x89\x90" + struct.pack("<i", FACILITY_LOG_ID)   # mov [rax+0x100], r10d  (capture)
    body += b"\xFF\x80" + struct.pack("<i", FACILITY_LOG_COUNT)    # inc dword [rax+0x108]
    body += b"\x44\x8B\x50" + bytes([FACILITY_SCRATCH_COUNT])   # mov r10d, [rax+0]    (count)
    body += b"\x45\x85\xD2"                                     # test r10d, r10d
    jz_allow_at = len(body); body += b"\x74\x00"                # jz allow
    body += b"\x4C\x8D\x58" + bytes([FACILITY_SCRATCH_IDS])     # lea r11, [rax+8]     (blocked set)
    body += b"\x8B\x43" + bytes([defid_off])                   # mov eax, [rbx+off]   (def-id)
    loop_at = len(body)
    body += b"\x41\x3B\x03"                                     # cmp eax, [r11]
    je_block_at = len(body); body += b"\x74\x00"                # je block
    body += b"\x49\x83\xC3\x04"                                 # add r11, 4
    body += b"\x41\xFF\xCA"                                     # dec r10d
    jnz_loop_at = len(body); body += b"\x75\x00"                # jnz loop
    allow_at = len(body)
    body += _PERMIT_RESTORE                                    # pop r11; pop r10; pop rax; popfq
    body += original                                            # relocated original instruction
    jmp_resume_at = _emit_jmp(body)
    block_at = len(body)
    body += _PERMIT_RESTORE
    jmp_fail_at = _emit_jmp(body)
    body[jz_allow_at + 1] = (allow_at - (jz_allow_at + 2)) & 0xFF
    body[je_block_at + 1] = (block_at - (je_block_at + 2)) & 0xFF
    body[jnz_loop_at + 1] = (loop_at - (jnz_loop_at + 2)) & 0xFF
    _patch_jmp(body, jmp_resume_at, region, resume_addr)
    _patch_jmp(body, jmp_fail_at, region, fail_addr)
    return bytes(body)


def trampoline_count_hits(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Reference trampoline: increment dword[scratch], run ``original``, jmp back.
    Preserves rax + flags. Used by hook_poc; real birth hook will extend this to
    record which species offset was written (species attribution)."""
    body = (b"\x9C\x50"                                   # pushfq; push rax
            + b"\x48\xB8" + struct.pack("<Q", scratch)    # mov rax, scratch
            + b"\xFF\x00"                                  # inc dword [rax]
            + b"\x58\x9D"                                  # pop rax; popfq
            + original)                                    # original instruction
    return _final_jmp(body, region, resume_addr)


def make_release_gate(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Combined GATE + counter detour for the release-to-wild executor (`FUN_145D84690`
    entry `mov [rsp+0x10],rbx`). scratch+0 = u32 release count, scratch+4 = u32 LOCK flag
    (client-written: 1 = conservation program locked). At the function entry rsp is clean
    (only the return addr), so when LOCKED we abort the whole release with `xor eax,eax; ret`
    (no release, no count) — the conservation-program AP gate. When UNLOCKED we count the
    release, run the rsp-relative original, and jmp back. rax is caller-scratch at entry
    (not a param), so clobbering it is safe."""
    body = bytearray()
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x83\x78\x04\x00"                         # cmp dword [rax+4], 0   (lock flag)
    jne_at = len(body); body += b"\x75\x00"             # jne LOCKED             (patch rel8)
    body += b"\xFF\x00"                                 # inc dword [rax]        (release count)
    body += original                                    # mov [rsp+0x10],rbx     (rsp clean)
    jmp_at = _emit_jmp(body)   # jmp resume
    locked_at = len(body)
    body += b"\x31\xC0\xC3"                             # LOCKED: xor eax,eax; ret (abort)
    body[jne_at + 1] = (locked_at - (jne_at + 2)) & 0xFF
    _patch_jmp(body, jmp_at, region, resume_addr)
    return bytes(body)


def make_value_gate(region: int, scratch: int, resume_addr: int, original: bytes,
                    locked_ret: int = 0) -> bytes:
    """GATE detour for a facility getter/predicate native reached via its script-binding
    handler thunk's `jmp`. scratch+0 = u32 LOCK flag (client writes; 1 = facility gated). The
    native is hooked AT ITS ENTRY where rsp is clean (only the return addr), so when LOCKED we
    force an immediate return of ``locked_ret`` (default 0/false; e.g. capacity 0 or a predicate
    false) with `mov eax,<locked_ret>; ret`. When UNLOCKED we run the relocated entry
    ``original`` (position-independent: rsp-relative mov / sub rsp / push) and jmp back. rax is
    the return register, clobbering it is exactly the point."""
    body = bytearray()
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x83\x38\x00"                             # cmp dword [rax], 0   (lock flag)
    jne_at = len(body); body += b"\x75\x00"             # jne LOCKED           (patch rel8)
    body += original                                    # relocated entry instruction(s)
    jmp_at = _emit_jmp(body)   # jmp resume
    locked_at = len(body)
    if locked_ret == 0:
        body += b"\x31\xC0"                            # xor eax, eax
    else:
        body += b"\xB8" + struct.pack("<I", locked_ret & 0xFFFFFFFF)  # mov eax, locked_ret
    body += b"\xC3"                                    # ret
    body[jne_at + 1] = (locked_at - (jne_at + 2)) & 0xFF
    _patch_jmp(body, jmp_at, region, resume_addr)
    return bytes(body)


# Scratch layout for the facility-presence gate (research_centre / workshop / future vet, trade...):
#   [scratch + 0x00]  u32 gated manager count (client writes)
#   [scratch + 0x08]  u64[PRESENCE_GATED_MAX] gated MANAGER pointers (client writes; the
#                     component-manager whose facility's AP item has NOT been received).
# The fill write 0x149E94863 is SHARED by every facility presence cache (research centre, workshop,
# vet, trade...), distinguished only by which manager is being filled (rbp). So the gate compares
# rbp against this client-resolved set (managers are heap pointers -> re-resolved each session).
PRESENCE_GATED_MAX = 6           # 0x08 + 6*8 = 0x38, fits before code at region+0x40
PRESENCE_GATED_COUNT = 0x00
PRESENCE_GATED_MGRS = 0x08
PRESENCE_LOG_COUNT = 0x100       # u32 monotonic fire count (every fill through the hook)
PRESENCE_LOG_RBP = 0x108         # u64 last manager pointer (rbp) the fill was seen with


def make_presence_gate(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Clean reversible facility-presence gate. Hooks the SHARED cache-FILL write that marks a
    facility present — `mov byte [rcx+rax],1` @ 0x149E94863 (rcx=slot, rax=flag array loaded just
    above by `mov rax,[rbp+0x390]`, so rbp = the component MANAGER being filled). scratch = a
    client-written set of gated manager pointers (count @ +0, u64 ptrs @ +8). If rbp is in the set
    the fill stores 0 (that facility reads as ABSENT -> its button greys natively, e.g. "you need a
    research centre"); else it stores 1 (normal). Because this is the clear-then-FILL cache write,
    the game re-applies it on EVERY rebuild, so the gate holds across build/demolish (unlike
    poll-zeroing the data, which desynced the facility and only held to the next rebuild). Unlock =
    drop the manager from the set; the next fill writes 1 and the button re-enables with no manual
    restore and no desync. Per-manager so gating research_centre never touches the workshop/vet/trade.

    ``original`` = the 4-byte store + the following 8-byte `lea rax,[rsp+0x80]` (12 bytes): the store
    is replaced by the conditional store, the lea is relocated. Preserves FLAGS + rdx + r8; rcx/rax/rbp
    are live and untouched. Stack is balanced before the relocated rsp-relative lea."""
    lea = original[4:]                                  # relocated instruction(s) after the store
    body = bytearray()
    body += b"\x9C"                                    # pushfq
    body += b"\x52"                                    # push rdx
    body += b"\x41\x50"                                # push r8
    body += b"\x48\xBA" + struct.pack("<Q", scratch)    # movabs rdx, scratch
    body += b"\xFF\x82" + struct.pack("<i", PRESENCE_LOG_COUNT)  # inc dword [rdx+0x100] (fire count)
    body += b"\x48\x89\xAA" + struct.pack("<i", PRESENCE_LOG_RBP)  # mov [rdx+0x108], rbp (last mgr)
    body += b"\x44\x8B\x02"                            # mov r8d, [rdx]      (gated count)
    body += b"\x45\x85\xC0"                            # test r8d, r8d
    jz_unlocked = len(body); body += b"\x74\x00"       # jz UNLOCKED (none gated -> store 1)
    body += b"\x48\x83\xC2\x08"                        # add rdx, 8          (gated mgr list)
    loop_at = len(body)
    body += b"\x48\x3B\x2A"                            # cmp rbp, [rdx]      (this manager gated?)
    je_locked = len(body); body += b"\x74\x00"         # je LOCKED
    body += b"\x48\x83\xC2\x08"                        # add rdx, 8
    body += b"\x41\xFF\xC8"                            # dec r8d
    jnz_loop = len(body); body += b"\x75\x00"          # jnz loop
    unlocked_at = len(body)
    body += b"\x41\x58"                                # pop r8
    body += b"\x5A"                                    # pop rdx
    body += b"\xC6\x04\x01\x01"                        # mov byte [rcx+rax], 1  (present)
    jmp_after_at = len(body); body += b"\xEB\x00"      # jmp AFTER
    locked_at = len(body)
    body += b"\x41\x58"                                # pop r8
    body += b"\x5A"                                    # pop rdx
    body += b"\xC6\x04\x01\x00"                        # mov byte [rcx+rax], 0  (absent)
    after_at = len(body)
    body += b"\x9D"                                    # AFTER: popfq
    body += lea                                         # relocated lea rax,[rsp+0x80]
    jmp_res = _emit_jmp(body)
    body[jz_unlocked + 1] = (unlocked_at - (jz_unlocked + 2)) & 0xFF
    body[je_locked + 1] = (locked_at - (je_locked + 2)) & 0xFF
    body[jnz_loop + 1] = (loop_at - (jnz_loop + 2)) & 0xFF
    body[jmp_after_at + 1] = (after_at - (jmp_after_at + 2)) & 0xFF
    _patch_jmp(body, jmp_res, region, resume_addr)
    return bytes(body)


# Scratch layout for the research-completion gate (research_centre/workshop facility items):
#   [scratch + 0x00]  u32 gated-category count (client writes)
#   [scratch + 0x08]  u8[...] gated category bytes (client writes; 7 = animal/Research Centre,
#                     3 = mechanic/Workshop). A research record's category is at record+0x3C.
RESEARCH_GATE_COUNT = 0x00
RESEARCH_GATE_CATS = 0x08
RESEARCH_CAT_OFF = 0x3C   # record+0x3C = category byte (read via [r14+0x3C] at the hook)


def make_research_gate(region: int, scratch: int, resume_addr: int, original: bytes,
                       cat_modrm: int = 0x4E) -> bytes:
    """Category-selective skip-the-status-write gate for research. Two uses:
      * COMPLETION block: `mov byte [r14+0x49],3` @ 0x140E48F82 (record in r14, cat_modrm 0x4E).
      * START block:      `mov byte [r15+0x49],2` @ 0x140E461C6 (record in r15, cat_modrm 0x4F)
        — preferred: research never enters "Researching", so no bar/level/completion at all.
    Reads the record's category ([reg+0x3C] via ``cat_modrm``); if it's in the client-written
    gated set (scratch), SKIPS the status write (research never advances to that status) and
    auto-resumes normal behavior once the facility item arrives; else performs the original
    write. Preserves FLAGS (callers' following branches may depend on a prior cmp) and rax/rcx/
    rdx; leaves the record register / rbp untouched. ``cat_modrm`` is the modrm byte for
    ``movzx ecx, byte [<record_reg>+0x3C]`` (0x4E = r14, 0x4F = r15; both with REX.B)."""
    body = bytearray()
    body += b"\x9C\x50\x51\x52"                        # pushfq; push rax; push rcx; push rdx
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x41\x0F\xB6" + bytes([cat_modrm, RESEARCH_CAT_OFF])  # movzx ecx, byte [<reg>+0x3C]
    body += b"\x8B\x10"                                 # mov edx, [rax]      (gated count)
    body += b"\x85\xD2"                                 # test edx, edx
    jz_do = len(body); body += b"\x74\x00"              # jz DO_WRITE
    body += b"\x48\x8D\x40" + bytes([RESEARCH_GATE_CATS])    # lea rax, [rax+8]   (gated cat list)
    loop = len(body)
    body += b"\x3A\x08"                                 # cmp cl, [rax]
    je_skip = len(body); body += b"\x74\x00"            # je SKIP (category gated -> no write)
    body += b"\x48\xFF\xC0"                             # inc rax
    body += b"\xFF\xCA"                                 # dec edx
    jnz_loop = len(body); body += b"\x75\x00"           # jnz loop
    do_at = len(body)
    body += b"\x5A\x59\x58\x9D"                         # pop rdx; pop rcx; pop rax; popfq
    body += original                                    # mov byte [r14+0x49],3 (relocated)
    jmp_do = _emit_jmp(body)
    skip_at = len(body)
    body += b"\x5A\x59\x58\x9D"                         # pop rdx; pop rcx; pop rax; popfq
    jmp_skip = _emit_jmp(body)
    body[jz_do + 1] = (do_at - (jz_do + 2)) & 0xFF
    body[je_skip + 1] = (skip_at - (je_skip + 2)) & 0xFF
    body[jnz_loop + 1] = (loop - (jnz_loop + 2)) & 0xFF
    _patch_jmp(body, jmp_do, region, resume_addr)
    _patch_jmp(body, jmp_skip, region, resume_addr)
    return bytes(body)


def make_research_progress_gate(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Category-selective PROGRESS gate for research. Hooks the progress-store in the research
    tick (`movss [r14+0x20],xmm0` @ 0x140E48E93, 6 bytes), where r14 = the research record and
    xmm0 = the just-accumulated progress. For a gated category ([r14+0x3C] in the client set) it
    zeros xmm0 BEFORE the store, so (a) progress [r14+0x20] is written 0 and (b) the very next
    `comiss xmm0,threshold` fails -> the completion block is skipped. Net effect: a gated
    research's bar never fills and it never completes (no level-up, reward, or AP check), held at
    the GAME's own frame rate so there's no flicker/race. Runs unconditionally for non-gated
    (normal store). Used at BOTH stores in the tick — the accumulated progress [r14+0x20]
    (0x140E48E93) and the displayed bar [r14+0x1c] (0x140E48EE0) — so progress is blocked AND the
    bar reads empty. Preserves FLAGS (pushfq/popfq — the display-store site has a later jp/jne
    depending on a prior ucomiss) and rax (= threshold ptr)/rcx/rdx. Same scratch layout as the
    completion gate: [+0x00] u32 gated-cat count, [+0x08] gated category bytes."""
    body = bytearray()
    body += b"\x9C\x50\x51\x52"                        # pushfq; push rax; push rcx; push rdx
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x41\x0F\xB6\x4E" + bytes([RESEARCH_CAT_OFF])  # movzx ecx, byte [r14+0x3C] (category)
    body += b"\x8B\x10"                                 # mov edx, [rax]   (gated count)
    body += b"\x85\xD2"                                 # test edx, edx
    jz_done = len(body); body += b"\x74\x00"            # jz DONE (none gated -> normal store)
    body += b"\x48\x83\xC0" + bytes([RESEARCH_GATE_CATS])   # add rax, 8   (gated cat list)
    loop = len(body)
    body += b"\x3A\x08"                                 # cmp cl, [rax]
    je_zero = len(body); body += b"\x74\x00"            # je ZERO (category gated)
    body += b"\x48\xFF\xC0"                             # inc rax
    body += b"\xFF\xCA"                                 # dec edx
    jnz_loop = len(body); body += b"\x75\x00"           # jnz loop
    jmp_done = len(body); body += b"\xEB\x00"           # jmp DONE (no match)
    zero_at = len(body)
    body += b"\x0F\x57\xC0"                             # ZERO: xorps xmm0, xmm0
    done_at = len(body)
    body += b"\x5A\x59\x58\x9D"                         # DONE: pop rdx; pop rcx; pop rax; popfq
    body += original                                    # movss [r14+<off>], xmm0 (relocated)
    jmp_res = _emit_jmp(body)
    body[jz_done + 1] = (done_at - (jz_done + 2)) & 0xFF
    body[je_zero + 1] = (zero_at - (je_zero + 2)) & 0xFF
    body[jnz_loop + 1] = (loop - (jnz_loop + 2)) & 0xFF
    body[jmp_done + 1] = (done_at - (jmp_done + 2)) & 0xFF
    _patch_jmp(body, jmp_res, region, resume_addr)
    return bytes(body)


def make_permit_capture_trampoline(region: int, scratch: int, resume_addr: int,
                                   fail_addr: int) -> bytes:
    """Species-handle CAPTURE detour for the Animal-Exchange buy site (0x14A0894E5).
    Logs the listing's species handle [rbx+0x10] + listing id [rbx+0x228] to scratch
    (+0x100/+0x104/+0x108), then ALWAYS jmps the fail-return (0x14A0894B7) so the
    purchase aborts with NO spend. Lets the user click 'buy' on each species to record
    its handle for free. resume_addr unused (we never resume). Saves rax/r10/r11+flags."""
    body = bytearray()
    body += _PERMIT_SAVE                               # pushfq; push rax; push r10; push r11
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x44\x8B\x53" + bytes([PERMIT_LISTING_SPECIES_OFF])      # mov r10d,[rbx+0x10]
    body += b"\x44\x89\x90" + struct.pack("<i", PERMIT_LOG_HANDLE)     # mov [rax+0x100],r10d
    body += b"\x44\x8B\x9B" + struct.pack("<i", PERMIT_LISTING_ID_OFF) # mov r11d,[rbx+0x228]
    body += b"\x44\x89\x98" + struct.pack("<i", PERMIT_LOG_ID)         # mov [rax+0x104],r11d
    body += b"\xFF\x80" + struct.pack("<i", PERMIT_LOG_COUNT)          # inc dword [rax+0x108]
    body += _PERMIT_RESTORE                            # pop r11; pop r10; pop rax; popfq
    jmp_at = _emit_jmp(body)   # jmp fail (always abort, no spend)
    _patch_jmp(body, jmp_at, region, fail_addr)
    return bytes(body)


def make_capture_rcx(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Capture detour: record rcx (a fn's 1st arg) + a fire-count at scratch, run the
    rsp-relative ``original`` first (rsp intact), then jmp back. scratch+0 = u32 fire
    count, scratch+8 = u64 captured rcx. Used to grab a manager pointer passed into a
    native fn (e.g. research_system into FUN_140E456A0) for live structure RE."""
    body = bytearray()
    body += original                                   # mov [rsp+0x10],rbx (rsp intact)
    body += b"\x50"                                    # push rax
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\x48\x89\x48\x08"                        # mov [rax+8], rcx   (captured arg)
    body += b"\xFF\x00"                                # inc dword [rax]    (fire count)
    body += b"\x58"                                    # pop rax
    jmp_at = _emit_jmp(body)
    _patch_jmp(body, jmp_at, region, resume_addr)
    return bytes(body)


def make_release_counter(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Counting detour for the release-to-wild executor (`ReleaseAnimalIntoWild` native
    fn @ 0x145D84690). Fires once per release (release-specific script action -> no
    sell-vs-release disambiguation, no species attribution needed - conservation_release
    is a cumulative total, threshold 1). The client counts increments at scratch+0.

    The hooked entry instr `mov [rsp+0x10],rbx` is RSP-RELATIVE, so it must run with
    the original rsp (a jmp detour doesn't push a return addr, so rsp at the trampoline
    == rsp at the site). Hence: run ``original`` FIRST (before any push), then bump the
    counter using rax+flags (push/pop), then jmp back."""
    body = bytearray()
    body += original                                   # mov [rsp+0x10],rbx (rsp intact)
    body += b"\x9C\x50"                                 # pushfq; push rax
    body += b"\x48\xB8" + struct.pack("<Q", scratch)    # movabs rax, scratch
    body += b"\xFF\x00"                                 # inc dword [rax]   (release count)
    body += b"\x58\x9D"                                 # pop rax; popfq
    jmp_at = _emit_jmp(body)
    _patch_jmp(body, jmp_at, region, resume_addr)
    return bytes(body)


# Scratch layout written by the birth trampoline (within the 0x40 scratch block):
#   [scratch + 0x00]  u32  monotonic birth count (the poll cursor)
#   [scratch + 0x08]  u16[BIRTH_RING] ring of recent species indices (r14w)
# The Nth birth (1-based) stores its species at ring[(N-1) & (BIRTH_RING-1)].
BIRTH_RING = 16          # power of two so the mask below is (RING-1)
BIRTH_RING_OFF = 0x08


def make_birth_trampoline(scratch: int, resume_addr: int, give_birth_target: int) -> bytes:
    """Trampoline for the Planet Zoo birth hook (site = the give-birth `call`).

    The hooked instruction is a **relative** ``call rel32`` (``E8 ...``), so its
    bytes can't be relocated by copying — the trampoline instead re-issues the
    call to the same absolute ``give_birth_target`` (computed from the original
    rel32 at install time). Sequence:

      1. record the species index (``r14w``, live at the hook) into the ring and
         bump the birth counter — using only rax/r11 (push/pop) + flags, so the
         give-birth argument registers (rcx/rdx/r8/r9) and stack are untouched;
      2. ``call give_birth_target`` — real give-birth runs with the original
         arguments and stack alignment (rsp is identical to the original site);
      3. ``jmp resume_addr`` — continue at the instruction after the original call.

    The client polls ``u32[scratch]``; on an increase of N it reads the last N
    species from the ring at ``scratch + BIRTH_RING_OFF`` (see ``read_birth_events``).
    """
    body = (
        b"\x9C"                                       # pushfq
        b"\x50"                                       # push rax
        b"\x41\x53"                                   # push r11
        + b"\x48\xB8" + struct.pack("<Q", scratch)    # mov  rax, scratch
        + b"\x44\x8B\x18"                             # mov  r11d, [rax]        ; count
        + b"\x41\xFF\xC3"                             # inc  r11d
        + b"\x44\x89\x18"                             # mov  [rax], r11d   -> store count+1
        + b"\x41\xFF\xCB"                             # dec  r11d               ; old count = index
        + b"\x41\x83\xE3" + bytes([BIRTH_RING - 1])   # and  r11d, RING-1
        + b"\x41\xD1\xE3"                             # shl  r11d, 1            ; *2 (u16 stride)
        + b"\x66\x46\x89\x74\x18" + bytes([BIRTH_RING_OFF])  # mov [rax+r11+off], r14w
        + b"\x41\x5B"                                 # pop  r11
        + b"\x58"                                     # pop  rax
        + b"\x9D"                                     # popfq
        + b"\x48\xB8" + struct.pack("<Q", give_birth_target)  # mov rax, give_birth
        + b"\xFF\xD0"                                 # call rax
        + b"\x48\xB8" + struct.pack("<Q", resume_addr)        # mov rax, resume
        + b"\xFF\xE0"                                 # jmp  rax
    )
    return body


# Scratch layout for the add-animal INSERT instrument:
#   [scratch + 0x00]    u32  monotonic insert count (poll cursor)
#   [scratch + RING_OFF] record[INSERT_RING], each INSERT_REC (0x40) bytes — 8 qwords:
#       handle(rsi), container(rbx), [rbp+0xd8], [rbp+0xe0], [rbp+0xf8], rbp, r13, r14
# The [rbp+...] slots / regs are candidate ANIMAL-OBJECT pointers so the client can
# read a newborn/age field to tell a BIRTH from a market BUY (path-independent).
INSERT_RING = 8
INSERT_REC = 0x40
INSERT_RING_OFF = 0x200
INSERT_FIELDS = ["handle", "container", "rbp+0xd8", "rbp+0xe0", "rbp+0xf8", "rbp", "r13", "r14"]


def make_insert_instrument(region: int, scratch: int, resume_addr: int, original: bytes) -> bytes:
    """Trampoline for the add-animal insert rejoin (Planet Zoo `0x140C82168`).

    At the hook the count is already incremented; `rsi` = inserted entity handle,
    `rbx` = per-species container (count at rbx+0x10, species-id at rbx+8). Records
    the handle, container, and candidate animal-object pointers ([rbp+0xd8] etc.,
    rbp/r13/r14) into a ring + bumps a counter. Uses rax/rdx/r8 + flags (push/pop).
    Fires on BOTH the in-place and grow/realloc insert paths (they converge here),
    keyed on a STABLE code address (immune to count data-address volatility)."""
    body = (
        b"\x9C"                                          # pushfq
        b"\x50"                                          # push rax
        b"\x52"                                          # push rdx
        b"\x41\x50"                                      # push r8
        + b"\x48\xB8" + struct.pack("<Q", scratch)       # movabs rax, scratch
        + b"\x8B\x10"                                    # mov  edx, [rax]      ; count
        + b"\xFF\xC2"                                    # inc  edx
        + b"\x89\x10"                                    # mov  [rax], edx      ; count++
        + b"\xFF\xCA"                                    # dec  edx             ; old = index
        + b"\x83\xE2" + bytes([INSERT_RING - 1])         # and  edx, RING-1
        + b"\x69\xD2" + struct.pack("<I", INSERT_REC)    # imul edx, edx, REC
        + b"\x48\x05" + struct.pack("<I", INSERT_RING_OFF)  # add rax, RING_OFF
        + b"\x48\x8D\x04\x10"                            # lea  rax, [rax+rdx]  ; rax=slot
        + b"\x48\x89\x30"                                # mov  [rax], rsi       ; +0x00 handle
        + b"\x48\x89\x58\x08"                            # mov  [rax+8], rbx     ; +0x08 container
        + b"\x4C\x8B\x85\xD8\x00\x00\x00"                # mov  r8, [rbp+0xd8]
        + b"\x4C\x89\x40\x10"                            # mov  [rax+0x10], r8
        + b"\x4C\x8B\x85\xE0\x00\x00\x00"                # mov  r8, [rbp+0xe0]
        + b"\x4C\x89\x40\x18"                            # mov  [rax+0x18], r8
        + b"\x4C\x8B\x85\xF8\x00\x00\x00"                # mov  r8, [rbp+0xf8]
        + b"\x4C\x89\x40\x20"                            # mov  [rax+0x20], r8
        + b"\x48\x89\x68\x28"                            # mov  [rax+0x28], rbp
        + b"\x4C\x89\x68\x30"                            # mov  [rax+0x30], r13
        + b"\x4C\x89\x70\x38"                            # mov  [rax+0x38], r14
        + b"\x41\x58"                                    # pop  r8
        + b"\x5A"                                        # pop  rdx
        + b"\x58"                                        # pop  rax
        + b"\x9D"                                        # popfq
        + original                                       # relocated original instruction
    )
    return _final_jmp(body, region, resume_addr)


def read_insert_events(scanner, scratch: int, cursor: int) -> "tuple[int, List[dict]]":
    """Drain records (INSERT_FIELDS) written by make_insert_instrument."""
    count = struct.unpack("<I", scanner.read_bytes(scratch, 4))[0]
    if count <= cursor:
        return count, []
    ring = scanner.read_bytes(scratch + INSERT_RING_OFF, INSERT_RING * INSERT_REC)
    first = max(cursor + 1, count - INSERT_RING + 1)
    out = []
    for n in range(first, count + 1):
        idx = (n - 1) & (INSERT_RING - 1)
        vals = struct.unpack_from("<8Q", ring, idx * INSERT_REC)
        out.append(dict(zip(INSERT_FIELDS, vals)))
    return count, out


def read_birth_events(scanner, scratch: int, cursor: int) -> "tuple[int, List[int]]":
    """Drain new birth events recorded by ``make_birth_trampoline``.

    Returns ``(new_cursor, species_indices)`` — the species index (r14w value)
    for each birth since ``cursor``. Caps the drain at ``BIRTH_RING`` (ring size);
    if more than that piled up between polls, older entries were overwritten and
    are reported lost. Map each index to a species_key via the per-species table.
    """
    count = struct.unpack("<I", scanner.read_bytes(scratch, 4))[0]
    if count <= cursor:
        return count, []
    ring = scanner.read_bytes(scratch + BIRTH_RING_OFF, BIRTH_RING * 2)
    first = max(cursor + 1, count - BIRTH_RING + 1)  # oldest still-present birth
    out = []
    for n in range(first, count + 1):
        idx = (n - 1) & (BIRTH_RING - 1)
        out.append(struct.unpack_from("<H", ring, idx * 2)[0])
    return count, out
