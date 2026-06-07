"""luaparse — structural parser for Frontier's custom Lua 5.3 chunks (8-byte instructions).

Stock unluac/luadec can't read these (custom header + 8-byte Instruction). We DON'T decode opcodes;
we parse the prototype tree and dump, per function: source/line-range/params, the constant pool
(strings + numbers), and debug local/upvalue names. The constants reveal which field keys + global
function names each function references — enough to see what e.g. IsWaterEditDisabled actually reads.

Custom header (verified against TerrainEditUIMode source string landing at 0x1e):
  sig "\x1bLua" 53, format 02, LUAC_DATA, sizes int=4 size_t=4 Instr=8 Number=4(f32), lua_Integer=8,
  LUAC_INT=0x5678 (8 bytes), LUAC_NUM=370.5 (f32, 4 bytes). header_len = 0x1c.

    python -m tools.luaparse <file.lua.bin> [keyword ...]
With keywords, only prints functions whose constant pool contains any keyword (case-insensitive).
"""
from __future__ import annotations
import struct
import sys

INT = 4
SIZE_T = 4
INSTR = 4  # standard 4-byte 5.3 instructions (header's 0x08 byte is lua_Integer, not Instruction)
INTEGER = 8
NUMBER = 4  # float32
HEADER_LEN = 0x1C


class R:
    def __init__(self, b):
        self.b = b
        self.p = 0

    def u8(self):
        v = self.b[self.p]; self.p += 1; return v

    def i(self):
        v = struct.unpack_from("<i", self.b, self.p)[0]; self.p += INT; return v

    def sz(self):
        v = struct.unpack_from("<I", self.b, self.p)[0]; self.p += SIZE_T; return v

    def integer(self):
        v = struct.unpack_from("<q", self.b, self.p)[0]; self.p += INTEGER; return v

    def number(self):
        v = struct.unpack_from("<f", self.b, self.p)[0]; self.p += NUMBER; return v

    def string(self):
        size = self.u8()
        if size == 0xFF:
            size = self.sz()
        if size == 0:
            return None
        ln = size - 1
        s = self.b[self.p:self.p + ln]; self.p += ln
        return s.decode("latin-1", "replace")


def _read_constants(r, path):
    """Read the constant pool; return (strings, numbers, full_kpool)."""
    strs, nums, kpool = [], [], []
    for _ in range(r.i()):
        t = r.u8()
        if t == 0:            # NIL
            kpool.append(None)
        elif t == 1:          # BOOLEAN
            v = bool(r.u8()); nums.append(v); kpool.append(v)
        elif t == 3:          # NUMFLT
            v = r.number(); nums.append(v); kpool.append(v)
        elif t == 0x13:       # NUMINT
            v = r.integer(); nums.append(v); kpool.append(v)
        elif t in (4, 0x14):  # SHRSTR / LNGSTR
            s = r.string(); kpool.append(s)
            if s is not None:
                strs.append(s)
        else:
            raise ValueError("bad const type 0x%X at %d (path=%s)" % (t, r.p, path))
    return strs, nums, kpool


def _read_locals(r):
    """Debug locvars section: return the list of local-variable names (start/end pc skipped)."""
    locs = []
    for _ in range(r.i()):
        nm = r.string(); r.i(); r.i()
        if nm:
            locs.append(nm)
    return locs


def _read_upnames(r):
    """Debug upvalue-names section: return the list of upvalue names."""
    upn = []
    for _ in range(r.i()):
        nm = r.string()
        if nm:
            upn.append(nm)
    return upn


def read_function(r, protos_out, path="main"):
    """Parse one Lua function prototype (recursively, depth-first) and append it to protos_out."""
    fn = {"path": path}
    fn["source"] = r.string()
    fn["linedefined"] = r.i()
    fn["lastline"] = r.i()
    fn["numparams"] = r.u8()
    fn["is_vararg"] = r.u8()
    fn["maxstack"] = r.u8()
    # code
    sizecode = r.i()
    fn["code"] = r.b[r.p:r.p + sizecode * INSTR]
    r.p += sizecode * INSTR
    fn["ninstr"] = sizecode
    # constants
    fn["strings"], fn["nums"], fn["kpool"] = _read_constants(r, path)
    # upvalues (instack, idx) — names come from the debug section below
    nup = r.i()
    for _ in range(nup):
        r.u8(); r.u8()
    fn["nup"] = nup
    # protos (recurse; each registers itself in protos_out)
    for j in range(r.i()):
        read_function(r, protos_out, "%s.%d" % (path, j))
    # debug: lineinfo (skipped) — read the count (advances p) THEN skip; do NOT fold into one
    # `r.p += r.i() * INT`, whose augmented-assignment evaluation order would discard the count read.
    sizeline = r.i()
    r.p += sizeline * INT
    # debug: locvars + upvalue names
    fn["locals"] = _read_locals(r)
    fn["upnames"] = _read_upnames(r)
    protos_out.append(fn)
    return fn


