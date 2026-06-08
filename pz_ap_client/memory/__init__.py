"""Memory access layer (Track A2/A3).

Splits into:
  * ``scanner``  - attach to the process, AOB/signature scan, pointer-chain
    resolution, typed read/write. No game-specific knowledge.
  * ``anchors``  - the offset/signature table (loaded from ``anchors.json``),
    filled in by the Cheat-Engine spike. This is the fragile, patch-sensitive
    part; everything else is generic.
  * ``applier``  - MemoryEffectApplier: turns received items into memory writes.
  * ``triggers`` - MemoryTriggerSource: polls memory, diffs, emits location ids.

Signature scanning (not hardcoded addresses) is used so the client survives
Frontier patches that shift code/data layout, per the locked decision in
ARCHIPELAGO_PLAN.md.
"""
