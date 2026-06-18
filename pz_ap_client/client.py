"""Planet Zoo Archipelago client - A1 shell.

Subclasses Archipelago's ``CommonContext`` (the network layer is free) and adds:

  * a **manual trigger console** - type ``/pz_check <location>`` to fire a check,
    standing in for the game until the A2 memory layer lands;
  * **idempotent item application** - received items are applied through an
    ``EffectApplier`` exactly once each, tracked by a persisted high-water mark
    (see :mod:`pz_ap_client.state`);
  * **goal detection** - derived from ``slot_data.goal`` mapped onto our own
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
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .memory.triggers import MemoryTriggerSource

# Don't let Archipelago's ModuleUpdate prompt to pip-install missing optional
# deps (e.g. kivy) at import time - that blocks on input() in a headless run.
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
    """Manual trigger console - the A1 stand-in for the running game."""

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
        self.output(f"Goal: {len(have)}/{len(need)} - {'COMPLETE' if self.ctx.finished_game else 'in progress'}")
        return True

    # --- AP scenario shell (ovl) management -------------------------------

    def _cmd_pz_mod(self) -> bool:
        """Show whether the AP scenario shell is installed in the game's Main.ovl."""
        from . import ovl
        st = ovl.status()
        self.output(f"Mod status: {st.state} - {st.detail}")
        if st.state in ("vanilla", "stale", "game-updated"):
            self.output("Run /pz_install to install/update the AP scenario (game must be closed).")
        elif st.state == "ambiguous":
            self.output("Run /pz_restore to recover vanilla, then /pz_install.")
        return True

    def _cmd_pz_install(self) -> bool:
        """Install/update the AP scenario shell into Main.ovl (builds from YOUR game files; ~4 min)."""
        return self.ctx.run_ovl_job("install")

    def _cmd_pz_restore(self) -> bool:
        """Restore the vanilla Main.ovl from the backup made at install time."""
        return self.ctx.run_ovl_job("restore")

    def _cmd_pz_launch(self) -> bool:
        """Launch Planet Zoo via Steam (with the scenario-intro skip flag)."""
        from . import ovl
        st = ovl.status()
        if st.state != "installed":
            self.output(f"Note: mod status is '{st.state}' - the ARCHIPELAGO career entry needs /pz_install.")
        ovl.launch_game()
        self.output("Launching Planet Zoo via Steam... pick the ARCHIPELAGO career entry once it's up.")
        return True