OPS = [
    ("MOVE", "ABC"), ("LOADK", "ABx"), ("LOADKX", "ABC"), ("LOADBOOL", "ABC"), ("LOADNIL", "ABC"),
    ("GETUPVAL", "ABC"), ("GETTABUP", "ABC"), ("GETTABLE", "ABC"), ("SETTABUP", "ABC"), ("SETUPVAL", "ABC"),
    ("SETTABLE", "ABC"), ("NEWTABLE", "ABC"), ("SELF", "ABC"), ("ADD", "ABC"), ("SUB", "ABC"),
    ("MUL", "ABC"), ("MOD", "ABC"), ("POW", "ABC"), ("DIV", "ABC"), ("IDIV", "ABC"), ("BAND", "ABC"),
    ("BOR", "ABC"), ("BXOR", "ABC"), ("SHL", "ABC"), ("SHR", "ABC"), ("UNM", "ABC"), ("BNOT", "ABC"),
    ("NOT", "ABC"), ("LEN", "ABC"), ("CONCAT", "ABC"), ("JMP", "AsBx"), ("EQ", "ABC"), ("LT", "ABC"),
    ("LE", "ABC"), ("TEST", "ABC"), ("TESTSET", "ABC"), ("CALL", "ABC"), ("TAILCALL", "ABC"),
    ("RETURN", "ABC"), ("FORLOOP", "AsBx"), ("FORPREP", "AsBx"), ("TFORCALL", "ABC"), ("TFORLOOP", "AsBx"),
    ("SETLIST", "ABC"), ("CLOSURE", "ABx"), ("VARARG", "ABC"), ("EXTRAARG", "Ax"),
]
# opcodes whose B and/or C operand is RK (register-or-constant)
RKB = {"SETTABUP", "SETTABLE", "ADD", "SUB", "MUL", "MOD", "POW", "DIV", "IDIV", "BAND", "BOR",
       "BXOR", "SHL", "SHR", "EQ", "LT", "LE", "CONCAT", "SELF"}
RKC = {"GETTABUP", "GETTABLE", "SETTABUP", "SETTABLE", "ADD", "SUB", "MUL", "MOD", "POW", "DIV",
       "IDIV", "BAND", "BOR", "BXOR", "SHL", "SHR", "EQ", "LT", "LE", "SELF"}


def _rk(x, allk):
    """Render an RK operand: R<n> for a register, or K<i>(value) for a constant (bit 0x100 set)."""
    if x & 0x100:
        i = x & 0xFF
        return "K%d(%r)" % (i, allk[i] if i < len(allk) else "?")
    return "R%d" % x


def _upname(fn, b):
    ups = fn["upnames"]
    return ups[b] if b < len(ups) else "?"


def _fmt_abc(name, a, ins, fn, allk):
    b = (ins >> 23) & 0x1FF
    c = (ins >> 14) & 0x1FF
    bs = _rk(b, allk) if name in RKB else "R%d" % b
    cs = _rk(c, allk) if name in RKC else "R%d" % c
    txt = "%-9s A=%d B=%s C=%s" % (name, a, bs, cs)
    if name in ("GETUPVAL", "SETUPVAL"):
        txt += "   ; upval[%d]=%s" % (b, _upname(fn, b))
    if name in ("GETTABUP", "SETTABUP"):
        txt = txt.replace("B=R%d" % b, "B=up[%d]=%s" % (b, _upname(fn, b)))
    return txt


