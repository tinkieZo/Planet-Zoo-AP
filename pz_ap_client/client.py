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
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .memory.triggers import MemoryTriggerSource

# Don't let Archipelago's ModuleUpdate prompt to pip-install missing optional
# deps (e.g. kivy) at import time - that blocks on input() in a headless run.
os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")

# GUI GL backend: default to ANGLE (Direct3D-backed GLES2) on Windows as a precaution against
# GPU-specific desktop-GL issues. ANGLE is the standard Windows reliability backend (Chromium uses it)
# and is verified working here on both NVIDIA and an AMD RX 6900 XT. Set BEFORE any kivy import (the
# backend is locked when kivy.core.window loads in run_gui). Override with KIVY_GL_BACKEND=glew.
# NOTE: the clean-machine "GUI closes on startup" bug was NOT the GL backend - it was a STALE C/C++
# RUNTIME bundled from the build machine (ucrtbase / MSVCP140 / VCRUNTIME140 too old for Python 3.13 /
# kivy / SDL2, which then fail to load when frozen); see docs/PACKAGING.md and the build-time GUI
# self-test (build-exe.ps1 runs `--selftest` -> _gui_selftest). Kept this ANGLE default as cheap
# insurance since it's verified harmless.
if sys.platform == "win32":
    os.environ.setdefault("KIVY_GL_BACKEND", "angle_sdl2")