class PZContext(CommonContext):
    # Stay game-agnostic at construction (game="") so CommonContext.__init__ doesn't look up a LOCAL
    # "Planet Zoo" data package - that needs the APWorld (Track B), which this client doesn't bundle
    # (it would KeyError). We still authenticate AS Planet Zoo by passing game=GAME_NAME explicitly in
    # send_connect() - the server validates that string against the slot, and empty-game "match any"
    # is rejected as InvalidGame in AP 0.6.x. After Connected we adopt the real game name from
    # slot_info; our own item/location lookups come from data.json, not the network data package.
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
        self.permit_gate = None  # PermitGate (memory mode); enforces species_unlock
        self.facility_gate = None  # FacilityGate (memory mode); enforces facility_unlock placement
        self.research_gate = None  # ResearchGate (memory mode); enforces research_centre/workshop
        self.presence_gate = None  # PresenceGate (memory mode); native greyed-button UX for those
        self.terrain_gate = None  # TerrainGate (memory mode); native terrain-tool greying (water_tools)
        self.market_gate = None  # SpeciesMarketGate (memory mode); restricts the autofill market to unlocked species
        self.reward_granter = None  # RewardGranter (memory mode); grants decoupled research rewards
        self._market_last_allowed = None  # last unlocked-species key set applied (re-apply only on change)
        self._park_age = None  # ParkAgeReader (memory mode); reads park years-open to detect a fresh save
        self._session = None  # ApSessionDetector (memory mode); is the LOADED park the AP scenario?
        self._scanner = None  # the shared MemoryScanner (memory mode)
        self._fresh_reset_done = False  # re-awarded all items for a fresh zoo once this session
        self._ovl_job_running = False  # one ovl install/restore at a time (see run_ovl_job)
        self._initial_applied: "Optional[int]" = None  # high-water mark at session start (drives re-award)
        self._preflight_done = False  # run the signatures self-check once on first attach (fail-loud)
        self._pending_checks: List[int] = []  # location ids queued by the poll thread, sent on the loop
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

    def make_gui(self):
        ui = super().make_gui()
        ui.base_title = "Planet Zoo Archipelago Client"
        return ui

    def run_ovl_job(self, action: str) -> bool:
        """Run the ovl install/restore in a worker thread (the inject takes minutes -
        it must not block the asyncio loop or the GUI). One job at a time."""
        from . import ovl
        if self._ovl_job_running:
            logger.info("An ovl install/restore is already running.")
            return False
        fn = ovl.install if action == "install" else ovl.restore

        async def _job():
            self._ovl_job_running = True
            try:
                loop = asyncio.get_running_loop()
                st = await loop.run_in_executor(None, lambda: fn(log=logger.info))
                logger.info("Mod status: %s - %s", st.state, st.detail)
            except ovl.OvlInstallError as e:
                logger.error("%s failed: %s", action, e)
            except Exception:
                logger.exception("%s failed", action)
            finally:
                self._ovl_job_running = False

        self._spawn(_job())
        return True

    def enable_memory(self, poll_interval: float = 1.0) -> None:
        """Switch to the memory-backed applier + start a trigger poll loop.

        Imported lazily so the console-only path never needs pymem/anchors.
        """
        from .memory.scanner import MemoryScanner
        from .memory.anchors import AnchorTable
        from .memory.applier import MemoryEffectApplier
        from .memory.triggers import MemoryTriggerSource
        from .memory.permits import PermitGate
        from .memory.facilities import FacilityGate
        from .memory.research import ResearchGate, FACILITY_RESEARCH_CATEGORY
        from .memory.presence import PresenceGate
        from .memory.terrain import TerrainGate
        from .memory.zoodate import ParkAgeReader

        anchors = AnchorTable.load()
        scanner = MemoryScanner(anchors.process_name)
        self._scanner = scanner
        # Reads the park's completed-years-open counter; a fresh zoo (Year 1) re-awards all received
        # items, latched so it fires once per fresh save (see _apply_new_items / state.fresh_pending).
        self._park_age = ParkAgeReader(scanner)
        # Permit gate: memory-enforced per-species purchase block. The gated universe
        # = every species that has a species_unlock item; received permits unblock them.
        self.permit_gate = PermitGate(scanner)
        # Purchase-block universe = every species whose gate has a token we enforce by
        # purchase-block: a species permit (species_unlock), OR a tool we can't gate
        # natively (water_tools - its in-game tool button lives in the data-driven Cobra
        # UI we can't reach; see docs/OVERNIGHT_2026-06-03.md). Facility tokens
        # (research_centre/workshop) are NOT in here - they have their own gates.
        self.permit_gate.set_gated(self.game_data.purchase_universe())
        # All facility_unlock keys in this seed, split by enforcement mechanism:
        #   research_centre / workshop -> ResearchGate (research-start block by category)
        #   everything else            -> FacilityGate (building-placement block)
        all_fac = {i.effect_args.get("facility_key") for i in self.game_data.items
                   if i.effect_type == "facility_unlock" and i.effect_args.get("facility_key")}
        research_fac = all_fac & set(FACILITY_RESEARCH_CATEGORY)
        placement_fac = all_fac - research_fac
        # Trigger source owns the ReleaseDetector (release-gate) + a ResearchReader the gate reuses.
        self.trigger_source = MemoryTriggerSource(scanner, anchors, self.game_data, self._collect_check)
        # Facility (placement) gate - only the non-research facilities (trade_centre, vet_surgery).
        self.facility_gate = FacilityGate(scanner)
        self.facility_gate.set_gated(placement_fac)
        # Research gate - research_centre / workshop, enforced by the research-start hook (hard
        # enforcement: no progress/completion, resets in-progress). The PresenceGate adds the
        # native greyed-button UX (the facility reads as not-built) on the same gated set.
        self.research_gate = ResearchGate(reader=self.trigger_source.research)
        self.research_gate.set_gated(research_fac)
        self.presence_gate = PresenceGate(scanner)
        self.presence_gate.set_gated(research_fac)
        # Terrain-tool gate - native greying of the terrain-edit menu tools (water_tools), by patching
        # the BuildCategories Lua bytecode. The real enforcement of the tool item (vs the PermitGate
        # purchase-block proxy, which stays as belt-and-suspenders for the water-gated species).
        self.terrain_gate = TerrainGate(scanner)
        self.terrain_gate.set_gated(self.game_data.tool_keys())
        # Scenario-market gate - the AP base (Scenario_15_Empty) AUTOFILLS its market from a candidate
        # pool (mode 0), unlike the old empty-schedule base. The client restricts that pool to the
        # unlocked species via the LocalAnimalExchange include-set (routing the rebuild to the default
        # whitelist) and expires already-listed blocked species, so only unlocked species are offered.
        # Reuses the trigger source's ResearchReader so species-id resolution shares one snapshot path
        # with the permit gate. (The old additive ScheduleSpawner is retained in market.py for bases
        # with a dormant autofill + a baked schedule, but this base needs the subtractive gate.)
        from .memory.market import SpeciesMarketGate
        self.market_gate = SpeciesMarketGate(scanner, research=self.trigger_source.research)
        # Reward granter - applies decoupled research rewards (research_reward items) by flipping
        # the content's unlocked byte in the unlockables map. Shares the trigger source's
        # ResearchReader so it resolves the research system the same way.
        from .memory.rewards import RewardGranter
        self.reward_granter = RewardGranter(scanner, research=self.trigger_source.research)
        # AP-session detection - the whole poll tick is a no-op unless the LOADED park is the AP
        # scenario (park-name marker planted by Scenario_AP_Script + scenario-mode market). Keeps the
        # client from gating/awarding inside franchise/sandbox/vanilla-career parks. Escape hatch:
        # PZAP_NO_SESSION_GATE=1 restores the old gate-whatever-is-loaded behaviour (debugging, or an
        # ovl that predates the SetParkName marker).
        from .memory.session import ApSessionDetector
        self._session = ApSessionDetector(scanner, mode_check=self.market_gate.scenario_mode)
        self.applier = MemoryEffectApplier(scanner, anchors, permit_gate=self.permit_gate,
                                           release_gate=self.trigger_source.releases,
                                           facility_gate=self.facility_gate,
                                           research_gate=self.research_gate,
                                           reward_granter=self.reward_granter)
        # Static seed facts for the conservation gate: is it a gated program here, and/or
        # do we need its release counter for a milestone? (Don't install the blocking hook
        # in a seed that neither gates conservation nor counts releases.)
        self._conservation_gated = any(
            i.effect_type == "program_unlock" and i.effect_args.get("program_key") == "conservation"
            for i in self.game_data.items)
        self._conservation_counted = any(
            l.trigger_type == "milestone" and (l.trigger_args or {}).get("metric") == "conservation_release"
            for l in self.game_data.locations)
        self.poll_interval = poll_interval
        unfilled = anchors.unfilled()
        if unfilled:
            logger.warning("Memory mode: %d anchors not yet filled in (%s). "
                           "Run the A2 spike - see docs/A2_SPIKE_PLAYBOOK.md.",
                           len(unfilled), ", ".join(unfilled))

    def _collect_check(self, location_id: int) -> None:
        """TriggerSource's report callback. Runs on the POLL WORKER THREAD, so it must NOT touch the
        websocket - it only queues the id. poll_loop drains the queue and does the real report on the
        event loop (where server sends are safe)."""
        self._pending_checks.append(location_id)

    def _session_active(self) -> bool:
        """True iff the loaded park is the AP scenario (or session detection is bypassed). Evaluated
        before every poll tick so the client is INERT in foreign parks - no hooks, no item writes, no
        checks - and wakes up by itself when the AP scenario loads."""
        if self._session is None or os.environ.get("PZAP_NO_SESSION_GATE") == "1":
            return True
        scanner = self._scanner
        if not scanner.attached and not scanner.attach():
            return False    # game not running; nothing to gate
        return self._session.is_ap_session()

    def _poll_tick(self) -> None:
        """The synchronous poll body. Runs in a worker thread (see poll_loop) and does only memory I/O -
        detection, item application, gate installs/reconciles - NO server sends (checks are queued via
        _collect_check). Each step is isolated so one failure doesn't abort the rest of the tick."""
        if not self._session_active():
            return
        for step in (
            lambda: self.trigger_source.poll(self.effective_checked),  # game events -> queued checks
            self._run_preflight,        # once, on first attach: self-check every patch-sensitive site
            self._apply_new_items,      # apply/retry received items (+ fresh-save re-award)
            self._reconcile_permits,    # keep the purchase gate = full received-permit set
            self._reconcile_conservation,  # keep the release gate = (conservation program received?)
            self._reconcile_facilities,    # keep the placement gate = full received-facility set
            self._reconcile_research,      # keep the research gate = (research facilities received?)
            self._reconcile_presence,      # keep the native greyed-button UX in sync
            self._reconcile_terrain,       # keep the native terrain-tool greying = received tool set
            self._reconcile_market,        # keep the scenario market stocked = unlocked species only
        ):
            try:
                step()
            except Exception:
                logger.exception("poll loop step failed")

    async def poll_loop(self) -> None:
        """Background tick: detect game events -> checks, apply items, reconcile gates.

        The synchronous memory work (gate installs, bytecode/heap scans, item writes) runs in a worker
        thread via run_in_executor so it NEVER blocks the asyncio loop - a long scan on the loop would
        starve the websocket keepalive ping/pong and trip a ``1011`` timeout disconnect (the heavy first
        tick after attach did exactly that). Detected checks are queued on the thread and reported here on
        the loop afterwards; run_in_executor completes before we drain, so there's no concurrency on the
        queue or on the websocket sends."""
        loop = asyncio.get_event_loop()
        while not self.exit_event.is_set():
            if self.trigger_source is not None and self.slot is not None:
                try:
                    await loop.run_in_executor(None, self._poll_tick)
                except Exception:
                    logger.exception("poll loop tick failed")
                while self._pending_checks:
                    self.report_check(self._pending_checks.pop(0))
            await asyncio.sleep(self.poll_interval)

    # -- AP connection handshake ----------------------------------------------

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        if self.auth:
            self.auth = self.auth.strip()  # defensive: drop any stray whitespace from a typed slot name
        # Send game=GAME_NAME on the wire so the server matches us to the Planet Zoo slot, WITHOUT
        # setting self.game (which would make CommonContext require a local Planet Zoo data package).
        await self.send_connect(game=GAME_NAME)

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
        self.game = ""  # stay game-agnostic on the context; we re-send game=GAME_NAME on reconnect
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

    def _park_years(self) -> "Optional[int]":
        """Completed years the loaded park has been open (0 in Year 1), or None if unknown (no memory /
        no zoo / counter not located). Returns None while the park-age anchor is gated off (see
        zoodate.PARKAGE_ENABLED), so the fresh-reset can never mis-fire on an established zoo."""
        from .memory.zoodate import PARKAGE_ENABLED
        reader = self._park_age
        if not PARKAGE_ENABLED or reader is None:
            return None
        try:
            return reader.read()
        except Exception:
            return None

    def _maybe_fresh_reset(self, seed: str, applied: int, years: "Optional[int]") -> int:
        """If attached to a FRESH zoo (``years`` open below the threshold) that we've previously awarded
        items to, zero the high-water mark so all cumulative items re-apply. Latched via
        state.fresh_pending so it fires once per fresh save - not on every reconnect to the same young
        zoo - and re-arms only once the zoo matures past the threshold. Returns the (possibly reset)
        applied count. Unknown age (None) => do nothing (fail safe: never a spurious re-award)."""
        from .memory.zoodate import FRESH_YEARS
        if years is None:
            return applied
        pending = self.state.get_fresh_pending(seed, self.slot)
        if years >= FRESH_YEARS:
            if pending:
                self.state.set_fresh_pending(seed, self.slot, False)  # matured -> arm for the next new save
            return applied
        if pending:
            return applied                                            # this fresh save already handled
        # First handling of this fresh park: set the room's starting cash baseline, then arm the
        # re-award (which re-applies all received items, incl. cash, on top of that baseline).
        self._apply_starting_money()
        if applied > 0 and not self._fresh_reset_done:
            self._fresh_reset_done = True
            self.state.set(seed, self.slot, 0)
            applied = 0
            logger.info("Fresh zoo detected (Year 1, %d years open) - re-awarding all received items", years)
        self.state.set_fresh_pending(seed, self.slot, True)           # mark this fresh save handled
        return applied

    def _apply_starting_money(self) -> None:
        """Override the scenario's default starting cash with the room's ``starting_money`` (slot_data,
        whole dollars). Once per fresh park (folded into the fresh-reset). The ovl ships a fixed
        starting cash; the room's per-seed value is applied here. No-op in console mode / if the room
        sent no starting_money / if the cash anchor isn't resolvable."""
        amount = self.slot_data.get("starting_money")
        if amount is None:
            return
        anchors = getattr(self.applier, "anchors", None)
        scanner = getattr(self.applier, "scanner", None)
        if anchors is None or scanner is None:
            return  # console applier (no game) - nothing to write
        if anchors.write(scanner, "cash", amount):
            logger.info("Starting money set to $%s (room slot_data)", amount)
        else:
            logger.info("Could not set starting money (cash anchor unresolved) - will retry next fresh detect")

    def applied_high_water(self) -> int:
        if self.state is None or self.slot is None:
            return 0
        return self.state.get(self.seed_name or "unknown", self.slot)

    def _apply_new_items(self) -> None:
        if self.state is None or self.slot is None:
            return
        seed = self.seed_name or "unknown"
        # Capture the high-water mark at SESSION START (before this session applies anything). The
        # fresh-save re-award keys off THIS, not the live mark: a first connect to a fresh zoo (mark 0)
        # must apply items once - NOT apply then re-award (which would double cumulative cash/cc, since the
        # park-age may read None on the first tick and only become 0 a tick later). Only a mark carried
        # over from a PRIOR session (a new save replacing an old one) should trigger the re-award.
        if self._initial_applied is None:
            self._initial_applied = self.state.get(seed, self.slot)
        # Fresh-save re-award: a brand-new zoo (Year 1) re-applies ALL received items by zeroing the
        # high-water mark. Unlocks are idempotent (gates reconcile); cash/cc re-grant. Latched in persisted
        # state so it fires once per fresh save, not on every reconnect to the same young zoo.
        self._maybe_fresh_reset(seed, self._initial_applied, self._park_years())
        applied = min(self.state.get(seed, self.slot), len(self.items_received))
        for idx in range(applied, len(self.items_received)):
            net_item = self.items_received[idx]
            item = self.game_data.item_by_id.get(net_item.item)
            if item is None:
                # Not one of ours (e.g. an Archipelago-global item); skip but
                # still advance so we don't get stuck re-trying it forever.
                logger.warning("Received unknown item id %s - skipping", net_item.item)
                self.state.set(seed, self.slot, idx + 1)
                continue
            if not self.applier.apply(item):
                # Transient failure (e.g. game not attached / write failed):
                # stop here, leave the high-water mark, retry on next event.
                logger.info("Pausing item application at #%s (%s); will retry", idx, item.name)
                break
            self.state.set(seed, self.slot, idx + 1)
        # Re-derive the purchase gate authoritatively right after applying, so a tool/permit
        # just received takes effect immediately (not only on the next poll tick), and any
        # transient over-unblock from a per-item unlock() is corrected within this call.
        self._reconcile_permits()

    def _reconcile_permits(self) -> None:
        """Drive the purchase gate from the COMPLETE set of received items, evaluating each
        species' FULL gate expression (AND-semantics over its purchase tokens). A species is
        unblocked only when every purchase token in its gate is satisfied - so nile_hippo
        unblocks on water_tools, and saltwater_croc only on water_tools AND its permit.
        Authoritative + idempotent (restart-correct; doesn't rely on the item high-water mark);
        the gate re-syncs only on change."""
        if self.permit_gate is None:
            return
        received_ids = [ni.item for ni in self.items_received]
        blocked = self.game_data.purchase_blocked_species(received_ids)
        satisfied = self.game_data.purchase_universe() - blocked
        try:
            self.permit_gate.reconcile(satisfied)
        except Exception:
            logger.exception("permit reconcile failed")

    def _reconcile_conservation(self) -> None:
        """Drive the release-to-wild gate from the received set, authoritatively (so it's
        correct across restarts, like permits). Locked until the Conservation Program
        (program_unlock) item is received. Skip entirely in a seed that neither gates
        conservation nor needs the release counter - so we never block a player whose
        seed has no conservation feature."""
        if self.trigger_source is None or not (self._conservation_gated or self._conservation_counted):
            return
        unlocked = not self._conservation_gated or any(
            (it := self.game_data.item_by_id.get(ni.item)) is not None
            and it.effect_type == "program_unlock"
            and it.effect_args.get("program_key") == "conservation"
            for ni in self.items_received)
        try:
            self.trigger_source.releases.set_locked(not unlocked)
        except Exception:
            logger.exception("conservation reconcile failed")

    def _reconcile_facilities(self) -> None:
        """Drive the placement gate from the COMPLETE set of received facility items.
        Authoritative + idempotent (restart-correct), like the permit reconcile."""
        if self.facility_gate is None:
            return
        received = set()
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type == "facility_unlock":
                key = it.effect_args.get("facility_key")
                if key:
                    received.add(key)
        try:
            self.facility_gate.reconcile(received)
        except Exception:
            logger.exception("facility reconcile failed")

    def _reconcile_research(self) -> None:
        """Drive the research-start gate from the COMPLETE set of received research facilities
        (research_centre -> animal research, workshop -> mechanic research). Authoritative +
        idempotent (restart-correct): research is blocked from starting until the facility item
        is received. No-op if the seed gates no research facilities."""
        if self.research_gate is None or not self.research_gate.gated_facilities:
            return
        received = set()
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type == "facility_unlock":
                key = it.effect_args.get("facility_key")
                if key:
                    received.add(key)
        try:
            self.research_gate.reconcile(received)
        except Exception:
            logger.exception("research reconcile failed")

    def _reconcile_presence(self) -> None:
        """Drive the native greyed-button presence gate from the COMPLETE set of received research
        facilities - the UX twin of _reconcile_research (research_centre greys the research button,
        workshop disables mechanic research). Authoritative + idempotent; no-op if no research
        facilities are gated this seed."""
        if self.presence_gate is None or not self.presence_gate.gated_facilities:
            return
        received = set()
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type == "facility_unlock":
                key = it.effect_args.get("facility_key")
                if key:
                    received.add(key)
        try:
            self.presence_gate.reconcile(received)
        except Exception:
            logger.exception("presence reconcile failed")

    def _reconcile_market(self) -> None:
        """Restrict the AP scenario's animal market to the unlocked species (and ONLY them). This
        base autofills its market from a candidate pool, so the client installs an include-set
        allow-list (the unlocked species) on the LocalAnimalExchange - routing the autofill rebuild
        to that whitelist - and expires any already-listed blocked species, so future autofill only
        spawns unlocked species. Re-applied only when the unlocked set CHANGES (each apply forces a
        pool rebuild). No-op outside scenario mode (sandbox/franchise markets are engine-driven)."""
        if self.market_gate is None or not self.market_gate.scenario_mode():
            return
        received_ids = [ni.item for ni in self.items_received]
        allowed = self.game_data.purchase_universe() - self.game_data.purchase_blocked_species(received_ids)
        if allowed == self._market_last_allowed:
            return  # unchanged since last apply; don't re-trigger a pool rebuild every tick
        allowed_ids = self.market_gate._resolve_handles(sorted(allowed))
        if not allowed_ids and allowed:
            return  # research snapshot not ready yet - retry next tick (don't mark this set applied)
        if self.market_gate.apply_unlocked(allowed_ids):
            self._market_last_allowed = set(allowed)
            self.market_gate.expire_blocked_listings(allowed_ids)  # clear stale blocked listings now

    def _run_preflight(self) -> None:
        """Once, on first successful attach: run the signatures self-check and log a health report.
        Fail-loud - if a Frontier patch shifted/changed any hook, anchor, or the terrain bytecode, this
        names exactly what broke so re-RE is targeted. Best-effort; never raises into the poll loop."""
        if self._preflight_done or self.trigger_source is None:
            return
        scanner = self.trigger_source.scanner
        if not getattr(scanner, "attached", False):
            return  # wait until attached (the game is running + a zoo loaded)
        self._preflight_done = True
        try:
            from .memory import signatures as sig
            from .memory.anchors import AnchorTable
            try:
                at = AnchorTable.load()
            except Exception:
                at = None
            results = sig.run_selfcheck(scanner, at)
            bad = [r for r in results if r.status not in ("ok", "relocated")]
            reloc = [r for r in results if r.status == "relocated"]
            ok = len(results) - len(bad)
            logger.info("preflight self-check: %d/%d sites OK", ok, len(results))
            for r in reloc:
                logger.warning("preflight: %s AUTO-RELOCATED (%s) - game likely patched; verify", r.name, r.detail)
            for r in bad:
                logger.error("preflight: %s [%s] %s - gate/detection for it may be unreliable", r.name, r.status, r.detail)
        except Exception as e:
            logger.warning("preflight self-check skipped (%s)", e)

    def _reconcile_terrain(self) -> None:
        """Drive the native terrain-tool gate from the COMPLETE set of received tool items: each gated
        terrain tool (e.g. water_tools) is greyed in the terrain-edit menu until its item arrives, then
        force-enabled. Authoritative + idempotent (restart-correct); no-op if the seed gates no tools."""
        if self.terrain_gate is None or not self.terrain_gate.gated_tools:
            return
        received = set()
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type == "tool_unlock":
                key = it.effect_args.get("tool_key")
                if key:
                    received.add(key)
        try:
            self.terrain_gate.reconcile(received)
        except Exception:
            logger.exception("terrain reconcile failed")

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


