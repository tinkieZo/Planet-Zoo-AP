"""Offline tests for the ovl installer (pz_ap_client/ovl.py).

Everything runs against a fake game tree of tiny files - no game, no cobra-tools.
The builds are injected (``build=``/``build_pack=``) so install() is exercised
end-to-end: backup hygiene, the status state machine, the two-artifact deploy
(content pack + Content0 inject), stamp receipts, and failure rollback.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pz_ap_client import ovl  # noqa: E402

VANILLA = b"VANILLA-OVL-CONTENT" * 16
PATCHED = b"PATCHED-OVL-CONTENT" * 16
GM_VANILLA = b"GAMEMAIN-VANILLA-OVL" * 16
GM_PATCHED = b"GAMEMAIN-PATCHED-OVL" * 16


def _stems(src_names):
    """install() verifies built ovls contain every expected entry name (plaintext stems), so the
    fake artifacts must carry them - like the real ovl header does."""
    return b" ".join(Path(n).stem.lower().encode("ascii") for n in src_names)


PACK = b"PACK-OVL " + _stems(ovl.PACK_SRC_FILES)
LOC = b"LOC-OVL " + _stems(ovl.PACK_LOC_SRC_FILES)
LOC_LEAFS = (Path("English") / "UnitedKingdom", Path("German") / "Germany")


@pytest.fixture
def game(tmp_path, monkeypatch):
    """A fake Planet Zoo install with a vanilla Content0 Main.ovl (+ localisation
    leafs) and a vanilla GameMain Main.ovl; game not running."""
    game_dir = tmp_path / "Planet Zoo"
    ovl_path = game_dir / ovl.OVL_REL_PATH
    ovl_path.parent.mkdir(parents=True)
    ovl_path.write_bytes(VANILLA)
    for leaf in LOC_LEAFS:
        d = ovl_path.parent / "Localised" / leaf
        d.mkdir(parents=True)
        (d / "Loc.ovl").write_bytes(b"vanilla loc")
    gm = game_dir / ovl.GAMEMAIN_REL_PATH
    gm.parent.mkdir(parents=True)
    gm.write_bytes(GM_VANILLA)
    monkeypatch.setattr(ovl, "game_running", lambda: False)
    return game_dir


def fake_build(base: Path, out: Path, log) -> None:
    assert base.read_bytes() == VANILLA, "build must start from the vanilla backup"
    out.write_bytes(PATCHED)


def fake_build_pack(out: Path, log) -> None:
    out.write_bytes(PACK)


def fake_build_loc(out: Path, log) -> None:
    out.write_bytes(LOC)


def fake_build_gm(base: Path, out: Path, log) -> None:
    assert base.read_bytes() == GM_VANILLA, "GameMain build must start from the vanilla backup"
    out.write_bytes(GM_PATCHED)


def gamemain_paths(game_dir):
    gm = game_dir / ovl.GAMEMAIN_REL_PATH
    return gm, gm.with_name(gm.name + ovl.BACKUP_SUFFIX)


def paths(game_dir):
    return ovl._paths(game_dir)


def install(game_dir, **kw):
    kw.setdefault("log", lambda m: None)
    kw.setdefault("build", fake_build)
    kw.setdefault("build_pack", fake_build_pack)
    kw.setdefault("build_loc", fake_build_loc)
    kw.setdefault("build_gm", fake_build_gm)
    return ovl.install(game_dir, **kw)


# ---------------------------------------------------------------------------
# src tree / digest
# ---------------------------------------------------------------------------

def make_src(tmp_path):
    root = tmp_path / "src"
    (root / "pack").mkdir(parents=True)
    (root / "content0").mkdir()
    (root / "pack" / "a.lua").write_text("one")
    (root / "content0" / "b.lua").write_text("two")
    return root


def test_src_digest_tracks_content(tmp_path):
    root = make_src(tmp_path)
    first = ovl.src_digest(root)
    assert first == ovl.src_digest(root)
    (root / "pack" / "a.lua").write_text("changed")
    assert ovl.src_digest(root) != first


def test_src_digest_ignores_non_source_files(tmp_path):
    root = make_src(tmp_path)
    before = ovl.src_digest(root)
    (root / "README.md").write_text("docs change")
    (root / "pack" / "notes.md").write_text("scratch")
    assert ovl.src_digest(root) == before
    # .txt IS a source suffix (loc strings) - it must affect the digest.
    (root / "pack" / "x.txt").write_text("loc")
    assert ovl.src_digest(root) != before


def test_bundled_src_complete():
    # The bundled tree must contain exactly the manifests' source files.
    ovl.check_src_complete()
    assert tuple(f.name for f in ovl.src_files(ovl.src_dir() / "pack")) == ovl.PACK_SRC_FILES
    assert tuple(f.name for f in ovl.src_files(ovl.src_dir() / "pack_loc")) == ovl.PACK_LOC_SRC_FILES
    assert tuple(f.name for f in ovl.src_files(ovl.src_dir() / "content0")) == ovl.CONTENT0_SRC_FILES


def test_incomplete_src_fails_loudly(tmp_path, monkeypatch):
    incomplete = tmp_path / "src"
    (incomplete / "pack").mkdir(parents=True)
    (incomplete / "pack" / "scenarioscripts.scenario_ap_script.lua").write_text("x")
    monkeypatch.setattr(ovl, "src_dir", lambda: incomplete)
    with pytest.raises(ovl.OvlInstallError, match="archipelagocareerdata"):
        ovl.check_src_complete()


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def test_find_game_dir_env_override(game, monkeypatch):
    monkeypatch.setenv("PZAP_GAME_DIR", str(game))
    assert ovl.find_game_dir() == game
    monkeypatch.setenv("PZAP_GAME_DIR", str(game / "nope"))
    assert ovl.find_game_dir() is None


def test_steam_libraries_vdf_parse(tmp_path):
    root = tmp_path / "Steam"
    lib2 = tmp_path / "OtherLib"
    (root / "steamapps").mkdir(parents=True)
    lib2.mkdir()
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n'
        % str(lib2).replace("\\", "\\\\"), encoding="utf-8")
    libs = ovl._steam_libraries(root)
    assert root in libs and lib2 in libs


# ---------------------------------------------------------------------------
# status state machine
# ---------------------------------------------------------------------------

def test_status_no_game(monkeypatch):
    monkeypatch.setattr(ovl, "find_game_dir", lambda: None)
    st = ovl.status()
    assert st.state == "no-game"
    assert not st.can_install


def test_status_fresh_install_is_vanilla(game):
    st = ovl.status(game)
    assert st.state == "vanilla"
    assert st.can_install


def test_status_backup_matches_current_is_vanilla(game):
    _, backup, _ = paths(game)
    backup.write_bytes(VANILLA)
    assert ovl.status(game).state == "vanilla"


def test_status_backup_differs_no_stamp_is_ambiguous(game):
    _, backup, _ = paths(game)
    backup.write_bytes(b"some other vanilla")
    st = ovl.status(game)
    assert st.state == "ambiguous"
    assert not st.can_install


def test_status_stamp_states(game):
    st = install(game)
    assert st.state == "installed"
    ovl_path, _, stamp_path = paths(game)

    # Newer bundled sources -> stale.
    stamp = json.loads(stamp_path.read_text())
    stamp["src_digest"] = "0" * 64
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    assert ovl.status(game).state == "stale"
    stamp["src_digest"] = ovl.src_digest()
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    assert ovl.status(game).state == "installed"

    # Pack ovl tampered/missing -> stale.
    pack_ovl = game / ovl.PACK_REL_DIR / "Main.ovl"
    pack_ovl.write_bytes(b"tampered")
    assert ovl.status(game).state == "stale"
    pack_ovl.unlink()
    assert ovl.status(game).state == "stale"
    pack_ovl.write_bytes(PACK)
    assert ovl.status(game).state == "installed"

    # Steam verify reverted Content0 -> vanilla.
    ovl_path.write_bytes(VANILLA)
    assert ovl.status(game).state == "vanilla"

    # Game update replaced Content0 -> game-updated.
    ovl_path.write_bytes(b"a brand new game version")
    assert ovl.status(game).state == "game-updated"


# ---------------------------------------------------------------------------
# install / restore
# ---------------------------------------------------------------------------

def test_install_from_vanilla(game):
    st = install(game)
    ovl_path, backup, stamp_path = paths(game)
    assert st.state == "installed"
    assert ovl_path.read_bytes() == PATCHED
    assert backup.read_bytes() == VANILLA
    pack_dir = game / ovl.PACK_REL_DIR
    assert (pack_dir / "Main.ovl").read_bytes() == PACK
    assert ovl.PACK_NAME in (pack_dir / "Manifest.xml").read_text()
    # The loc ovl is mirrored into every language leaf Content0 ships.
    for leaf in LOC_LEAFS:
        assert (pack_dir / "Localised" / leaf / "Loc.ovl").read_bytes() == LOC
    # GameMain is patched (mechanic-research rename) + its own vanilla backup kept.
    gm, gm_backup = gamemain_paths(game)
    assert gm.read_bytes() == GM_PATCHED
    assert gm_backup.read_bytes() == GM_VANILLA
    stamp = json.loads(stamp_path.read_text())
    assert stamp["vanilla_sha256"] == ovl.hashlib.sha256(VANILLA).hexdigest()
    assert stamp["patched_sha256"] == ovl.hashlib.sha256(PATCHED).hexdigest()
    assert stamp["pack_sha256"] == ovl.hashlib.sha256(PACK).hexdigest()
    assert stamp["gamemain_sha256"] == ovl.hashlib.sha256(GM_PATCHED).hexdigest()
    assert stamp["src_digest"] == ovl.src_digest()


def test_reinstall_builds_from_backup_not_live(game):
    install(game)
    # Stale it and reinstall: the base passed to build must be the VANILLA
    # backup (fake_build asserts that), not the live patched file.
    _, _, stamp_path = paths(game)
    stamp = json.loads(stamp_path.read_text())
    stamp["src_digest"] = "0" * 64
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    assert install(game).state == "installed"


def test_install_refuses_while_game_running(game, monkeypatch):
    monkeypatch.setattr(ovl, "game_running", lambda: True)
    with pytest.raises(ovl.OvlInstallError, match="running"):
        install(game)


def test_install_refuses_ambiguous(game):
    _, backup, _ = paths(game)
    backup.write_bytes(b"differs from live")
    with pytest.raises(ovl.OvlInstallError, match="ambiguous"):
        install(game)


def test_install_failure_leaves_game_untouched(game):
    def short_build(base, out, log):
        out.write_bytes(b"x")  # way under the size sanity floor

    with pytest.raises(ovl.OvlInstallError, match="short"):
        install(game, build=short_build)
    ovl_path, _, stamp_path = paths(game)
    assert ovl_path.read_bytes() == VANILLA
    assert not stamp_path.exists()
    assert not (game / ovl.PACK_REL_DIR).exists()
    assert not list(ovl_path.parent.glob("*.apnew"))


def test_restore_round_trip(game):
    install(game)
    st = ovl.restore(game, log=lambda m: None)
    ovl_path, backup, stamp_path = paths(game)
    gm, gm_backup = gamemain_paths(game)
    assert st.state == "vanilla"
    assert ovl_path.read_bytes() == VANILLA
    assert gm.read_bytes() == GM_VANILLA   # GameMain reverted too
    assert backup.exists()          # kept, so install can run again
    assert gm_backup.exists()
    assert not stamp_path.exists()
    assert not (game / ovl.PACK_REL_DIR).exists()
    # And the cycle works again.
    assert install(game).state == "installed"


def test_gamemain_reinstall_builds_from_backup_not_live(game):
    install(game)
    # Stale + reinstall: the base passed to the GameMain build must be the VANILLA
    # backup (fake_build_gm asserts it), never the live patched GameMain.
    _, _, stamp_path = paths(game)
    stamp = json.loads(stamp_path.read_text())
    stamp["src_digest"] = "0" * 64
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    gm, _ = gamemain_paths(game)
    assert gm.read_bytes() == GM_PATCHED      # currently patched (ours)
    assert install(game).state == "installed"  # would raise in fake_build_gm if it built from the live file


def test_gamemain_missing_backup_when_installed_errors(game):
    install(game)
    _, gm_backup = gamemain_paths(game)
    gm_backup.unlink()
    _, _, stamp_path = paths(game)
    stamp = json.loads(stamp_path.read_text())
    stamp["src_digest"] = "0" * 64           # force a reinstall path
    stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
    with pytest.raises(ovl.OvlInstallError, match="GameMain vanilla backup is missing"):
        install(game)


def test_restore_without_backup_errors(game):
    with pytest.raises(ovl.OvlInstallError, match="backup"):
        ovl.restore(game, log=lambda m: None)


# ---------------------------------------------------------------------------
# cobra child plumbing
# ---------------------------------------------------------------------------

def test_apply_content0_fdb_edits(tmp_path):
    """Full-decouple gating: mint the gate rows in c0research; re-point all 12 barriers (by BoundaryType) +
    every mechanic content (by research-item id) onto its gate across c0modularscenery/c0trackedrides/
    c0blueprints; keep the basic RC/Workshop facility gating; leave unrelated research items alone."""
    import sqlite3
    d = tmp_path / "fdb"; d.mkdir()
    gate_id = {c: gid for c, (_n, gid) in zip(ovl._mechanic_content_names(), ovl._content_gates())}
    SAMPLE = {"FoodShopsPizzaPen": 9001, "AfricaThemeSetsBlueprintsL1": 9002, "TransportSteamTrainStation": 9003}
    for c in SAMPLE:
        assert c in gate_id, f"{c} not in data.json mechanic content - update the sample"

    cr = sqlite3.connect(d / ovl.RESEARCH_FDB)
    cr.execute("CREATE TABLE ResearchItemData (ResearchItem TEXT, ResearchItemID INTEGER, ResearchCategory TEXT,"
               " HoursToComplete REAL, Icon, MechanicItemIcon)")
    for n, i in (("GuestSpawner", 50000), ("ParkGate", 50001), ("ScenarioBlueprint01", 50002),
                 ("ScenarioBlueprint02", 50003)):
        cr.execute("INSERT INTO ResearchItemData VALUES (?,?,?,?,?,?)", (n, i, "NoneResearchable", 1.0, None, None))
    for c, i in SAMPLE.items():
        cr.execute("INSERT INTO ResearchItemData VALUES (?,?,?,?,?,?)", (c, i, "Mechanic", 1.0, None, None))
    cr.commit(); cr.close()
    bd = sqlite3.connect(d / ovl.HABITAT_BOUNDARY_FDB)
    bd.execute("CREATE TABLE Simulation (BoundaryType TEXT, ResearchPack INTEGER)")
    for bt in ("Hedge", "Glass_One_Way", "Concrete", "Null"):
        bd.execute("INSERT INTO Simulation VALUES (?, 0)", (bt,))
    bd.commit(); bd.close()
    ms = sqlite3.connect(d / ovl.MODULAR_SCENERY_FDB)
    ms.execute("CREATE TABLE Simulation (SceneryPartName TEXT, ResearchItemID INTEGER, ResearchPackID INTEGER)")
    # EnrichmentItem's ResearchPackID 16 collides (same low int) with a transport item id - the regression
    # the pack-id fix guards: the pack column must be left ALONE (re-pointing it unlocked enrichment globally).
    ms.executemany("INSERT INTO Simulation VALUES (?,?,?)", [
        ("PizzaPenShop", 9001, 0), ("RS_Room_4x4", 0, 0), ("WS_Room_4x4", 0, 0), ("Untouched", 8888, 0),
        ("EnrichmentItem", 0, 16)])
    ms.commit(); ms.close()
    tr = sqlite3.connect(d / ovl.TRACKEDRIDES_FDB)
    tr.execute("CREATE TABLE Simulation (RideType TEXT, ResearchPack INTEGER)")
    tr.execute("INSERT INTO Simulation VALUES ('SteamTrain', 9003)")
    tr.commit(); tr.close()
    bp = sqlite3.connect(d / ovl.BLUEPRINTS_FDB)
    bp.execute("CREATE TABLE PrebuiltBlueprints "
               "(BlueprintID INTEGER PRIMARY KEY, Title TEXT, File TEXT, ResearchItemIDs TEXT)")
    bp.executemany("INSERT INTO PrebuiltBlueprints VALUES (?,?,?,?)", [
        (1, "Africa Set", "africa.bp", "9002"), (2, "Small Research_Centre", "rc.bp", ""), (3, "Other", "o.bp", "777")])
    bp.commit(); bp.close()
    nt = sqlite3.connect(d / ovl.NOTIFICATIONS_FDB)
    nt.execute("CREATE TABLE Alerts (Name TEXT, SelectionContextType TEXT)")
    nt.executemany("INSERT INTO Alerts VALUES (?,?)", [
        ("NoResearchCentre", "SceneryPlacement"), ("NoWorkshop", "SceneryPlacement"),
        ("NoStaffCentre", "SceneryPlacement"), ("LowOnCash", "FinanceManagement")])
    nt.commit(); nt.close()

    ovl._apply_content0_fdb_edits(d, log=lambda m: None)

    cr = sqlite3.connect(d / ovl.RESEARCH_FDB)
    minted = dict(cr.execute("SELECT ResearchItem, ResearchItemID FROM ResearchItemData "
                             "WHERE ResearchItem LIKE 'ApGate%' OR ResearchItem LIKE 'ApBarrierGate%'").fetchall())
    cr.close()
    assert minted.get("ApBarrierGate2") == 50004
    assert minted.get("ApGateFoodShopsPizzaPen") == gate_id["FoodShopsPizzaPen"]
    bd = sqlite3.connect(d / ovl.HABITAT_BOUNDARY_FDB)
    packs = dict(bd.execute("SELECT BoundaryType, ResearchPack FROM Simulation").fetchall()); bd.close()
    assert packs == {"Hedge": 50002, "Glass_One_Way": 50004, "Concrete": 50007, "Null": 0}  # Null ungated
    ms = sqlite3.connect(d / ovl.MODULAR_SCENERY_FDB)
    rows = dict(ms.execute("SELECT SceneryPartName, ResearchItemID FROM Simulation").fetchall())
    packids = dict(ms.execute("SELECT SceneryPartName, ResearchPackID FROM Simulation").fetchall()); ms.close()
    assert rows["PizzaPenShop"] == gate_id["FoodShopsPizzaPen"]   # content 9001 re-pointed -> gate (ITEM col)
    assert rows["RS_Room_4x4"] == 50000 and rows["WS_Room_4x4"] == 50001  # facility rooms
    assert rows["Untouched"] == 8888   # unrelated research item left alone
    assert packids["EnrichmentItem"] == 16   # pack-id col NOT re-pointed (regression: re-pointing it
    #                                           unlocked enrichment globally - the pack-id namespace fix)
    tr = sqlite3.connect(d / ovl.TRACKEDRIDES_FDB)
    # trackedrides ResearchPack is a PACK-id column - also left vanilla (same fix). Transport pack-gated
    # content gating is deferred to the scenario-scoping rework; re-pointing the pack id would unlock it.
    assert tr.execute("SELECT ResearchPack FROM Simulation").fetchone()[0] == 9003   # unchanged
    tr.close()
    bp = sqlite3.connect(d / ovl.BLUEPRINTS_FDB)
    ids = dict(bp.execute("SELECT BlueprintID, ResearchItemIDs FROM PrebuiltBlueprints").fetchall()); bp.close()
    assert ids[1] == str(gate_id["AfricaThemeSetsBlueprintsL1"])   # blueprint content 9002 re-pointed
    assert ids[2] == "50000"   # RC blueprint: facility gate appended
    assert ids[3] == "777"     # unrelated untouched
    nt = sqlite3.connect(d / ovl.NOTIFICATIONS_FDB)
    ctx = dict(nt.execute("SELECT Name, SelectionContextType FROM Alerts").fetchall()); nt.close()
    # gated facilities' alerts lose their click-to-place action (the build-menu bypass); others keep theirs
    assert ctx == {"NoResearchCentre": "None", "NoWorkshop": "None",
                   "NoStaffCentre": "SceneryPlacement", "LowOnCash": "FinanceManagement"}


def test_add_noneresearchable_gates():
    """The GameMain topology edit interns the gate names: bumps ResearchRoot count by the gate count and adds
    one <research> entry per minted gate (so the c0research gate rows don't crash on load)."""
    xml = ('<ResearchRoot count="4" game="Planet Zoo">\n\t<levels pool_type="4">\n'
           '\t\t<research is_completed="0" is_entry_level="0" is_enabled="0" unk_3="0" unk_4="0">\n'
           '\t\t\t<item_name>GuestSpawner</item_name>\n\t\t</research>\n\t</levels>\n</ResearchRoot>\n')
    out = ovl._add_noneresearchable_gates(xml)
    n = len(ovl._all_gate_names())
    assert ('<ResearchRoot count="%d"' % (4 + n)) in out
    assert "<item_name>ApBarrierGate2</item_name>" in out
    assert "<item_name>ApGate" in out
    assert out.count("</research>") == 1 + n   # original GuestSpawner + n minted gates


def test_child_inject_content0_stages_one_dir_and_uses_input(tmp_path):
    """Regression (cross-drive crash on the build machine): the Content0 inject must stage the lua + edited
    fdbs into ONE dir and pass --input, NEVER -f. cobra's -f path runs os.path.commonpath() over the files,
    which raises 'Paths don't have the same drive' when the bundled lua (install drive) and the extracted
    fdbs (%TEMP%, usually C:) span drives. Also verifies the gating is applied to the staged fdbs."""
    import sqlite3
    luaf = tmp_path / "scenarioscripts.scenarioscriptutils.lua"
    luaf.write_text("-- script")
    captured = {}

    def write_vanilla_fdbs(out_dir):
        out = Path(out_dir)
        bd = sqlite3.connect(out / ovl.HABITAT_BOUNDARY_FDB)
        bd.execute("CREATE TABLE Simulation (BoundaryType TEXT, ResearchPack INTEGER)")
        bd.execute("INSERT INTO Simulation VALUES ('Hedge', 0)")
        bd.commit(); bd.close()
        ms = sqlite3.connect(out / ovl.MODULAR_SCENERY_FDB)
        ms.execute("CREATE TABLE Simulation (SceneryPartName TEXT, ResearchItemID INTEGER, ResearchPackID INTEGER)")
        ms.execute("INSERT INTO Simulation VALUES ('RS_Room_4x4', 0, 0)")
        ms.commit(); ms.close()
        bp = sqlite3.connect(out / ovl.BLUEPRINTS_FDB)
        bp.execute("CREATE TABLE PrebuiltBlueprints "
                   "(BlueprintID INTEGER PRIMARY KEY, Title TEXT, File TEXT, ResearchItemIDs TEXT)")
        bp.execute("INSERT INTO PrebuiltBlueprints VALUES (1, 'Workshop A', 'w.bp', '')")
        bp.commit(); bp.close()
        cr = sqlite3.connect(out / ovl.RESEARCH_FDB)
        cr.execute("CREATE TABLE ResearchItemData (ResearchItem TEXT, ResearchItemID INTEGER, "
                   "ResearchCategory TEXT, HoursToComplete REAL, Icon, MechanicItemIcon)")
        cr.commit(); cr.close()
        tr = sqlite3.connect(out / ovl.TRACKEDRIDES_FDB)
        tr.execute("CREATE TABLE Simulation (RideType TEXT, ResearchPack INTEGER)")
        tr.commit(); tr.close()
        nt = sqlite3.connect(out / ovl.NOTIFICATIONS_FDB)
        nt.execute("CREATE TABLE Alerts (Name TEXT, SelectionContextType TEXT)")
        nt.execute("INSERT INTO Alerts VALUES ('NoResearchCentre', 'SceneryPlacement')")
        nt.commit(); nt.close()

    class FakeCmd:
        @staticmethod
        def main(args):
            if args[0] == "extract":
                write_vanilla_fdbs(args[args.index("-o") + 1])
            elif args[0] == "inject":
                captured["args"] = list(args)
                in_dir = Path(args[args.index("-i") + 1])
                captured["staged"] = sorted(p.name for p in in_dir.iterdir())
                bd = sqlite3.connect(in_dir / ovl.HABITAT_BOUNDARY_FDB)
                captured["hedge"] = bd.execute(
                    "SELECT ResearchPack FROM Simulation WHERE BoundaryType='Hedge'").fetchone()[0]
                bd.close()

    ovl._child_inject_content0(FakeCmd, [luaf], str(tmp_path / "base.ovl"), str(tmp_path / "out.ovl"))
    assert "-i" in captured["args"] and "-f" not in captured["args"]   # --input, never -f (cross-drive safe)
    assert set(captured["staged"]) == {luaf.name, ovl.HABITAT_BOUNDARY_FDB, ovl.MODULAR_SCENERY_FDB,
                                       ovl.BLUEPRINTS_FDB, ovl.RESEARCH_FDB, ovl.TRACKEDRIDES_FDB,
                                       ovl.NOTIFICATIONS_FDB}
    assert captured["hedge"] == 50002   # barrier re-point applied to the staged fdb before inject


def test_child_argv_dev_and_frozen(monkeypatch):
    argv = ovl._child_argv("inject", Path("C:/cobra"), Path("C:/src"), Path("C:/out.ovl"), Path("C:/base.ovl"))
    assert argv[:3] == [sys.executable, "-m", "pz_ap_client.ovl"]
    assert argv[3:] == ["inject", "C:\\cobra", "C:\\src", "C:\\out.ovl", "C:\\base.ovl"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    argv = ovl._child_argv("new", Path("C:/cobra"), Path("C:/src"), Path("C:/out.ovl"), None)
    assert argv[:2] == [sys.executable, "--run-ovl-inject"]
    assert argv[2:] == ["new", "C:\\cobra", "C:\\src", "C:\\out.ovl"]


# --- build-pipeline hardening (Proton/Wine silent-drop regression, 2026-07-08) --------------------
# A Linux user's install produced a 408-byte pack: cobra's luacheck lint raised under Wine, add_files
# swallowed the per-file error, and every .lua was silently missing while cobra exited 0 + SUCCESS.

def test_verify_ovl_contents_passes_and_fails(tmp_path):
    names = ("database.foo.lua", "bar_settings.animalresearchstartunlockedsettings")
    good = tmp_path / "good.ovl"
    good.write_bytes(b"FRES...database.foo...bar_settings junk...")
    ovl._verify_ovl_contents(good, names, "content pack")  # all stems present - no raise
    bad = tmp_path / "bad.ovl"
    bad.write_bytes(b"FRES...bar_settings only...")
    with pytest.raises(ovl.OvlInstallError) as ei:
        ovl._verify_ovl_contents(bad, names, "content pack")
    assert "database.foo.lua" in str(ei.value)
    assert "missing 1 of its 2" in str(ei.value)


def test_run_cobra_child_fails_on_dropped_files(tmp_path, monkeypatch):
    """cobra exits 0 even when per-file loaders failed; the 'Could not create' log line must be
    treated as fatal, while benign start-up ERROR noise (Could not load DDS) must not."""
    monkeypatch.setattr(ovl, "find_cobra_dir", lambda: tmp_path)
    monkeypatch.setattr(ovl, "_ensure_oodle", lambda *a, **k: None)
    monkeypatch.setattr(ovl, "check_src_complete", lambda: None)

    def fake_child(lines):
        script = tmp_path / "fake_child.py"
        script.write_text("\n".join(f"print({line!r})" for line in lines), encoding="utf-8")
        monkeypatch.setattr(ovl, "_child_argv",
                            lambda op, cobra, src, out, base: [sys.executable, str(script)])

    # benign start-up noise + success -> no raise
    fake_child(["ERROR | Could not load DDS", "INFO | Creating OVL from x", "SUCCESS | Created OVL: y"])
    ovl._run_cobra_child("new", tmp_path, tmp_path / "out.ovl", None, lambda m: None)

    # a dropped file -> OvlInstallError naming it, despite exit 0 + SUCCESS
    fake_child(["ERROR | Could not load DDS", "ERROR | Could not create: database.foo.lua",
                "SUCCESS | Created OVL: y"])
    with pytest.raises(ovl.OvlInstallError) as ei:
        ovl._run_cobra_child("new", tmp_path, tmp_path / "out.ovl", None, lambda m: None)
    assert "Could not create: database.foo.lua" in str(ei.value)


def test_run_cobra_child_hides_benign_noise_only(tmp_path, monkeypatch):
    """Console filter: every known-benign round-trip warning is hidden behind one summary line,
    while unknown ERROR/WARNING lines - including the real 'Could not load pointers for ...' /
    'Could not load Oodle DLL, ...' failures the plugin-noise pattern must not swallow - surface."""
    monkeypatch.setattr(ovl, "find_cobra_dir", lambda: tmp_path)
    monkeypatch.setattr(ovl, "_ensure_oodle", lambda *a, **k: None)
    monkeypatch.setattr(ovl, "check_src_complete", lambda: None)

    benign = [
        "ERROR | Could not load DDS",
        "ERROR | Could not load VOXELSKIRT",
        "WARNING | bitarray module is not installed",
        "WARNING | crowdgoal.prefab can't find the original name of UnknownHash_557031118.enumnamer",
        "WARNING | Won't update hash unknownhash_2374363819",
        "WARNING | Could not map 14 fragments in STATIC, storing them for saving",
        "WARNING | Restoring 14 uncaught fragments to STATIC",
        "ERROR | Collecting default.frenderlodspec errored",
        "ERROR | Could not read array of 'UIntPair'",
        "WARNING | Missing sub-element 'next_research' on XML element 'research'",
    ]
    real = [
        "ERROR | Could not load pointers for STATIC - something went wrong before",
        "ERROR | Could not load Oodle DLL, requires Windows and 64bit python to run.",
        "WARNING | Missing sub-element 'name' on XML element 'research'",
        "ERROR | Collecting database.c0research.fdb errored",
    ]
    script = tmp_path / "fake_child.py"
    script.write_text("\n".join(f"print({line!r})" for line in benign + real), encoding="utf-8")
    monkeypatch.setattr(ovl, "_child_argv",
                        lambda op, cobra, src, out, base: [sys.executable, str(script)])
    seen = []
    ovl._run_cobra_child("new", tmp_path, tmp_path / "out.ovl", None, seen.append)
    for line in benign:
        assert line not in seen, "benign line leaked to the console: %s" % line
    for line in real:
        assert line in seen, "real problem was hidden: %s" % line
    assert any("%d known-benign" % len(benign) in m for m in seen), "summary line missing/wrong count"