def _fmt_abx(name, a, ins, allk):
    bx = (ins >> 14) & 0x3FFFF
    if name == "LOADK":
        return "%-9s A=%d K%d(%r)" % (name, a, bx, allk[bx] if bx < len(allk) else "?")
    return "%-9s A=%d Bx=%d" % (name, a, bx)


def _fmt_insn(ins, pc, fn, allk):
    """Format one 4-byte instruction's operands per its encoding mode."""
    op = ins & 0x3F
    name, mode = OPS[op] if op < len(OPS) else ("OP%d" % op, "ABC")
    a = (ins >> 6) & 0xFF
    if mode == "ABC":
        return _fmt_abc(name, a, ins, fn, allk)
    if mode == "ABx":
        return _fmt_abx(name, a, ins, allk)
    if mode == "AsBx":
        sbx = ((ins >> 14) & 0x3FFFF) - 0x1FFFF
        return "%-9s A=%d sBx=%d (-> %d)" % (name, a, sbx, pc + 1 + sbx)
    return "%-9s Ax=%d" % (name, (ins >> 6) & 0x3FFFFFF)


def disasm(fn):
    # Note: the original (strings, nums) constant order is lost, so K-indices show a best-guess value.
    allk = fn.get("kpool", [])
    code = fn["code"]
    out = []
    for pc in range(len(code) // 4):
        ins = struct.unpack_from("<I", code, pc * 4)[0]
        out.append("  %3d: %s" % (pc, _fmt_insn(ins, pc, fn, allk)))
    return "\n".join(out)


def _parse_chunk(path):
    """Read a .lua.bin file, skip the custom header + main-proto upvalue byte, parse all protos."""
    with open(path, "rb") as f:
        data = f.read()
    r = R(data)
    r.p = HEADER_LEN
    r.u8()  # main proto upvalue count byte
    protos = []
    read_function(r, protos, "main")
    return protos


def _run_disasm(path, want) -> int:
    for fn in _parse_chunk(path):
        if fn["path"] == want:
            print("=== disasm %s  lines %d-%d  params=%d ===" % (
                fn["path"], fn["linedefined"], fn["lastline"], fn["numparams"]))
            print("kpool: %s" % ", ".join("%d:%r" % (i, k) for i, k in enumerate(fn["kpool"])))
            print(disasm(fn))
            return 0
    print("function path %r not found" % want)
    return 1


def _matches(fn, kws) -> bool:
    """True if no keyword filter, or any keyword is a substring of any of fn's constant strings."""
    return not kws or any(any(k in s.lower() for s in fn["strings"]) for k in kws)


def _print_fn(fn) -> None:
    print("=== fn %s  lines %d-%d  params=%d vararg=%d ninstr=%d ===" % (
        fn["path"], fn["linedefined"], fn["lastline"], fn["numparams"], fn["is_vararg"], fn["ninstr"]))
    if fn["locals"]:
        print("   locals: %s" % ", ".join(fn["locals"][:24]))
    if fn["upnames"]:
        print("   upvals: %s" % ", ".join(fn["upnames"][:16]))
    if fn["strings"]:
        print("   strings: %s" % " | ".join(fn["strings"][:60]))
    if fn["nums"]:
        print("   nums: %s" % ", ".join(str(n) for n in fn["nums"][:24]))


def _dump_chunk(path, kws) -> int:
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x1bLua" or data[4] != 0x53:
        print("not a Lua 5.3 chunk")
        return 1
    r = R(data)
    r.p = HEADER_LEN
    nup_main = r.u8()  # main proto upvalue count byte
    protos = []
    try:
        read_function(r, protos, "main")
    except Exception as e:
        print("PARSE ERROR: %s (consumed %d/%d bytes, %d protos ok)" % (e, r.p, len(data), len(protos)))
    print("parsed %d functions; main nup=%d; consumed %d/%d bytes\n" % (len(protos), nup_main, r.p, len(data)))
    for fn in protos:
        if _matches(fn, kws):
            _print_fn(fn)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m tools.luaparse <file.lua.bin> [keyword ...]  |  --disasm <file> <path>")
        return 1
    if sys.argv[1] == "--disasm":
        return _run_disasm(sys.argv[2], sys.argv[3])
    return _dump_chunk(sys.argv[1], [k.lower() for k in sys.argv[2:]])


if __name__ == "__main__":
    raise SystemExit(main())