def _gui_available() -> bool:
    """Is the Kivy GUI importable? (The frozen release bundles it; a bare dev env may not.)"""
    import importlib.util
    return importlib.util.find_spec("kivy") is not None


def _prompt_missing(parsed) -> None:
    """Fill in connection details interactively so a double-clicked exe needs no flags.
    Anything passed on the command line is respected and skips the matching prompt; an empty
    answer falls back to the in-console "/connect" flow. Skipped entirely when the GUI will
    run - its connect bar is the prompt (and input() would block the GUI thread)."""
    if gui_enabled and _gui_available():
        return
    if not parsed.connect:
        parsed.connect = input("Archipelago server address (host:port): ").strip() or None
    if not parsed.name:
        parsed.name = input("Slot name: ").strip() or None


def _log_mod_status() -> None:
    """One startup line so a user immediately knows whether the game side is ready."""
    try:
        from . import ovl
        st = ovl.status()
        logger.info("AP scenario mod: %s - %s", st.state, st.detail)
        if st.state in ("vanilla", "stale", "game-updated"):
            logger.info("Type /pz_install to install/update it (game must be closed), "
                        "/pz_launch to start the game.")
        elif st.state == "ambiguous":
            logger.info("Type /pz_restore to recover vanilla, then /pz_install.")
    except Exception as e:
        logger.warning("Could not determine mod status: %s", e)


