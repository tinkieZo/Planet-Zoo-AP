"""release_species_probe - DIAGNOSTIC: find the animal-manager reachable from the release frame and
confirm the released entity is still resolvable at poll time.

*(rbp+0x48)* turned out to be a relocate sub-object (neither zoo nor animal-roster manager). So this
probe captures rbp (the relocate manager / arg1) + the released handle (rsi), then BRUTE-FORCE scans
pointers in the release frame - rbp itself, every qword in rbp[0..WINDOW], *(rbp+0x48) and its
qwords - trying each as an animal MANAGER (resolve_entity_via_manager) and as a ZOO (resolve_entity).
A hit means: (a) that pointer is the manager/zoo, AND (b) the entity is still in the roster (the
hashmap genuinely contained the handle - random pointers don't have a power-of-two map holding exactly
this key). The hit's offset tells us the stable source to wire into the client.

    python -m tools.release_species_probe [seconds=120]

Run in the loaded AP zoo, then RELEASE one animal of a KNOWN species. Read the FOUND line(s).
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pz_ap_client.memory.scanner import MemoryScanner          # noqa: E402
from pz_ap_client.memory.hook import HookManager, _final_jmp   # noqa: E402
from pz_ap_client.memory.animals import AnimalResolver         # noqa: E402
from pz_ap_client.memory.research import ResearchReader        # noqa: E402
from pz_ap_client.memory.registry import RegistryResolver      # noqa: E402

SITE = 0x145D8478F
ORIG = bytes.fromhex("488b4d484889f2")   # mov rcx,[rbp+0x48] ; mov rdx,rsi
CAP_COUNT, CAP_HANDLE, CAP_RBP = 0x00, 0x08, 0x18
WINDOW = 0x400                            # bytes of each frame object to scan (qword stride)
HEAP_LO, HEAP_HI = 0x10000, (1 << 47)


def make_diag_capture(region: int, scratch: int, resume: int, original: bytes) -> bytes:
    """Capture count(+0), rsi=handle(+8), rbp(+0x18); run original; jmp back. rax saved/restored;
    only stores to our scratch + the rsp-clean original -> can't fault."""
    body = bytearray()
    body += b"\x50"                                   # push rax
    body += b"\x48\xB8" + struct.pack("<Q", scratch)   # movabs rax, scratch
    body += b"\xFF\x00"                               # inc dword [rax]        (count)
    body += b"\x48\x89\x70\x08"                       # mov [rax+8], rsi       (handle)
    body += b"\x48\x89\x68\x18"                       # mov [rax+0x18], rbp    (relocate mgr / arg1)
    body += b"\x58"                                   # pop rax
    body += original                                  # mov rcx,[rbp+0x48] ; mov rdx,rsi
    return _final_jmp(body, region, resume)


def _qword(s, addr):
    try:
        return int.from_bytes(s.read_bytes(addr, 8), "little")
    except Exception:
        return 0


def _candidate_ptrs(s, rbp):
    """rbp, *(rbp+0x48), and every heap-looking qword inside rbp[0..WINDOW] and *(rbp+0x48)[0..WINDOW]."""
    seen, out = set(), []
    def add(p, src):
        if HEAP_LO < p < HEAP_HI and p not in seen:
            seen.add(p); out.append((p, src))
    add(rbp, "rbp")
    sub = _qword(s, rbp + 0x48)
    add(sub, "*(rbp+0x48)")
    for base, tag in ((rbp, "rbp"), (sub, "*(rbp+0x48)")):
        if not (HEAP_LO < base < HEAP_HI):
            continue
        for off in range(0, WINDOW, 8):
            add(_qword(s, base + off), "%s+0x%X" % (tag, off))
    return out


def _diagnose(s, res, rr, reg, cnt, handle, rbp):
    print("\n*** release #%d - handle 0x%X - rbp=0x%X ***" % (cnt, handle, rbp))
    h2k = {}
    try:
        h2k = rr.handle_key_map() or {}
    except Exception:
        pass
    hits = 0
    for ptr, src in _candidate_ptrs(s, rbp):
        for label, ent in (("mgr", res.resolve_entity_via_manager(ptr, handle)),
                           ("zoo", res.resolve_entity(ptr, handle))):
            if ent is None:
                continue
            sh = res.species_handle(ent)
            key = h2k.get(sh)
            name = reg.id_to_name(sh) if sh else None
            print("  FOUND %s as %s -> 0x%X  entity 0x%X  species 0x%X  key=%r name=%r"
                  % (src, label, ptr, ent, sh or 0, key, name))
            hits += 1
    if not hits:
        print("  no candidate in the release frame resolved the handle "
              "(entity already freed? manager is a global, not frame-reachable?)")
    print("  research handle_key_map: %d entries" % len(h2k))


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    s = MemoryScanner("PlanetZoo.exe")
    if not s.attach():
        print("FAIL: not attached")
        return 1
    reg = RegistryResolver(s)
    reg.build_name_map()
    rr = ResearchReader(s, registry=reg)
    res = AnimalResolver(s)
    if s.read_bytes(SITE, len(ORIG)) != ORIG:
        print("FAIL: unexpected bytes at 0x%X - RVA drift?" % SITE)
        return 1
    hm = HookManager(s)
    if not hm.install("relsp", SITE, ORIG, lambda r, sc, rs: make_diag_capture(r, sc, rs, ORIG)):
        print("FAIL: could not install capture detour")
        return 1
    scratch = hm.scratch("relsp")
    print("diag probe @0x%X; RELEASE one animal of a KNOWN species now (%ds)..." % (SITE, secs))
    last = 0
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            cnt = int.from_bytes(s.read_bytes(scratch + CAP_COUNT, 4), "little")
            if cnt != last:
                last = cnt
                handle = _qword(s, scratch + CAP_HANDLE)
                rbp = _qword(s, scratch + CAP_RBP)
                _diagnose(s, res, rr, reg, cnt, handle, rbp)
            time.sleep(0.02)
    finally:
        hm.restore("relsp")
        print("\nprobe detour restored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