# Diagnostics: PZAP_DEBUG=1 turns on full DEBUG logging (incl. Kivy's, which routes through Python
# logging) so a packaged GUI failure on another machine logs every init step. Must set KIVY_LOG_LEVEL
# before kivy imports - the Python log level alone won't surface Kivy DEBUG records Kivy never emits.
if os.environ.get("PZAP_DEBUG"):
    os.environ["KIVY_LOG_LEVEL"] = "debug"   # force (a bundled kivy config can pin it otherwise)
    os.environ["KIVY_NO_FILELOG"] = "0"

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
    # Bootstrap-fill floor: minimum live listings each autofill market is kept stocked to, so an
    # early-game tiny unlocked pool still offers a usable selection (see _reconcile_market_fill).
    MARKET_MIN_LISTINGS = 8

    # Max poll ticks to hold item application while the fresh-save signal (park age) is still
    # resolving, so a fresh zoo's starting-money baseline is written BEFORE any cash item lands on top
    # (see _apply_new_items / _fresh_signal_pending). After this many waits we apply anyway, so a
    # disabled/broken park-age anchor can never block items forever.
    FRESH_WAIT_MAX = 8

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
        self.market_gate = None  # SpeciesMarketGate (memory mode); restricts the habitat autofill market to unlocked species
        self.exhibit_gate = None  # ExhibitMarketGate (memory mode); same include-set gate for the exhibit-animal market
        self.reward_granter = None  # RewardGranter (memory mode); grants decoupled research rewards
        self._market_last_allowed = None  # last unlocked-species key set applied (re-apply only on change)
        self._market_last_applied_ids = frozenset()  # last resolved id set applied (re-apply if it grows as the research map loads)
        self._park_age = None  # ParkAgeReader (memory mode); reads park years-open to detect a fresh save
        self._session = None  # ApSessionDetector (memory mode); is the LOADED park the AP scenario?
        self._session_was_active = False  # was the AP park loaded last tick? (detect unload -> clean up market gates)
        self._scanner = None  # the shared MemoryScanner (memory mode)
        self._fresh_reset_done = False  # fresh handling ran for the CURRENT fingerprint episode (re-arms
        # when the baked-balance fingerprint disappears; stops a no-op handling refiring every tick)
        self._ovl_job_running = False  # one ovl install/restore at a time (see run_ovl_job)
        self._initial_applied: "Optional[int]" = None  # high-water mark at session start (drives re-award)
        self._fresh_wait_ticks = 0  # ticks spent holding item apply for the park-age (fresh) signal to land
        self._paused_at_idx: "Optional[int]" = None  # item idx we last logged a pause for; throttles the
        # "Pausing item application" line (a stuck cumulative item retries every tick - log once per episode)
        self._cum_warned: set = set()  # ledger kinds warned unresolved (once per episode; cleared on success)
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
        from .memory.market import SpeciesMarketGate, ExhibitMarketGate
        self.market_gate = SpeciesMarketGate(scanner, research=self.trigger_source.research)
        # The exhibit-animal market (ExhibitAnimalExchange @park+0x1C0) also autofills on this base, via
        # the same include-set mechanism at a different manager. The client applies the SAME unlocked-
        # species id set to both gates: the exhibit pool holds only exhibit species, so it self-filters
        # to the unlocked exhibit subset (habitat ids in the set match nothing in the exhibit pool).
        self.exhibit_gate = ExhibitMarketGate(scanner, research=self.trigger_source.research)
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
            if self._session_was_active:
                # The AP park just unloaded (Exit-to-Menu / loaded another save). Restore the market gates
                # NOW, while the exchange manager may still resolve: their repointed include-set would
                # otherwise be freed by the engine's teardown of its OWN buffer (a foreign-pointer free ->
                # crash on exit). Best-effort + race-aware (restore() re-resolves + skips if already gone).
                self._session_was_active = False
                self._on_park_unload()
            return
        self._session_was_active = True
        steps = (
            # preflight FIRST, before anything installs its detours: it's a read-only health check of the
            # game's PRISTINE code sites (orig bytes / AOB). Running it after our own hooks install made it
            # inspect our freshly-installed detours and misreport them as "leaked (prior unclean exit)" /
            # "broken" - false positives on every clean start. On a genuine prior crash the leaked detour is
            # still present here (we haven't reinstalled yet) so real leaks are still caught + then recovered
            # by the install path (resolve_hook -> recover_leaked_hook) in the steps that follow.
            ("preflight", self._run_preflight),        # once, on first attach: self-check patch-sensitive sites
            ("triggers", lambda: self.trigger_source.poll(self.effective_checked)),  # game events -> checks
            ("apply_items", self._apply_new_items),    # apply/retry received items (+ fresh-save re-award)
            ("permits", self._reconcile_permits),      # purchase gate = full received-permit set
            ("conservation", self._reconcile_conservation),  # release gate = (conservation received?)
            ("facilities", self._reconcile_facilities),    # placement gate = full received-facility set
            ("barriers", self._reconcile_barriers),        # barrier buildability = received Progressive count
            ("research", self._reconcile_research),        # research gate = (research facilities received?)
            ("facility_reveal", self._reconcile_facility_reveal),  # reveal RC/Workshop build items (fdb-hide)
            ("mechanic_content", self._reconcile_mechanic_content),  # shops/themes/blueprints/transport/staff/power gates
            ("rewards", self._reconcile_rewards),          # animal research_reward (enrichment...) = unlocked iff received
            ("terrain", self._reconcile_terrain),          # native terrain-tool greying = received tool set
            ("market", self._reconcile_market),            # scenario market stocked = unlocked species only
            ("market_fill", self._reconcile_market_fill),  # bootstrap fill: keep a minimum selection in both markets
        )
        first = not getattr(self, "_first_tick_done", False)
        if first:
            logger.info("AP client: setting up - resolving hooks, installing gates, applying items "
                        "(first tick after attach; may take a while)...")
        for name, step in steps:
            t0 = time.monotonic()
            try:
                step()
            except Exception:
                logger.exception("poll loop step %r failed", name)
            dt = time.monotonic() - t0
            if first and dt > 0.5:   # surface what's slow on the first tick (doubles as a profiler)
                logger.info("  ... %s: %.1fs", name, dt)
        if first:
            self._first_tick_done = True
            logger.info("AP client: READY - all gates active (%d items received so far).",
                        len(self.items_received))

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

    # Our scenario park-bin's baked starting cash (dollars; save metadata nCash=15000000 cents,
    # live-confirmed twice: injections started from cash 150000.0). A Year-1 park holding EXACTLY this
    # untouched balance is a park the client has never handled - the fresh-save fingerprint. Changes
    # only if we rebuild the shell with a different baked balance.
    BAKED_STARTING_CASH = 150000.0

    def _current_cash(self) -> "Optional[float]":
        """The park's current cash (dollars) via the anchor layer, or None (console mode / unresolved)."""
        anchors = getattr(self.applier, "anchors", None)
        scanner = getattr(self.applier, "scanner", None)
        if anchors is None or scanner is None:
            return None
        try:
            return anchors.read(scanner, "cash")
        except Exception:
            return None

    def _maybe_fresh_reset(self, seed: str, applied: int, years: "Optional[int]") -> int:
        """Detect a NEVER-HANDLED fresh save and run the fresh handling (starting money -> ledger reset ->
        the item re-award). FINGERPRINT detection: a park below the age threshold whose cash is EXACTLY the
        scenario's baked starting balance ($150k) - only a park the client never touched reads that, because
        the handling itself (starting-money write + ledger grant) immediately changes the balance. So this
        fires once per new save and never on a reconnect to an already-handled young zoo - INCLUDING the
        repeated-Year-1-restart case that permanently jammed the old fresh_pending maturity latch.
        Unknown age or unreadable cash => do nothing (fail safe: never a spurious re-award).
        LIMITATION: a brand-new scenario PLAYED (money spent/earned) before the client ever connects no
        longer reads the baked balance and is NOT detected - connect the client before playing a new
        scenario. (Fix-for-good: a script-planted per-park save id; needs an ovl rebuild.)"""
        from .memory.zoodate import FRESH_YEARS
        if years is None or years >= FRESH_YEARS:
            return applied
        cash = self._current_cash()
        if cash is None or abs(cash - self.BAKED_STARTING_CASH) > 0.005:
            self._fresh_reset_done = False   # fingerprint gone -> arm for the next new save
            return applied            # not the untouched baked balance -> already handled (or played)
        if self._fresh_reset_done:
            # Handling already ran this episode but was a visible no-op (e.g. the room's starting_money
            # equals the baked balance and no money items are received yet) - don't refire every tick.
            return applied
        # FRESH: set the room's starting cash baseline, zero the money ledger (so the cumulative
        # reconcile re-grants the FULL received sum on top of that baseline, as one delta), then re-run
        # the item list (unlocks are idempotent; cash/cc items are acknowledge-only - the ledger is the
        # money authority). Self-clearing: this handling changes the cash, so the fingerprint is gone
        # next tick. The legacy fresh_pending latch is kept up to date for state-file compatibility.
        logger.info("Fresh zoo detected (Year 1, %d years open, untouched baked $%s) - applying starting "
                    "money and re-awarding all received items", years, self.BAKED_STARTING_CASH)
        self._fresh_reset_done = True
        self._apply_starting_money()
        self.state.reset_granted(seed, self.slot)
        if applied > 0:
            self.state.set(seed, self.slot, 0)
            applied = 0
        self.state.set_fresh_pending(seed, self.slot, True)
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

    def _fresh_signal_pending(self) -> bool:
        """True while the fresh-save signal (park age) is EXPECTED but not yet resolved. The park-age
        anchor reads None on the first tick(s) and only lands a tick later; until it does we can't tell
        a fresh zoo (which needs its starting-money baseline set first) from an established one, so item
        application holds. False when park-age is disabled/unavailable (no signal to wait for) or once
        it resolves - so this never blocks indefinitely."""
        from .memory.zoodate import PARKAGE_ENABLED
        if not PARKAGE_ENABLED or self._park_age is None:
            return False
        return self._park_years() is None

    def _apply_new_items(self) -> None:
        if self.state is None or self.slot is None:
            return
        seed = self.seed_name or "unknown"
        # Hold item application until the fresh-save signal (park age) resolves, so a fresh zoo's
        # starting-money baseline (_apply_starting_money, inside _maybe_fresh_reset) is written BEFORE
        # any Cash Injection lands on top. Without this, the first tick reads park-age None -> the
        # fresh-reset no-ops -> items apply on the scenario's BAKED starting cash, and the baseline
        # write a tick later clobbers them (observed: cash climbs via injections, then snaps to $50k).
        # Only holds when there's something to apply and the signal is still pending; bounded by
        # FRESH_WAIT_MAX so a disabled/broken anchor degrades to applying anyway (old behaviour).
        if (self.state.get(seed, self.slot) < len(self.items_received)
                and self._fresh_signal_pending()
                and self._fresh_wait_ticks < self.FRESH_WAIT_MAX):
            self._fresh_wait_ticks += 1
            logger.info("apply: holding items for the fresh-save signal (park age) to resolve so "
                        "starting money lands first [%d/%d]", self._fresh_wait_ticks, self.FRESH_WAIT_MAX)
            return
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
                # Transient failure (e.g. game not attached / anchor not resolved yet): stop here, leave the
                # high-water mark, retry on the next event. Log ONCE per episode - this item stays at the head
                # of the queue and is re-attempted every poll tick, so logging each time floods the console.
                if idx != self._paused_at_idx:
                    logger.info("Pausing item application at #%s (%s); will retry quietly until it applies",
                                idx, item.name)
                    self._paused_at_idx = idx
                break
            self.state.set(seed, self.slot, idx + 1)
            self._paused_at_idx = None  # progress made; a later pause (new stuck item) logs again
        # Money is applied HERE, by ledger delta, not per-item above (cash/cc handlers acknowledge only).
        # Runs after the loop + fresh-reset so a fresh save's starting-money baseline landed first.
        self._reconcile_cumulative(seed)
        # Re-derive the purchase gate authoritatively right after applying, so a tool/permit
        # just received takes effect immediately (not only on the next poll tick), and any
        # transient over-unblock from a per-item unlock() is corrected within this call.
        self._reconcile_permits()

    # kind -> the anchor it reconciles through (data.json amounts are dollars; anchors handle the scale)
    _CUM_ANCHOR = {"cash": "cash", "cc": "conservation_credits"}
    # Room-driven money amounts: the APWorld options (slot_data) carry the MEDIUM amount per kind;
    # the item's size scales it - Small = half, Large = double (Options.FillerAmountsCash/-Conservation).
    _FILLER_OPTION = {"cash": "filler_amounts_cash", "cc": "filler_amounts_conservation"}
    _FILLER_MULT = {"small": 0.5, "medium": 1.0, "large": 2.0}

    def _money_amount(self, it) -> float:
        """One money item's value: the room's Medium base (slot_data option) scaled by the item's
        size, falling back to data.json's per-size default ``amount`` when the room doesn't carry
        the option (older room / console dev)."""
        size = (it.effect_args.get("size") or "").lower()
        base = (self.slot_data or {}).get(self._FILLER_OPTION[it.effect_type])
        if base is not None and size in self._FILLER_MULT:
            return round(base * self._FILLER_MULT[size], 2)
        return it.effect_args.get("amount", 0) or 0

    def _cumulative_targets(self) -> "Dict[str, float]":
        """{kind -> sum of amounts} over every received cash/cc item. The authoritative money target:
        game total granted must equal this, regardless of when/how items arrived."""
        totals = dict.fromkeys(self._CUM_ANCHOR, 0.0)
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type in totals:
                totals[it.effect_type] += self._money_amount(it)
        return totals

    def _reconcile_cumulative(self, seed: str) -> None:
        """Apply cash/cc by LEDGER DELTA: game value += (received sum) - (granted so far), then persist
        the new granted total. One addition per change instead of a per-item replay - a reconnect is a
        no-op (delta 0), a fresh save re-grants everything in one write (ledger was reset), a transient
        anchor failure just retries next tick, and the player's spending is never overwritten (we only
        ever ADD the missing delta to the current value)."""
        anchors = getattr(self.applier, "anchors", None)
        scanner = getattr(self.applier, "scanner", None)
        if anchors is None or scanner is None or self.state is None:
            return  # console mode (no game) - the acknowledge-only handlers already logged intent
        for kind, target in self._cumulative_targets().items():
            granted = self.state.get_granted(seed, self.slot, kind)
            delta = target - granted
            if delta <= 0:
                if delta < 0:
                    # State carries more than this room ever sent (e.g. a reused seed name with a smaller
                    # pool). Adopt the target; never subtract money from the player.
                    self.state.set_granted(seed, self.slot, kind, target)
                continue
            anchor = self._CUM_ANCHOR[kind]
            current = anchors.read(scanner, anchor)
            if current is None:
                if kind not in self._cum_warned:
                    self._cum_warned.add(kind)
                    logger.info("ledger: %s +%s pending - the %r anchor isn't resolved yet (no zoo "
                                "loaded / finance data not located); applies automatically once it is.",
                                kind, delta, anchor)
                continue
            if anchors.write(scanner, anchor, current + delta):
                self.state.set_granted(seed, self.slot, kind, target)
                self._cum_warned.discard(kind)
                logger.info("[apply] %s +%s (%s -> %s; ledger now %s granted of %s received)",
                            kind, delta, current, current + delta, target, target)

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

    def _reconcile_barriers(self) -> None:
        """Drive barrier buildability from the COUNT of received Progressive Barrier Level items (= N):
        status-write every RESEARCHABLE barrier of grade <= N to buildable (status 3). Buildability is
        status >= 3 while the barrier_N research LOCATION fires only at == 4, so this never falsely fires
        a check (live-confirmed). Idempotent + restart-correct, like the other reconciles. No-op if the
        seed grants no Progressive Barrier Level. (The 6 default barriers need a c0habitatboundary.fdb
        research-tag edit to be gateable - not handled yet.)"""
        if self.reward_granter is None:
            return
        levels = 0
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if (it is not None and it.effect_type == "progressive_research_reward"
                    and it.effect_args.get("family") == "barrier"):
                levels += 1
        if levels <= 0:
            return
        try:
            self.reward_granter.reconcile_barriers(levels)
        except Exception:
            logger.exception("barrier reconcile failed")

    def _reconcile_facility_reveal(self) -> None:
        """Reveal the Research Centre / Workshop build items (hidden via the c0modularscenery/c0blueprints
        fdb-hide) for received facility_unlock items - status-writes their NoneResearchable placeholder to 4
        (rewards.reconcile_facilities). This REPLACES the PresenceGate (the build item is now hidden until the
        AP item, not greyed). Idempotent + restart-correct, like the other reconciles."""
        if self.reward_granter is None:
            return
        keys = set()
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type == "facility_unlock":
                k = it.effect_args.get("facility_key")
                if k:
                    keys.add(k)
        if not keys:
            return
        try:
            self.reward_granter.reconcile_facilities(keys)
        except Exception:
            logger.exception("facility reveal reconcile failed")

    def _reconcile_mechanic_content(self) -> None:
        """Drive mechanic-research build content (shops/themes/blueprints/transport/staff/power) from the
        received research_reward items: status-write each mechanic content's re-pointed gate to buildable
        (rewards.reconcile_mechanic). The real research item stays the player-research location -> no false
        check (same decouple as barriers). ANIMAL research_reward content unlocks separately via grant()
        (rs+0x148) in the applier; reconcile_mechanic filters to mechanic content. Idempotent + restart-
        correct, like the other reconciles."""
        if self.reward_granter is None:
            return
        contents = [it.effect_args.get("content") for net_item in self.items_received
                    if (it := self.game_data.item_by_id.get(net_item.item)) is not None
                    and it.effect_type == "research_reward" and it.effect_args.get("content")]
        if not contents:
            return
        try:
            self.reward_granter.reconcile_mechanic(contents)
        except Exception:
            logger.exception("mechanic content reconcile failed")

    def _reconcile_rewards(self) -> None:
        """Authoritatively gate ANIMAL research rewards each tick (restart-correct). Two pools:
        (1) PER-CONTENT (enrichment items, etc.): unlocked byte = (its AP item received?). LOCKS content
            the base scenario bin pre-unlocks (e.g. basic enrichment baked into Scenario_22_Empty -
            research-locked in vanilla but the bin ships it unlocked) until its item arrives; the
            applier's grant() is ONE-WAY (only unlocks), so without this such content is free from start.
        (2) COUNT-BASED per-species LEVEL families (supplement/breeding/education/exhibit_enrichment):
            the family's N received copies unlock level <= N for EVERY species that has it.
        Mechanic content is excluded (gated at /pz_install)."""
        if self.reward_granter is None:
            return
        from .memory.rewards import LEVEL_FAMILIES, is_mechanic_content

        def _is_animal_rr(it) -> bool:
            return (it is not None and it.effect_type == "research_reward"
                    and bool(it.effect_args.get("content"))
                    and not is_mechanic_content(it.effect_args["content"]))

        universe = [i.effect_args["content"] for i in self.game_data.items if _is_animal_rr(i)]
        received = [it.effect_args["content"] for net_item in self.items_received
                    if _is_animal_rr(it := self.game_data.item_by_id.get(net_item.item))]
        try:
            self.reward_granter.reconcile_rewards(received, universe)
        except Exception:
            logger.exception("reward reconcile failed")
        # Count-based level families: the k-th received copy of the family's progressive item unlocks
        # level k for every species that has it (like barriers).
        counts = dict.fromkeys(LEVEL_FAMILIES, 0)
        for net_item in self.items_received:
            it = self.game_data.item_by_id.get(net_item.item)
            if it is not None and it.effect_type == "progressive_research_reward":
                fam = it.effect_args.get("family")
                if fam in counts:
                    counts[fam] += 1
        for fam, count in counts.items():
            try:
                self.reward_granter.reconcile_progressive_levels(fam, count)
            except Exception:
                logger.exception("%s level reconcile failed", fam)

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
        """Restrict BOTH animal markets (habitat LocalAnimalExchange + exhibit ExhibitAnimalExchange)
        to the unlocked species (and ONLY them). This base autofills both markets from candidate pools,
        so the client installs an include-set allow-list (the unlocked species) on each - routing the
        autofill rebuild to the default whitelist - and expires any already-listed blocked species, so
        future autofill only spawns unlocked species. The SAME id set goes to both gates: each pool
        holds only its own type, so the exhibit gate self-filters to unlocked exhibit species and the
        habitat gate to unlocked habitat species. Re-applied only when the unlocked set CHANGES (each
        apply forces a pool rebuild). No-op outside scenario mode (sandbox/franchise markets are
        engine-driven)."""
        if self.market_gate is None or not self.market_gate.scenario_mode():
            return
        received_ids = [ni.item for ni in self.items_received]
        allowed = self.game_data.purchase_universe() - self.game_data.purchase_blocked_species(received_ids)
        allowed_ids = self.market_gate._resolve_handles(sorted(allowed))
        if not allowed_ids and allowed:
            return  # research snapshot not ready yet - retry next tick (don't mark this set applied)
        applied_ids = frozenset(allowed_ids)
        # Re-apply when EITHER the unlocked set OR the resolved-id set changes. The resolved-id check
        # matters because the research map loads lazily: a species that's unlocked but unresolvable on
        # an early tick (partial snapshot) is dropped from the first allow-list, and must still be gated
        # in once it resolves - even though `allowed` (the key set) hasn't changed. A settled map yields
        # a stable resolved set, so this still avoids redundant pool rebuilds.
        if allowed == self._market_last_allowed and applied_ids == self._market_last_applied_ids:
            return
        if self.market_gate.apply_unlocked(allowed_ids):
            self._market_last_allowed = set(allowed)
            self._market_last_applied_ids = applied_ids
            self.market_gate.expire_blocked_listings(allowed_ids)  # clear stale blocked listings now
            # Same allow-list to the exhibit market (the pool there self-filters to exhibit species).
            if self.exhibit_gate is not None and self.exhibit_gate.scenario_mode():
                self.exhibit_gate.apply_unlocked(allowed_ids)
                self.exhibit_gate.expire_blocked_listings(allowed_ids)

    def _on_park_unload(self) -> None:
        """The AP park unloaded (Exit-to-Menu / loading another save). Restore the market gates' include-set
        to the engine's own buffer BEFORE the game's park teardown frees it - otherwise the engine frees our
        VirtualAllocEx buffer (a foreign pointer) and crashes. restore() re-resolves the manager and writes
        only if it still resolves, so this is safe whether the manager is still alive or already gone (in the
        latter case the crash window was already missed - nothing we can do from a ~1s poll). Resets the
        apply-cache so the gate re-applies if the AP scenario reloads. Detours are process-stable and
        self-heal on re-attach, so they're intentionally left installed."""
        logger.info("AP park unloaded - restoring market gates before the engine tears them down")
        for gate in (self.market_gate, self.exhibit_gate):
            if gate is not None:
                try:
                    gate.restore()
                except Exception:
                    logger.exception("market gate restore on park-unload failed")
        self._market_last_allowed = None        # force a fresh apply if the AP scenario reloads
        self._market_last_applied_ids = frozenset()

    def _reconcile_market_fill(self) -> None:
        """Bootstrap fill: each tick, keep both autofill markets stocked with a minimum selection so an
        early-game tiny unlocked pool isn't a near-empty market (the autofill target scales with pool
        size, so a 2-3 species start barely fills). Raises the target toward MARKET_MIN_LISTINGS, capped
        at ~2 listings per unlocked species so the ask stays reachable - which, with the engine's
        per-species spawn backoff, also tends toward ~one of each unlocked species. Soft + sanity-guarded
        + wake-throttled (see ensure_min_fill); only ever spawns from the gated pool, so it can't offer
        anything locked. No-op outside scenario mode."""
        for gate in (self.market_gate, self.exhibit_gate):
            if gate is not None and gate.scenario_mode():
                gate.ensure_min_fill(self.MARKET_MIN_LISTINGS)

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
            from .memory.hook import HookManager
            try:
                at = AnchorTable.load()
            except Exception:
                at = None
            # Reuse the session reader (already scanned + cached by _session_active, which fronts this
            # very tick) and the terrain gate, so the preflight doesn't repeat their ~20s full-heap
            # scans - the single biggest first-tick cost. See run_selfcheck / check_session / check_terrain.
            # installed_hooks tells the check which sites already hold OUR detour (e.g. the permit gate can
            # install during the Connected handler, before this first-tick preflight) so they aren't
            # misreported as leaked prior-crash detours.
            results = sig.run_selfcheck(
                scanner, at,
                session_reader=(self._session.names if self._session is not None else None),
                terrain_gate=self.terrain_gate,
                installed_hooks=HookManager.active_hooks(),
            )
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

    def _goal_ids_for(self, trigger: str, arg_key: str, wanted_keys) -> List[int]:
        """Location ids whose ``trigger_args[arg_key]`` matches one of ``wanted_keys``."""
        wanted = set(wanted_keys)
        if not wanted:
            return []
        return [loc.id for loc in self.game_data.locations_by_trigger(trigger)
                if loc.trigger_args.get(arg_key) in wanted]

    def _resolve_goal_locations(self) -> None:
        """Map slot_data.goal (research/species keys) onto our own location IDs."""
        goal = self.slot_data.get("goal") or {}
        gargs = goal.get("args", {})
        ids = (self._goal_ids_for("research_complete", "research_key", gargs.get("required_research", []))
               + self._goal_ids_for("first_breed", "species_key", gargs.get("required_breed", [])))
        self._goal_location_ids = sorted(set(ids))
        if not goal:
            return
        if self._goal_location_ids:
            logger.info("goal resolved: %d location(s) %s", len(self._goal_location_ids),
                        self._goal_location_ids)
        else:
            # Fail-loud: a goal that maps to no locations can never complete (_check_goal would just
            # silently never fire) - a species/research key mismatch between the apworld's slot_data
            # and our data.json.
            logger.error("goal %r resolved to NO locations - the goal can never complete! "
                         "slot_data keys don't match data.json (apworld/data.json out of sync?)", goal)

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