def _shutdown_gates(ctx) -> None:
    """Restore every installed code patch / detour on the way out (trigger birth-detour, the
    permit / placement / research-start hooks, and the presence-fill + terrain bytecode patches)."""
    for gate, method in ((ctx.trigger_source, "close"), (ctx.permit_gate, "shutdown"),
                         (ctx.facility_gate, "shutdown"), (ctx.research_gate, "shutdown"),
                         (ctx.presence_gate, "shutdown"), (ctx.terrain_gate, "shutdown")):
        if gate is not None:
            getattr(gate, method)()


# Keeps ctypes console-handler callbacks alive for the process lifetime (GC'ing one would
# silently unregister the handler).
_EMERGENCY_CLEANUP_REFS: list = []


def _install_emergency_cleanup(ctx) -> None:
    """Best-effort detour restore on abrupt exits the asyncio shutdown path misses - notably the
    Windows console **X button** (CTRL_CLOSE_EVENT) and Ctrl-C, which terminate the process without
    running ``await exit_event`` -> ``_shutdown_gates``. Detours are ALSO self-healed on the next
    attach (signatures.recover_leaked_hook), so this only narrows the window where the game is left
    patched. Idempotent: _shutdown_gates pops each hook, so a later clean shutdown is a no-op."""
    import atexit
    state = {"done": False}

    def _cleanup(*_args):
        if state["done"]:
            return
        state["done"] = True
        try:
            _shutdown_gates(ctx)
        except Exception:
            pass

    atexit.register(_cleanup)
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def _console_handler(_ctrl_type):
        _cleanup()      # restore detours before the OS tears us down (X button / Ctrl-C / logoff)
        return False    # not "handled" -> let default handling proceed to terminate the process

    cb = handler_type(_console_handler)
    _EMERGENCY_CLEANUP_REFS.append(cb)  # prevent GC (would unregister the handler)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True)
    except Exception:
        pass


