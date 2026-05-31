"""Planet Zoo Archipelago client — A1 shell.

Subclasses Archipelago's ``CommonContext`` (the network layer is free) and adds:

  * a **manual trigger console** — type ``/pz_check <location>`` to fire a check,
    standing in for the game until the A2 memory layer lands;
  * **idempotent item application** — received items are applied through an
    ``EffectApplier`` exactly once each, tracked by a persisted high-water mark
    (see :mod:`pz_ap_client.state`);
  * **goal detection** — derived from ``slot_data.goal`` mapped onto our own
    location IDs; sends ``CLIENT_GOAL`` when satisfied.

Run it:  python -m pz_ap_client.client <server:port> --name <slot>

The A2/A3 wiring swaps ``ConsoleEffectApplier`` for a memory-backed applier and
adds a poll loop that calls :meth:`PZContext.report_check` from real game events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .memory.triggers import MemoryTriggerSource

# Don't let Archipelago's ModuleUpdate prompt to pip-install missing optional
# deps (e.g. kivy) at import time — that blocks on input() in a headless run.
os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")

# Make the vendored Archipelago tree importable.
_AP_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "Archipelago"
if str(_AP_ROOT) not in sys.path:
    sys.path.insert(0, str(_AP_ROOT))

# Importing CommonClient pulls in every registered AP world; several fail to
# import here because their niche optional deps aren't installed (we don't need
# them). Mute the root logger across the import so startup isn't a wall of
# "Could not load world" tracebacks.
_root = logging.getLogger()
_prev_level = _root.level
_root.setLevel(logging.CRITICAL)
try:
    # AP modules; importable only after the sys.path insert above (hence E402).
    import Utils  # noqa: E402
    from CommonClient import (  # noqa: E402
        CommonContext,
        ClientCommandProcessor,
        get_base_parser,
        gui_enabled,
        handle_url_arg,
        logger,
        server_loop,
    )
    from NetUtils import ClientStatus  # noqa: E402
finally:
    _root.setLevel(_prev_level)

from . import data as pz_data  # noqa: E402
from .effects import ConsoleEffectApplier, EffectApplier  # noqa: E402
from .state import ClientState  # noqa: E402

GAME_NAME = "Planet Zoo"


class PZCommandProcessor(ClientCommandProcessor):
    """Manual trigger console — the A1 stand-in for the running game."""

    # Narrow ctx from CommonContext to PZContext so the type checker sees our
    # subclass methods (resolve_location, report_check, game_data, ...).
    ctx: "PZContext"

    def _cmd_pz_check(self, location: str = "") -> bool:
        """Fire a location check by name (exact / substring) or id. Stand-in for a game event."""
        if not location:
            self.output("Usage: /pz_check <location name or id>")
            return False
        loc = self.ctx.resolve_location(location)
        if loc is None:
            self.output(f"No Planet Zoo location matches {location!r}. Try /pz_locations.")
            return False
        if loc.id in self.ctx.effective_checked:
            self.output(f"Already checked: {loc.name}")
            return False
        self.output(f"Reporting check: {loc.name} (id {loc.id})")
        self.ctx.report_check(loc.id)
        return True

    def _cmd_pz_locations(self, filter_text: str = "") -> bool:
        """List Planet Zoo locations with checked/missing status. Optional substring filter."""
        f = filter_text.lower()
        shown = 0
        for loc in self.ctx.game_data.locations:
            if f and f not in loc.name.lower():
                continue
            if loc.id in self.ctx.checked_locations:
                mark = "[x]"
            elif loc.id in self.ctx.missing_locations:
                mark = "[ ]"
            else:
                mark = "[-]"  # not part of this seed's location pool
            self.output(f"  {mark} {loc.id}  {loc.name}")
            shown += 1
        if not shown:
            self.output(f"No locations match {filter_text!r}.")
            return False
        return True

    def _cmd_pz_items(self) -> bool:
        """List received items and whether their effect has been applied."""
        if not self.ctx.items_received:
            self.output("No items received yet.")
            return False
        applied = self.ctx.applied_high_water()
        for idx, net_item in enumerate(self.ctx.items_received):
            item = self.ctx.game_data.item_by_id.get(net_item.item)
            name = item.name if item else f"<unknown {net_item.item}>"
            mark = "applied" if idx < applied else "pending"
            self.output(f"  #{idx:<3} {mark:<7} {name}")
        return True

    def _cmd_pz_goal(self) -> bool:
        """Show goal progress derived from slot_data."""
        need, have = self.ctx.goal_progress()
        if not need:
            self.output("No goal locations resolved yet (connect first).")
            return False
        for loc_id in need:
            loc = self.ctx.game_data.location_by_id.get(loc_id)
            mark = "[x]" if loc_id in have else "[ ]"
            self.output(f"  {mark} {loc.name if loc else loc_id}")
        self.output(f"Goal: {len(have)}/{len(need)} — {'COMPLETE' if self.ctx.finished_game else 'in progress'}")
        return True


class PZContext(CommonContext):
    # Empty string matches any game on connect (since AP 0.3.2) and avoids the
    # need for a locally-registered "Planet Zoo" world (that's Track B's APWorld).
    # We adopt the real game name from slot_info on Connected. Our own item/
    # location lookups come from data.json, not AP's network data package.
    game = ""
    items_handling = 0b111  # receive all items (own + others' sends to us)
    want_slot_data = True
    command_processor = PZCommandProcessor

    def __init__(self, server_address: Optional[str], password: Optional[str],
                 data_path: Optional[str] = None, applier: Optional[EffectApplier] = None,
                 state_dir: Optional[str] = None):
        super().__init__(server_address, password)
        self.game_data = pz_data.load(data_path)
        self.applier: EffectApplier = applier or ConsoleEffectApplier()
        self.state: Optional[ClientState] = None
        # Override the on-disk state location (tests pass a temp dir).
        self._state_dir = state_dir
        self.slot_data: dict = {}
        self._goal_location_ids: List[int] = []
        # Optional memory layer (A2/A3); set by enable_memory().
        self.trigger_source: "Optional[MemoryTriggerSource]" = None
        self.poll_interval: float = 1.0
        self._poll_task: "Optional[asyncio.Task]" = None
        # Strong refs to fire-and-forget tasks so the event loop's weak
        # references don't let them be garbage-collected mid-flight.
        self._bg_tasks: "set[asyncio.Task]" = set()

    def _spawn(self, coro) -> "asyncio.Task":
        """Schedule a fire-and-forget coroutine while holding a strong reference."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def enable_memory(self, poll_interval: float = 1.0) -> None:
        """Switch to the memory-backed applier + start a trigger poll loop.

        Imported lazily so the console-only path never needs pymem/anchors.
        """
        from .memory.scanner import MemoryScanner
        from .memory.anchors import AnchorTable
        from .memory.applier import MemoryEffectApplier
        from .memory.triggers import MemoryTriggerSource

        anchors = AnchorTable.load()
        scanner = MemoryScanner(anchors.process_name)
        self.applier = MemoryEffectApplier(scanner, anchors)
        self.trigger_source = MemoryTriggerSource(scanner, anchors, self.game_data, self.report_check)
        self.poll_interval = poll_interval
        unfilled = anchors.unfilled()
        if unfilled:
            logger.warning("Memory mode: %d anchors not yet filled in (%s). "
                           "Run the A2 spike — see docs/A2_SPIKE_PLAYBOOK.md.",
                           len(unfilled), ", ".join(unfilled))

    async def poll_loop(self) -> None:
        """Background tick: detect game events -> checks, and retry stalled item writes."""
        while not self.exit_event.is_set():
            try:
                if self.trigger_source is not None and self.slot is not None:
                    self.trigger_source.poll(self.effective_checked)
                    self._apply_new_items()  # retry anything that stalled (e.g. game not ready)
            except Exception:
                logger.exception("poll loop tick failed")
            await asyncio.sleep(self.poll_interval)

    # -- AP connection handshake ----------------------------------------------

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict):
        if cmd == "RoomInfo":
            self.seed_name = args["seed_name"]
        elif cmd == "Connected":
            # The base client sets ctx.slot before dispatching this packet.
            assert self.slot is not None, "Connected without a slot"
            # Adopt the real game name and teach AP's name lookups about our
            # data.json so built-in commands (/received, /missing) resolve names.
            self.game = self.slot_info[self.slot].game
            self.item_names.update_game(self.game, {i.name: i.id for i in self.game_data.items})
            self.location_names.update_game(self.game, self.game_data.name_to_location_id)
            self.slot_data = args.get("slot_data", {}) or {}
            self.state = ClientState.load(self.seed_name or "unknown", self.slot, self._state_dir)
            self._resolve_goal_locations()
            logger.info("Connected as slot %s. Goal: %s", self.slot, self.slot_data.get("goal"))
            self._apply_new_items()
            self._check_goal()
        elif cmd == "ReceivedItems":
            self._apply_new_items()
        elif cmd == "RoomUpdate":
            # base already merged checked_locations; re-evaluate goal
            self._check_goal()

    async def disconnect(self, allow_autoreconnect: bool = False):
        self.game = ""  # back to match-any so reconnect works
        await super().disconnect(allow_autoreconnect)

    # -- location checks (called by console now, by the poll loop in A3) -------

    @property
    def effective_checked(self) -> set:
        """Locations we consider done: server-confirmed plus ones we've sent.

        Using the union makes goal detection immediate (no dependence on the
        server's RoomUpdate echo timing) and survives a reconnect, since
        ``locations_checked`` is resent by the base client on reconnect.
        """
        return self.checked_locations | self.locations_checked

    def report_check(self, location_id: int) -> None:
        """Report a single location check to the server (deduped)."""
        if location_id in self.effective_checked:
            return
        self.locations_checked.add(location_id)
        self._spawn(self.check_locations([location_id]))
        self._check_goal()

    def resolve_location(self, query: str) -> Optional[pz_data.Location]:
        """Resolve a console query to a Location: exact name, then ci-substring, then id."""
        gd = self.game_data
        if query in gd.location_by_name:
            return gd.location_by_name[query]
        q = query.lower()
        matches = [l for l in gd.locations if q in l.name.lower()]
        if len(matches) == 1:
            return matches[0]
        if query.isdigit():
            return gd.location_by_id.get(int(query))
        return None

    # -- idempotent item application (A3) -------------------------------------

    def applied_high_water(self) -> int:
        if self.state is None or self.slot is None:
            return 0
        return self.state.get(self.seed_name or "unknown", self.slot)

    def _apply_new_items(self) -> None:
        if self.state is None or self.slot is None:
            return
        seed = self.seed_name or "unknown"
        applied = min(self.state.get(seed, self.slot), len(self.items_received))
        for idx in range(applied, len(self.items_received)):
            net_item = self.items_received[idx]
            item = self.game_data.item_by_id.get(net_item.item)
            if item is None:
                # Not one of ours (e.g. an Archipelago-global item); skip but
                # still advance so we don't get stuck re-trying it forever.
                logger.warning("Received unknown item id %s — skipping", net_item.item)
                self.state.set(seed, self.slot, idx + 1)
                continue
            if not self.applier.apply(item):
                # Transient failure (e.g. game not attached / write failed):
                # stop here, leave the high-water mark, retry on next event.
                logger.info("Pausing item application at #%s (%s); will retry", idx, item.name)
                break
            self.state.set(seed, self.slot, idx + 1)

    # -- goal detection --------------------------------------------------------

    def _resolve_goal_locations(self) -> None:
        """Map slot_data.goal (research/species keys) onto our own location IDs."""
        goal = self.slot_data.get("goal") or {}
        gargs = goal.get("args", {})
        ids: List[int] = []
        for rkey in gargs.get("required_research", []):
            for loc in self.game_data.locations_by_trigger("research_complete"):
                if loc.trigger_args.get("research_key") == rkey:
                    ids.append(loc.id)
        for skey in gargs.get("required_breed", []):
            for loc in self.game_data.locations_by_trigger("first_breed"):
                if loc.trigger_args.get("species_key") == skey:
                    ids.append(loc.id)
        self._goal_location_ids = sorted(set(ids))

    def goal_progress(self) -> tuple[List[int], set]:
        need = self._goal_location_ids
        have = set(need) & self.effective_checked
        return need, have

    def _check_goal(self) -> None:
        if self.finished_game or not self._goal_location_ids:
            return
        if set(self._goal_location_ids) <= self.effective_checked:
            self.finished_game = True
            logger.info("Goal complete! Sending CLIENT_GOAL.")
            self._spawn(
                self.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
            )


def main(args=None):
    async def _run(args):
        ctx = PZContext(args.connect, args.password, data_path=getattr(args, "data", None))
        ctx.auth = args.name
        if args.memory:
            ctx.enable_memory(poll_interval=args.poll_interval)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
        if args.memory:
            ctx._poll_task = asyncio.create_task(ctx.poll_loop(), name="pz poll loop")

        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        await ctx.shutdown()

    parser = get_base_parser(description="Planet Zoo Archipelago hooking client (Track A).")
    parser.add_argument("--name", default=None, help="Slot name to connect as.")
    parser.add_argument("--data", default=None, help="Path to data.json (defaults to project root).")
    parser.add_argument("--memory", action="store_true",
                        help="Attach to the running game: apply items + detect checks via memory (A2/A3). "
                             "Without this, runs the A1 manual-trigger console only.")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="Seconds between memory poll ticks (default 1.0).")
    parser.add_argument("url", nargs="?", help="Archipelago connection url / address.")
    parsed = parser.parse_args(args)
    parsed = handle_url_arg(parsed, parser=parser)

    import colorama
    colorama.just_fix_windows_console()
    asyncio.run(_run(parsed))
    colorama.deinit()


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    main(sys.argv[1:])
