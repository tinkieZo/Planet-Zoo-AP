"""/pz_report - one-file diagnostic bundle for beta bug reports.

Collects everything a report like "I sent a bunch of checks just from connecting" needs into a
single text file the player can attach: client version + platform, the data.json fingerprint
(id-desync detector), mod-shell status, PZAP_* environment, connection/seed state, session state
(memory layer, AP-park detection, preflight results), every check sent THIS session with
timestamps, and the tail of the client log. Every section is best-effort - a report must never
fail because one probe did.
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List

if TYPE_CHECKING:
    from .client import PZContext

LOG_TAIL_LINES = 150


def _section(title: str) -> str:
    return f"\n=== {title} " + "=" * max(0, 60 - len(title)) + "\n"


def _guarded(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a report must never fail because one probe did
        return f"  <section failed: {e!r}>\n"


def _client_info() -> str:
    from . import CLIENT_VERSION
    return (f"  client version: {CLIENT_VERSION}\n"
            f"  frozen exe:     {bool(getattr(sys, 'frozen', False))}\n"
            f"  platform:       {platform.platform()}\n"
            f"  python:         {sys.version.split()[0]}\n")


def _data_fingerprint(ctx: "PZContext") -> str:
    gd = ctx.game_data
    path = Path(getattr(ctx, "_data_path", None) or
                Path(__file__).resolve().parent.parent / "data.json")
    digest = "<file unreadable>"
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except Exception:
        pass
    meta = gd.meta or {}
    return (f"  path:      {path}\n"
            f"  sha256/12: {digest}\n"
            f"  counts:    {len(gd.items)} items, {len(gd.locations)} locations, "
            f"{len(gd.species)} species\n"
            f"  id_ranges: {meta.get('id_ranges')}\n"
            f"  scope:     {meta.get('scope')} (schema {meta.get('schema_version')})\n")


def _env_flags() -> str:
    flags = {k: v for k, v in os.environ.items() if k.startswith("PZAP_")}
    return ("  " + "\n  ".join(f"{k}={v}" for k, v in sorted(flags.items())) + "\n") if flags \
        else "  (no PZAP_* environment overrides)\n"


def _mod_status() -> str:
    from . import ovl
    st = ovl.status()
    return f"  {st.state} - {st.detail}\n"


def _connection(ctx: "PZContext") -> str:
    lines = [
        f"  server:    {getattr(ctx, 'server_address', None)}",
        f"  seed:      {getattr(ctx, 'seed_name', None)}",
        f"  slot:      #{getattr(ctx, 'slot', None)} {getattr(ctx, 'auth', None)!r} "
        f"(team {getattr(ctx, 'team', None)})",
        f"  items received:      {len(ctx.items_received)} "
        f"(applied high-water {ctx.applied_high_water() if ctx.state else '<no state>'})",
        f"  server-checked:      {len(ctx.checked_locations)}",
        f"  sent (this client):  {len(ctx.locations_checked)}",
        f"  missing:             {len(ctx.missing_locations)}",
    ]
    goal = (ctx.slot_data or {}).get("goal")
    opts = {k: v for k, v in (ctx.slot_data or {}).items() if k != "goal"}
    lines.append(f"  slot_data goal:      {goal}")
    lines.append(f"  slot_data options:   {opts}")
    return "\n".join(lines) + "\n"


def _session_state(ctx: "PZContext") -> str:
    mem = ctx.trigger_source is not None
    lines = [f"  memory layer: {'enabled' if mem else 'DISABLED (console mode)'}"]
    if mem:
        scanner = getattr(ctx, "_scanner", None)
        lines.append(f"  game attached: {getattr(scanner, 'attached', None)}")
        session = getattr(ctx, "_session", None)
        lines.append(f"  AP park detected: {getattr(session, 'kind', None)} "
                     f"(game mode {getattr(session, 'mode', None)}; "
                     f"active last tick: {getattr(ctx, '_session_was_active', None)})")
        lines.append(f"  first poll tick done: {getattr(ctx, '_first_tick_done', False)}")
    zoo_binding = getattr(ctx, "_zoo_binding", None)
    lines.append(f"  claimed Challenge zoo: {zoo_binding() if callable(zoo_binding) else None}")
    results = getattr(ctx, "_preflight_results", None)
    if results is None:
        lines.append("  preflight: not run yet (runs on first attach)")
    else:
        bad = [r for r in results if r.status not in ("ok", "relocated")]
        lines.append(f"  preflight: {len(results) - len(bad)}/{len(results)} sites OK")
        lines.extend(f"    [{r.status}] {r.name}: {r.detail}" for r in results
                     if r.status != "ok")
    return "\n".join(lines) + "\n"


def _session_checks(ctx: "PZContext") -> str:
    log: List[tuple] = list(getattr(ctx, "_session_check_log", ()))
    if not log:
        return "  (none sent this session)\n"
    gd = ctx.game_data
    out = []
    for ts, loc_id in log:
        loc = gd.location_by_id.get(loc_id)
        out.append(f"  {time.strftime('%H:%M:%S', time.localtime(ts))}  {loc_id}  "
                   f"{loc.name if loc else '<unknown>'}")
    return "\n".join(out) + "\n"


def _log_tail() -> str:
    log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PlanetZooAP" / "logs"
    path = log_dir / "pz_client.log"
    if not path.is_file():
        return f"  <no log file at {path}>\n"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-LOG_TAIL_LINES:]
    return (f"  (last {len(tail)} of {len(lines)} lines from {path})\n"
            + "\n".join("  " + l for l in tail) + "\n")


def build_report(ctx: "PZContext") -> str:
    parts = [
        f"Planet Zoo AP client report - generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        _section("client"), _guarded(_client_info),
        _section("data.json fingerprint"), _guarded(lambda: _data_fingerprint(ctx)),
        _section("environment"), _guarded(_env_flags),
        _section("mod shell"), _guarded(_mod_status),
        _section("connection"), _guarded(lambda: _connection(ctx)),
        _section("session"), _guarded(lambda: _session_state(ctx)),
        _section("checks sent this session"), _guarded(lambda: _session_checks(ctx)),
        _section("client log tail"), _guarded(_log_tail),
    ]
    return "".join(parts)


def write_report(ctx: "PZContext") -> Path:
    """Build the report and write it under %LOCALAPPDATA%/PlanetZooAP/reports/; returns the path."""
    out_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PlanetZooAP" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / time.strftime("pz_report_%Y%m%d_%H%M%S.txt")
    path.write_text(build_report(ctx), encoding="utf-8")
    return path