def _gui_selftest() -> int:
    """Load the full GUI native stack, print a parseable pass/fail marker, and return an exit code.

    BUILD-TIME GUARD for the silent "frozen GUI closes on startup" failure. That failure is a STALE C/C++
    runtime (ucrtbase / MSVCP140 / VCRUNTIME140) bundled from the build machine: Python 3.13 / kivy / SDL2
    fail to load against it and the process dies with no window and no traceback. The frozen exe uses its
    OWN BUNDLED runtime, so running ``pz-ap-client.exe --selftest`` on the build machine reproduces the
    target condition - build-exe.ps1 runs it after every build so a broken runtime is caught BEFORE shipping
    (which is how the original bug went undiagnosed for so long: nothing exercised the bundled runtime).

    Prints exactly one of ``PZAP_SELFTEST: OK`` / ``SKIP`` / ``FAIL``. A stale runtime can also hard-exit
    with NO marker at all, so the caller treats a missing OK/SKIP as failure too."""
    if not _gui_available():
        print("PZAP_SELFTEST: SKIP (no GUI bundled - console-only build)", flush=True)
        return 0
    os.environ.setdefault("KIVY_NO_ARGS", "1")  # don't let kivy try to parse our argv (--selftest)
    try:
        import kvui  # noqa: F401 - mirrors run_gui's import chain (kivy.app + kivymd)
        # Importing kivy.core.window CREATES the Window singleton, which loads the SDL2 / GL native
        # DLLs that link MSVCP140/VCRUNTIME140 - exactly the load that fails against a stale runtime.
        from kivy.core.window import Window
        if Window is None:
            print("PZAP_SELFTEST: FAIL (no window provider - kivy.core.window.Window is None)", flush=True)
            return 3
    except Exception as e:
        # A catchable GUI-load failure (e.g. ImportError / OSError from a DLL that won't load). A *fatally*
        # stale runtime can't be caught here at all - the process aborts with no Python exception; that case
        # is handled by build-exe.ps1 treating a missing OK/SKIP marker as failure. KeyboardInterrupt /
        # SystemExit deliberately propagate.
        import traceback
        print("PZAP_SELFTEST: FAIL (%r)" % (e,), flush=True)
        traceback.print_exc()
        return 3
    print("PZAP_SELFTEST: OK (GUI native stack loaded; window provider=%s)" % type(Window).__name__,
          flush=True)
    return 0


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
    """Restore every installed code patch / detour / market repoint on the way out (trigger birth+exhibit
    detours, the permit / placement / research-start hooks, the presence-fill + terrain bytecode patches,
    AND the two market gates - whose include-set repoint MUST be restored so the engine frees its own
    buffer, not our VirtualAllocEx allocation = the Exit-to-Menu/Exit-Game crash).

    Each restore is isolated: one gate raising MUST NOT abort the loop and leave the rest of the
    detours installed - a leaked detour forces the NEXT startup into a recovery scan (and, for the
    trigger birth-detour, can require a game restart to clear). Best-effort, never raises."""
    for gate, method in ((ctx.trigger_source, "close"), (ctx.permit_gate, "shutdown"),
                         (ctx.market_gate, "shutdown"), (ctx.exhibit_gate, "shutdown"),
                         (ctx.facility_gate, "shutdown"), (ctx.research_gate, "shutdown"),
                         (ctx.presence_gate, "shutdown"), (ctx.terrain_gate, "shutdown")):
        if gate is None:
            continue
        try:
            getattr(gate, method)()
        except Exception:
            logger.exception("shutdown: %s.%s() failed - other detours still restored", type(gate).__name__, method)


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


