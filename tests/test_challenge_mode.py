"""Game-free tests for Challenge-mode support: the market mode classifier (scenario / challenge /
franchise), the two AP-session kinds (scenario marker vs claimed Challenge zoo), ParkIdentity matching,
and the client's claim flow - claim a fresh zoo, refuse a played one unless forced, start over in a new
zoo, release - plus the claim-driven fresh handling (starting money + CCs, ledger reset, full re-award)
that replaces the scenario's baked-cash fingerprint in a Challenge zoo."""
from __future__ import annotations

import asyncio
import os
import struct
import sys
import types
from pathlib import Path

os.environ.setdefault("SKIP_REQUIREMENTS_UPDATE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client.client import PZContext  # noqa: E402
from pz_ap_client.memory import market as mk  # noqa: E402
from pz_ap_client.memory.session import AP_PARK_NAME, ApSessionDetector, ParkIdentity  # noqa: E402
from pz_ap_client.state import ClientState  # noqa: E402

MGR = 0x7000000


class _Mem:
    """Sparse byte memory with the read_bytes API the market gate uses."""

    def __init__(self):
        self._b: dict = {}

    def set(self, addr, value):
        self._b[addr] = value

    def read_bytes(self, addr, size):
        return bytes(self._b.get(addr + i, 0) for i in range(size))


def _gate(mode, franchise=0):
    mem = _Mem()
    mem.set(MGR + mk.OFF_MGR_MODE, mode)
    mem.set(MGR + mk.OFF_MGR_FRANCHISE, franchise)
    gate = mk.SpeciesMarketGate(mem, research=object())
    gate._mgr_cache = MGR
    return gate


# -- market mode ---------------------------------------------------------------------------------

def test_game_mode_classifies_challenge_vs_franchise():
    assert _gate(mk.MODE_SCENARIO).game_mode() == "scenario"
    assert _gate(mk.MODE_CHALLENGE).game_mode() == "challenge"
    assert _gate(mk.MODE_CHALLENGE, franchise=1).game_mode() == "franchise"
    assert _gate(mk.MODE_SANDBOX).game_mode() == "sandbox"


def test_gateable_modes_are_scenario_and_challenge():
    assert _gate(mk.MODE_SCENARIO).gateable_mode()
    assert _gate(mk.MODE_CHALLENGE).gateable_mode()
    assert not _gate(mk.MODE_SANDBOX).gateable_mode()
    assert not _gate(mk.MODE_OFF).gateable_mode()


def test_no_zoo_loaded_has_no_mode():
    gate = mk.SpeciesMarketGate(_Mem(), research=object())
    gate._mgr_cache = None
    gate.exchange_mgr = lambda: None
    assert gate.mode() is None
    assert gate.game_mode() is None
    assert not gate.gateable_mode()


# -- ParkIdentity --------------------------------------------------------------------------------

def test_identity_matches_on_shared_fields_only():
    ident = ParkIdentity(name="Temperate Zoo", geome="temperate", lat=51.5, lon=-0.12)
    assert ident.matches({"name": "Temperate Zoo", "geome": "temperate", "lat": 51.5, "lon": -0.12})
    assert ident.matches({"name": "Temperate Zoo"})                       # older binding: name only
    assert not ident.matches({"name": "Other Zoo"})
    assert not ident.matches({"name": "Temperate Zoo", "geome": "tundra"})
    assert not ident.matches({"name": "Temperate Zoo", "lat": 51.6})      # moved pin = another zoo
    assert not ident.matches(None)


def _f32_step(value, ulps):
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    return struct.unpack("<f", struct.pack("<I", bits + ulps))[0]


def test_identity_floats_compare_exactly():
    """One f32 step apart = a different zoo (that is the claim nonce); JSON keeps the f32 exact."""
    import json
    lat = _f32_step(49.0, 37)
    ident = ParkIdentity(name="Temperate Zoo", continent=1, lat=lat, lon=23.0)
    assert ident.matches(json.loads(json.dumps(ident.to_binding())))
    assert not ident.matches({"name": "Temperate Zoo", "continent": 1, "lat": 49.0, "lon": 23.0})


def test_identity_binding_drops_unread_fields():
    assert ParkIdentity(name="Z", geome="desert").to_binding() == {"name": "Z", "geome": "desert"}


# -- park-info reader: identity fields + claim nonce ---------------------------------------------

class _ParkMem:
    """One park-info instance (vtable, name string, continent/lat/lon) in a sparse byte memory, with
    the scanner API ParkNameReader uses. Layout as located live 2026-11-13."""
    BASE, INST, STR = 0x140000000, 0x13F9F8060, 0x2CCA73FD0

    def __init__(self, name="Temperate Zoo", continent=1, lat=49.0, lon=23.0):
        from pz_ap_client.memory import signatures as sig
        self.module_base = self.BASE
        self.b = bytearray(0x400)
        self.s = bytearray(0x40)
        struct.pack_into("<Q", self.b, 0, self.BASE + sig.PARKINFO_VTABLE_RVA)
        struct.pack_into("<Q", self.b, sig.PARKINFO_NAME_OFF, self.STR)
        struct.pack_into("<q", self.s, 0, len(name))
        self.s[0x14:0x14 + len(name)] = name.encode()
        # continent byte + the 3 varying bytes after it (live claim-time value 0x0676B0)
        struct.pack_into("<B3sff", self.b, 0x1F0, continent, b"\xb0\x76\x06", lat, lon)

    def _buf(self, addr):
        if self.INST <= addr < self.INST + len(self.b):
            return self.b, addr - self.INST
        return self.s, addr - self.STR

    def read_bytes(self, addr, n):
        buf, off = self._buf(addr)
        return bytes(buf[off:off + n])

    def write_bytes(self, addr, data):
        buf, off = self._buf(addr)
        buf[off:off + len(data)] = data

    def read_qword(self, addr):
        return struct.unpack("<Q", self.read_bytes(addr, 8))[0]

    def scan_heap_for_qword(self, _value, max_hits=64):
        return [self.INST]


def test_reader_reads_identity_fields():
    from pz_ap_client.memory.session import ParkNameReader
    mem = _ParkMem()
    reader = ParkNameReader(mem)
    ident = reader.read_identity()
    assert ident == ParkIdentity(name="Temperate Zoo", continent=1, lat=49.0, lon=23.0)
    mem.b[0x1F1:0x1F4] = b"\x00\x00\x00"          # the bytes after the continent change at runtime
    assert reader.read_identity() == ident           # ... and must not change the identity (live bug)


def test_stamp_pin_nonce_shifts_latitude_only():
    from pz_ap_client.memory.session import ParkNameReader
    mem = _ParkMem()
    reader = ParkNameReader(mem)
    before = reader.read_identity()
    after = reader.stamp_pin_nonce(37)
    assert after.lat == _f32_step(49.0, 37)               # 49.00014..., live-written value 0x42440025
    assert abs(after.lat - 49.0) < 1e-3
    assert (after.name, after.continent, after.lon) == (before.name, before.continent, before.lon)
    assert reader.read_identity() == after                 # what the next session check reads
    assert not before.matches(after.to_binding())          # the unstamped (pre-claim) zoo no longer matches


# -- session kinds --------------------------------------------------------------------------------

class _Names:
    def __init__(self, name):
        self.name = name

    def read(self):
        return self.name

    def read_identity(self):
        return ParkIdentity(name=self.name) if self.name else None


def _detector(name, mode, claimed=None):
    det = ApSessionDetector(None, game_mode=lambda: mode,
                            claim_match=lambda ident: claimed is not None and ident.matches(claimed))
    det.names = _Names(name)
    return det


def test_scenario_session_needs_marker():
    assert _detector(AP_PARK_NAME, "scenario").is_ap_session()
    assert not _detector("Goodwin House", "scenario").is_ap_session()


def test_challenge_session_needs_claim():
    det = _detector("Temperate Zoo", "challenge", claimed={"name": "Temperate Zoo"})
    assert det.is_ap_session()
    assert det.kind == "challenge"
    unclaimed = _detector("Temperate Zoo", "challenge")
    assert not unclaimed.is_ap_session()
    assert unclaimed.mode == "challenge"
    assert unclaimed.identity.name == "Temperate Zoo"


def test_franchise_and_sandbox_never_sessions():
    # even when named like the marker / like the claimed zoo
    assert not _detector(AP_PARK_NAME, "sandbox").is_ap_session()
    assert not _detector("Temperate Zoo", "franchise", claimed={"name": "Temperate Zoo"}).is_ap_session()


def test_marker_name_in_challenge_is_not_a_scenario_session():
    assert not _detector(AP_PARK_NAME, "challenge").is_ap_session()


# -- client claim flow ---------------------------------------------------------------------------

def _ctx(tmp_path, items=3):
    loop = asyncio.new_event_loop()

    async def make():
        return PZContext(None, None, state_dir=str(tmp_path))   # CommonContext needs a running loop
    ctx = loop.run_until_complete(make())
    ctx.slot, ctx.team, ctx.seed_name = 1, 0, "seed"
    ctx.state = ClientState.load("seed", 1, state_dir=tmp_path)
    ctx.items_received = [types.SimpleNamespace(item=0)] * items
    ctx.sent = []

    async def _send(msgs):
        ctx.sent.extend(msgs)
    ctx.send_msgs = _send
    ctx._spawn = loop.run_until_complete          # run fire-and-forget sends to completion, in order
    ctx.calls = []
    ctx._apply_starting_money = lambda: ctx.calls.append("money")
    ctx._apply_starting_ccs = lambda: ctx.calls.append("ccs")
    return ctx


def _load_park(ctx, name, fresh=True):
    """Simulate the poll thread seeing an unclaimed challenge park."""
    ctx._session = types.SimpleNamespace(kind=None, mode="challenge", identity=ParkIdentity(name=name))
    ctx._park_is_fresh = lambda: fresh
    ctx._update_claim_candidate()


def test_claim_fresh_zoo_binds_locally_and_on_server(tmp_path):
    ctx = _ctx(tmp_path)
    _load_park(ctx, "Temperate Zoo")
    msg = ctx.claim_zoo()
    assert msg.startswith("Claimed")
    assert ctx.state.get_zoo_binding("seed", 1)["name"] == "Temperate Zoo"
    assert ctx.stored_data["pz_zoo_0_1"]["name"] == "Temperate Zoo"
    sets = [m for m in ctx.sent if m.get("cmd") == "Set"]
    assert sets
    assert sets[0]["key"] == "pz_zoo_0_1"
    assert sets[0]["operations"][0]["value"]["name"] == "Temperate Zoo"
    assert ctx._claim_matches(ParkIdentity(name="Temperate Zoo"))
    assert ctx._claim_fresh_pending


def test_claim_binds_the_nonce_stamped_identity(tmp_path):
    """A same-name, same-spot restart (unstamped latitude) is NOT the claimed zoo."""
    ctx = _ctx(tmp_path)
    plain = ParkIdentity(name="Temperate Zoo", continent=1, lat=49.0, lon=23.0)
    stamped = ParkIdentity(name="Temperate Zoo", continent=1, lat=_f32_step(49.0, 5), lon=23.0)
    nonces = []

    def _stamp(n):
        nonces.append(n)
        return stamped
    ctx._session = types.SimpleNamespace(kind=None, mode="challenge", identity=plain,
                                         names=types.SimpleNamespace(stamp_pin_nonce=_stamp))
    ctx._park_is_fresh = lambda: True
    ctx._update_claim_candidate()
    assert ctx.claim_zoo().startswith("Claimed")
    assert len(nonces) == 1
    assert 1 <= nonces[0] <= 1023
    assert ctx._zoo_binding()["lat"] == stamped.lat
    assert ctx._claim_matches(stamped)
    assert not ctx._claim_matches(plain)


def test_claim_without_locatable_pin_binds_plain_identity(tmp_path):
    ctx = _ctx(tmp_path)
    plain = ParkIdentity(name="Temperate Zoo", continent=1, lat=49.0, lon=23.0)
    ctx._session = types.SimpleNamespace(kind=None, mode="challenge", identity=plain,
                                         names=types.SimpleNamespace(stamp_pin_nonce=lambda n: None))
    ctx._park_is_fresh = lambda: True
    ctx._update_claim_candidate()
    assert ctx.claim_zoo().startswith("Claimed")
    assert ctx._claim_matches(plain)


def test_claim_refuses_played_zoo_unless_forced(tmp_path):
    ctx = _ctx(tmp_path)
    _load_park(ctx, "Old Zoo", fresh=False)
    assert not ctx.claim_zoo().startswith("Claimed")
    assert ctx.state.get_zoo_binding("seed", 1) is None
    assert ctx.claim_zoo(force=True).startswith("Claimed")


def test_claim_waits_while_freshness_unknown(tmp_path):
    ctx = _ctx(tmp_path)
    _load_park(ctx, "Temperate Zoo", fresh=None)
    assert "couldn't confirm" in ctx.claim_zoo()


def test_claim_without_challenge_park(tmp_path):
    ctx = _ctx(tmp_path)
    ctx._session = types.SimpleNamespace(kind=None, mode="sandbox", identity=None)
    ctx._update_claim_candidate()
    assert "No unclaimed Challenge zoo" in ctx.claim_zoo()


def test_start_over_in_new_zoo_replaces_binding(tmp_path):
    ctx = _ctx(tmp_path)
    _load_park(ctx, "First Zoo")
    ctx.claim_zoo()
    ctx._claim_fresh_pending = False                       # first zoo got handled
    _load_park(ctx, "Second Zoo")                          # restart: a new fresh zoo
    assert ctx.claim_zoo().startswith("Claimed")
    assert ctx._zoo_binding()["name"] == "Second Zoo"
    assert not ctx._claim_matches(ParkIdentity(name="First Zoo"))
    assert ctx._claim_fresh_pending                        # re-award scheduled again


def test_server_binding_wins_over_local(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.state.set_zoo_binding("seed", 1, {"name": "Local Zoo"})
    assert ctx._zoo_binding()["name"] == "Local Zoo"
    ctx.stored_data["pz_zoo_0_1"] = {"name": "Claimed Elsewhere"}   # claimed on another PC
    assert ctx._zoo_binding()["name"] == "Claimed Elsewhere"


def test_unclaim_releases(tmp_path):
    ctx = _ctx(tmp_path)
    _load_park(ctx, "Temperate Zoo")
    ctx.claim_zoo()
    assert "Released" in ctx.unclaim_zoo()
    assert ctx._zoo_binding() is None
    assert "No Challenge zoo" in ctx.unclaim_zoo()


def test_claim_triggers_full_reaward_once(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.state.set("seed", 1, 3)                            # items applied to a previous zoo
    ctx.state.set_granted("seed", 1, "cash", 5000.0)
    _load_park(ctx, "Temperate Zoo")
    ctx.claim_zoo()
    ctx._session.kind = "challenge"                        # next tick: the claimed zoo is active
    assert ctx._fresh_park_verdict_cb() is True            # research guard sees a fresh park
    assert ctx._maybe_fresh_reset("seed", 3, 0) == 0       # full re-award from item 0
    assert ctx.calls == ["money", "ccs"]
    assert ctx.state.get("seed", 1) == 0
    assert ctx.state.get_granted("seed", 1, "cash") == 0.0
    assert ctx._fresh_park_verdict is True
    # the following ticks / a later reconnect: nothing pending, nothing re-awarded
    assert ctx._maybe_fresh_reset("seed", 3, 0) == 3
    assert ctx.calls == ["money", "ccs"]


def test_reconnect_to_claimed_zoo_is_established(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.state.set_zoo_binding("seed", 1, {"name": "Temperate Zoo"})
    ctx._session = types.SimpleNamespace(kind="challenge", mode="challenge",
                                         identity=ParkIdentity(name="Temperate Zoo"))
    assert ctx._fresh_park_verdict_cb() is False
    assert ctx._maybe_fresh_reset("seed", 3, 0) == 3
    assert ctx.calls == []
    assert ctx._fresh_park_verdict is False


def test_challenge_skips_baked_cash_fingerprint(tmp_path):
    """A claimed challenge zoo that happens to hold $150k must not be mistaken for a fresh scenario."""
    ctx = _ctx(tmp_path)
    ctx._session = types.SimpleNamespace(kind="challenge", mode="challenge", identity=None)
    ctx._current_cash = lambda: PZContext.BAKED_STARTING_CASH
    assert ctx._maybe_fresh_reset("seed", 3, 0) == 3
    assert ctx.calls == []


def test_park_is_fresh_ignores_ap_gate_items(tmp_path):
    ctx = _ctx(tmp_path)
    ctx._park_years = lambda: 0
    snap = ({100: (0x30, 0, 0, 7), PZContext.AP_GATE_ITEM_MIN + 2: (0, 0, 4, 3)}, {})
    ctx.trigger_source = types.SimpleNamespace(research=types.SimpleNamespace(_snapshot=lambda: snap))
    assert PZContext._park_is_fresh(ctx) is True            # only a client-written gate is complete
    snap[0][101] = (0x30, 1, 4, 7)                          # a real research completed
    assert PZContext._park_is_fresh(ctx) is False
    ctx._park_years = lambda: 2
    del snap[0][101]
    assert PZContext._park_is_fresh(ctx) is False           # past Year 1


# -- in-game rename + game restart (beta report 2026-11-13) ---------------------------------------

class _RenamableNames:
    """A loaded park whose name the player can change in-game; ``addr`` is its park-info instance."""

    def __init__(self, name, addr=0x5000, pin=(1, 49.0, 23.0)):
        self.name, self.last_addr, self.pin = name, addr, pin

    def read(self):
        return self.name

    def read_identity(self):
        if not self.name:
            return None
        c, lat, lon = self.pin
        return ParkIdentity(name=self.name, continent=c, lat=lat, lon=lon)


def _rename_detector(name, mode="scenario", claimed=None):
    det = ApSessionDetector(None, game_mode=lambda: mode[0],
                            claim_match=lambda ident: claimed is not None and ident.matches(claimed))
    det.names = _RenamableNames(name)
    return det


def test_renamed_scenario_zoo_stays_active():
    """Renaming the AP zoo in-game turned the client off (full market, no checks) until the next load."""
    mode = ["scenario"]
    det = _rename_detector(AP_PARK_NAME, mode)
    assert det.is_ap_session()
    det.names.name = "Send Monkeys"
    assert det.is_ap_session()
    assert det.kind == "scenario"
    assert det.is_ap_session(), "stays active on later ticks under the new name"


def test_rename_latch_needs_the_same_park():
    mode = ["scenario"]
    det = _rename_detector(AP_PARK_NAME, mode)
    assert det.is_ap_session()
    det.names.name, det.names.last_addr = "Other Zoo", 0x9000          # a different park-info instance
    assert not det.is_ap_session()
    det = _rename_detector(AP_PARK_NAME, mode)
    assert det.is_ap_session()
    det.names.name, det.names.pin = "Other Zoo", (2, 10.0, 5.0)         # same slot reused, other globe pin
    assert not det.is_ap_session()


def test_park_switch_through_menu_clears_the_latch():
    """Loading another park passes through a no-zoo tick; a same-address park after it is not the AP zoo."""
    mode = ["scenario"]
    det = _rename_detector(AP_PARK_NAME, mode)
    assert det.is_ap_session()
    mode[0] = None
    det.names.name = None
    assert not det.is_ap_session()
    mode[0] = "scenario"
    det.names.name = "Goodwin House"
    assert not det.is_ap_session()


def test_renamed_challenge_zoo_stays_active_and_flags_rebind():
    mode = ["challenge"]
    claimed = {"name": "Temperate Zoo", "continent": 1, "lat": 49.0, "lon": 23.0}
    det = _rename_detector("Temperate Zoo", mode, claimed)
    assert det.is_ap_session()
    assert det.renamed is None
    det.names.name = "Monkey Town"
    assert det.is_ap_session()
    assert det.kind == "challenge"
    assert det.renamed is not None
    assert det.renamed.name == "Monkey Town"


def test_rename_rebinds_the_claim_keeping_claim_time(tmp_path):
    ctx = _ctx(tmp_path)
    ctx._store_zoo_binding({"name": "Temperate Zoo", "continent": 1, "lat": 49.0, "lon": 23.0,
                            "claimed_at": 123})
    renamed = ParkIdentity(name="Monkey Town", continent=1, lat=49.0, lon=23.0)
    ctx._queue_rebind(renamed)
    assert ctx._pending_rebind == {"name": "Monkey Town", "continent": 1, "lat": 49.0, "lon": 23.0,
                                   "claimed_at": 123}
    ctx._store_zoo_binding(ctx._pending_rebind)
    assert renamed.matches(ctx._zoo_binding()), "the renamed zoo matches its claim after a reload"
    ctx._pending_rebind = None
    ctx._queue_rebind(None)
    assert ctx._pending_rebind is None


class _DeadScanner:
    def __init__(self, alive):
        self.alive, self.detached = alive, False

    attached = True

    def is_alive(self):
        return self.alive

    def detach(self):
        self.detached = True


def test_game_exit_rebuilds_the_memory_layer(tmp_path):
    """After a game restart the client kept the dead process handle forever (no checks, items held)."""
    ctx = _ctx(tmp_path)
    rebuilt = []
    ctx.enable_memory = lambda poll_interval: rebuilt.append(poll_interval)
    ctx._scanner = _DeadScanner(alive=True)
    assert ctx._game_exited() is False
    assert rebuilt == []
    dead = _DeadScanner(alive=False)
    ctx._scanner = dead
    ctx._first_tick_done = ctx._preflight_done = ctx._session_was_active = True
    assert ctx._game_exited() is True
    assert dead.detached
    assert rebuilt == [ctx.poll_interval]
    assert not ctx._first_tick_done
    assert not ctx._preflight_done
    assert not ctx._session_was_active
