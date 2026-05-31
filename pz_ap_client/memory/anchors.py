"""The offset/signature table — the fragile, patch-sensitive part of Track A.

Anchors are loaded from ``anchors.json`` (sibling file). Each anchor describes
how to resolve a logical game value (cash, CC, a research flag, the birth
signal, ...) to a memory address, in a way that survives patches by preferring
**signatures over absolute addresses**.

Two resolution kinds:

  * ``module_offset`` — final address = module_base walked through ``offsets``
    as a pointer chain. Simple, but the base offset can move between patches.

  * ``signature`` — AOB-scan for ``signature``; optionally treat the match as a
    RIP-relative instruction (``rip: {disp_offset, instr_len}``) to get the
    address of the static it references; then walk ``offsets``. This is the
    patch-robust form and the one the spike should prefer.

Until the Cheat-Engine spike fills this in, ``anchors.json`` ships with empty /
TODO entries and ``AnchorTable.resolve`` returns None for unfilled anchors, so
the client runs (A1 console) without a populated table.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .scanner import MemoryScanner

logger = logging.getLogger("PZClient")

VALUE_TYPES = {"i32", "i64", "float", "double", "bytes"}

DEFAULT_ANCHORS_PATH = Path(__file__).resolve().parent / "anchors.json"


@dataclass
class Anchor:
    name: str
    kind: str  # "module_offset" | "signature"
    type: str = "i32"
    offsets: List[int] = field(default_factory=list)
    signature: Optional[str] = None
    rip: Optional[Dict[str, int]] = None  # {"disp_offset": int, "instr_len": int}
    module_only: bool = True
    notes: str = ""

    @property
    def filled(self) -> bool:
        """True once the spike has supplied enough to resolve this anchor."""
        if self.kind == "signature":
            return bool(self.signature)
        if self.kind == "module_offset":
            return bool(self.offsets)
        return False

    def resolve(self, scanner: MemoryScanner) -> Optional[int]:
        """Resolve this anchor to a final memory address, or None if unresolved."""
        if not scanner.attached or not self.filled:
            return None
        if self.kind == "module_offset":
            if scanner.module_base is None:
                return None
            return scanner.resolve_pointer_chain(scanner.module_base, self.offsets)
        if self.kind == "signature":
            if self.signature is None:  # guaranteed by .filled, but narrows the type
                return None
            hit = scanner.aob_scan(self.signature, module_only=self.module_only)
            if hit is None:
                logger.warning("anchor %r: signature not found (patch changed?)", self.name)
                return None
            base = hit
            if self.rip:
                base = scanner.resolve_rip_relative(hit, self.rip["disp_offset"], self.rip["instr_len"])
            return scanner.resolve_pointer_chain(base, self.offsets) if self.offsets else base
        logger.error("anchor %r: unknown kind %r", self.name, self.kind)
        return None


@dataclass
class AnchorTable:
    process_name: str
    anchors: Dict[str, Anchor] = field(default_factory=dict)
    # entity_offsets["research"][research_key] -> byte offset into research_state_base, etc.
    entity_offsets: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AnchorTable":
        path = Path(path or DEFAULT_ANCHORS_PATH)
        raw = json.loads(path.read_text(encoding="utf-8"))
        anchors = {
            name: Anchor(
                name=name,
                kind=spec.get("kind", "signature"),
                type=spec.get("type", "i32"),
                offsets=[int(o) for o in spec.get("offsets", [])],
                signature=spec.get("signature"),
                rip=spec.get("rip"),
                module_only=spec.get("module_only", True),
                notes=spec.get("notes", ""),
            )
            for name, spec in raw.get("anchors", {}).items()
        }
        for a in anchors.values():
            if a.type not in VALUE_TYPES:
                raise ValueError(f"anchor {a.name!r} has unknown type {a.type!r}")
        eo = {
            group: {k: int(v) for k, v in mapping.items()}
            for group, mapping in raw.get("entity_offsets", {}).items()
            if group != "_doc" and isinstance(mapping, dict)
        }
        return cls(
            process_name=raw.get("process_name", "PlanetZoo.exe"),
            anchors=anchors,
            entity_offsets=eo,
        )

    def get(self, name: str) -> Optional[Anchor]:
        return self.anchors.get(name)

    def entity_offset(self, group: str, key: str) -> Optional[int]:
        return self.entity_offsets.get(group, {}).get(key)

    def read_entity(self, scanner: MemoryScanner, base_anchor: str, group: str, key: str,
                    type_: str = "i32"):
        """Read a per-entity value at (resolved base_anchor address) + entity_offset.

        Returns None if the base anchor or the per-key offset isn't filled in yet.
        """
        anchor = self.get(base_anchor)
        if anchor is None:
            return None
        base_addr = anchor.resolve(scanner)
        off = self.entity_offset(group, key)
        if base_addr is None or off is None:
            return None
        return _read_typed(scanner, base_addr + off, type_)

    def write_entity(self, scanner: MemoryScanner, base_anchor: str, group: str, key: str,
                     value, type_: str = "i32") -> bool:
        anchor = self.get(base_anchor)
        if anchor is None:
            return False
        base_addr = anchor.resolve(scanner)
        off = self.entity_offset(group, key)
        if base_addr is None or off is None:
            return False
        _write_typed(scanner, base_addr + off, type_, value)
        return True

    def read(self, scanner: MemoryScanner, name: str):
        anchor = self.get(name)
        if anchor is None:
            return None
        addr = anchor.resolve(scanner)
        if addr is None:
            return None
        return _read_typed(scanner, addr, anchor.type)

    def write(self, scanner: MemoryScanner, name: str, value) -> bool:
        anchor = self.get(name)
        if anchor is None:
            return False
        addr = anchor.resolve(scanner)
        if addr is None:
            return False
        _write_typed(scanner, addr, anchor.type, value)
        return True

    def unfilled(self) -> List[str]:
        return [n for n, a in self.anchors.items() if not a.filled]


def _read_typed(scanner: MemoryScanner, addr: int, type_: str):
    return {
        "i32": scanner.read_i32,
        "i64": scanner.read_i64,
        "float": scanner.read_float,
        "double": scanner.read_double,
    }.get(type_, scanner.read_i32)(addr)


def _write_typed(scanner: MemoryScanner, addr: int, type_: str, value) -> None:
    {
        "i32": scanner.write_i32,
        "i64": scanner.write_i64,
        "float": scanner.write_float,
        "double": scanner.write_double,
    }.get(type_, scanner.write_i32)(addr, value)