def main(args=None):
    async def _run(args):
        ctx = PZContext(args.connect, args.password, data_path=getattr(args, "data", None))
        ctx.auth = args.name
        if args.memory:
            ctx.enable_memory(poll_interval=args.poll_interval)
            _install_emergency_cleanup(ctx)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")
        if args.memory:
            ctx._poll_task = asyncio.create_task(ctx.poll_loop(), name="pz poll loop")
        _log_mod_status()

        if gui_enabled:
            # gui_enabled is just "not --nogui"; the Kivy GUI may not be installed (this client ships
            # headless - no kivy). Fall back to the console UI instead of crashing on its import.
            try:
                ctx.run_gui()
            except ImportError as e:
                logger.info("GUI unavailable (%s) - running headless console. Pass --nogui to skip this.", e)
        ctx.run_cli()

        await ctx.exit_event.wait()
        _shutdown_gates(ctx)
        await ctx.shutdown()

    parser = get_base_parser(description="Planet Zoo Archipelago hooking client (Track A).")
    parser.add_argument("--name", default=None, help="Slot name to connect as.")
    parser.add_argument("--data", default=None, help="Path to data.json (defaults to project root).")
    parser.add_argument("--memory", dest="memory", action="store_true", default=True,
                        help="Attach to the running game: apply items + detect checks via memory (default ON).")
    parser.add_argument("--no-memory", dest="memory", action="store_false",
                        help="Console-only (A1): don't attach to the game - manual-trigger console for testing.")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="Seconds between memory poll ticks (default 1.0).")
    parser.add_argument("url", nargs="?", help="Archipelago connection url / address.")
    parsed = parser.parse_args(args)
    parsed = handle_url_arg(parsed, parser=parser)

    _prompt_missing(parsed)

    import colorama
    colorama.just_fix_windows_console()
    asyncio.run(_run(parsed))
    colorama.deinit()


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    main(sys.argv[1:])