def _watch_gui_task(ctx) -> None:
    """Surface a crashed GUI task instead of hanging forever on ``exit_event.wait()`` with no window and
    no traceback. Observed on a Python 3.13 build: Kivy initialises through 'Clipboard: Provider', then
    the async UI task dies silently (the client is tested/shipped on 3.11.x). The done-callback logs the
    real exception and trips exit_event so the process exits cleanly (rerun with --nogui for the console
    UI). On a normal GUI close it just sets exit_event = ordinary shutdown."""
    task = getattr(ctx, "ui_task", None)
    if task is None:
        return

    def _done(t) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("GUI task crashed (%r) - exiting. Rerun with --nogui for the console UI.",
                         exc, exc_info=exc)
        else:
            logger.error("GUI task ENDED with no exception (window closed or the app stopped itself "
                         "before/at the main loop) - exiting.")
        ctx.exit_event.set()

    task.add_done_callback(_done)


_FILE_LOG_INSTALLED = False


def _setup_file_logging() -> "Optional[Path]":
    """Persist the client's console output to a rotating file so third-party users (who run the packaged
    exe with no console) have something to send when something breaks. Attaches a RotatingFileHandler to
    the ROOT logger - so it captures every child logger (PZClient, Client, the websockets debug, AP's
    FileLog/StreamLog) AND the /pz_install run, which shares this process. Lives next to the client state
    (%LOCALAPPDATA%\\PlanetZooAP\\logs\\pz_client.log; falls back to ~). INFO by default, DEBUG under
    PZAP_DEBUG. Idempotent (guards against a double install) and best-effort (never blocks startup if the
    dir/file can't be created). Returns the log path, or None on failure."""
    global _FILE_LOG_INSTALLED
    if _FILE_LOG_INSTALLED:
        return None
    import logging.handlers
    try:
        log_dir = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "PlanetZooAP" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "pz_client.log"
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        fh.setLevel(logging.DEBUG if os.environ.get("PZAP_DEBUG") else logging.INFO)
        root = logging.getLogger()
        # Ensure the root passes records to the handler even if main() runs outside the __main__ guard
        # (e.g. a packaged entry point) - the root is left at WARNING after the AP-import mute is restored.
        if root.level == logging.NOTSET or root.level > fh.level:
            root.setLevel(fh.level)
        root.addHandler(fh)
        _FILE_LOG_INSTALLED = True
        logging.getLogger("Client").info("Logging to %s", path)
        return path
    except Exception as e:
        # Logging must never crash the client; fall back to console-only.
        logging.getLogger("Client").warning("Could not set up file logging (%s) - console only", e)
        return None


def main(args=None):
    # Build-time GUI smoke test (build-exe.ps1 runs `--selftest`). Handle it before the normal startup
    # flow so it neither prompts for a connection nor spins up the asyncio client - it just loads the
    # GUI native stack and exits with a parseable marker. See _gui_selftest.
    _argv = list(sys.argv[1:] if args is None else args)
    if "--selftest" in _argv:
        sys.exit(_gui_selftest())
    _setup_file_logging()   # persist console output to a rotating file for third-party debugging

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
            else:
                _watch_gui_task(ctx)   # a silently-dying UI task must surface + exit, not hang
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
