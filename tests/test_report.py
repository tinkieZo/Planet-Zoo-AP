"""Game-free tests for /pz_report (pz_ap_client/report.py): the diagnostic bundle must build on a
bare disconnected context (every section is best-effort), carry the load-bearing fields, and the
session-check audit trail must record what report_check sends."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client import CLIENT_VERSION, report  # noqa: E402
from pz_ap_client.client import PZContext  # noqa: E402


def _ctx(state_dir: str) -> PZContext:
    async def make():
        ctx = PZContext(None, None, state_dir=state_dir)
        ctx.send_msgs = lambda msgs: asyncio.sleep(0)          # stub the network
        ctx.check_locations = lambda ids: asyncio.sleep(0)
        # report_check fire-and-forgets network sends via _spawn; outside a running loop that
        # raises - close the coroutine instead (the send itself is not under test here)
        ctx._spawn = lambda coro: coro.close()
        return ctx
    return asyncio.new_event_loop().run_until_complete(make())


def test_report_builds_on_bare_context():
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx(td)
        text = report.build_report(ctx)
    assert CLIENT_VERSION in text, "client version stamped"
    for section in ("client", "data.json fingerprint", "environment", "mod shell",
                    "connection", "session", "checks sent this session", "client log tail"):
        assert f"=== {section} " in text, f"missing section {section!r}"
    assert "sha256/12:" in text and "id_ranges:" in text, "data fingerprint fields present"
    assert "(none sent this session)" in text, "empty session-check trail rendered"
    assert "memory layer: DISABLED" in text, "console-mode session state rendered"


def test_report_lists_session_checks():
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx(td)
        loc = ctx.game_data.locations[0]
        ctx.report_check(loc.id)
        text = report.build_report(ctx)
    assert str(loc.id) in text and loc.name in text, "sent check appears with id + name"
    assert "(none sent this session)" not in text


def test_write_report_creates_file(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("LOCALAPPDATA", td)     # redirect the reports dir
        ctx = _ctx(td)
        path = report.write_report(ctx)
        assert path.is_file() and path.suffix == ".txt"
        assert CLIENT_VERSION in path.read_text(encoding="utf-8")
        assert path.parent == Path(td) / "PlanetZooAP" / "reports"
