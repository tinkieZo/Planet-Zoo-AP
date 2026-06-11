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
PACK = b"PACK-OVL"


@pytest.fixture
def game(tmp_path, monkeypatch):
    """A fake Planet Zoo install with a vanilla Main.ovl; game not running."""
    game_dir = tmp_path / "Planet Zoo"
    ovl_path = game_dir / ovl.OVL_REL_PATH
    ovl_path.parent.mkdir(parents=True)
    ovl_path.write_bytes(VANILLA)
    monkeypatch.setattr(ovl, "game_running", lambda: False)
    return game_dir


def fake_build(base: Path, out: Path, log) -> None:
    assert base.read_bytes() == VANILLA, "build must start from the vanilla backup"
    out.write_bytes(PATCHED)


def fake_build_pack(out: Path, log) -> None:
    out.write_bytes(PACK)


def paths(game_dir):
    return ovl._paths(game_dir)


def install(game_dir, **kw):
    kw.setdefault("log", lambda m: None)
    kw.setdefault("build", fake_build)
    kw.setdefault("build_pack", fake_build_pack)
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


def test_src_digest_ignores_non_lua(tmp_path):
    root = make_src(tmp_path)
    before = ovl.src_digest(root)
    (root / "README.md").write_text("docs change")
    (root / "pack" / "notes.txt").write_text("scratch")
    assert ovl.src_digest(root) == before


def test_bundled_src_complete():
    # The bundled tree must contain exactly the two manifests' .lua modules.
    ovl.check_src_complete()
    assert tuple(f.name for f in ovl.src_files(ovl.src_dir() / "pack")) == ovl.PACK_SRC_FILES
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
    stamp = json.loads(stamp_path.read_text())
    assert stamp["vanilla_sha256"] == ovl.hashlib.sha256(VANILLA).hexdigest()
    assert stamp["patched_sha256"] == ovl.hashlib.sha256(PATCHED).hexdigest()
    assert stamp["pack_sha256"] == ovl.hashlib.sha256(PACK).hexdigest()
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
    assert st.state == "vanilla"
    assert ovl_path.read_bytes() == VANILLA
    assert backup.exists()          # kept, so install can run again
    assert not stamp_path.exists()
    assert not (game / ovl.PACK_REL_DIR).exists()
    # And the cycle works again.
    assert install(game).state == "installed"


def test_restore_without_backup_errors(game):
    with pytest.raises(ovl.OvlInstallError, match="backup"):
        ovl.restore(game, log=lambda m: None)


# ---------------------------------------------------------------------------
# cobra child plumbing
# ---------------------------------------------------------------------------

def test_child_argv_dev_and_frozen(monkeypatch):
    argv = ovl._child_argv("inject", Path("C:/cobra"), Path("C:/src"), Path("C:/out.ovl"), Path("C:/base.ovl"))
    assert argv[:3] == [sys.executable, "-m", "pz_ap_client.ovl"]
    assert argv[3:] == ["inject", "C:\\cobra", "C:\\src", "C:\\out.ovl", "C:\\base.ovl"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    argv = ovl._child_argv("new", Path("C:/cobra"), Path("C:/src"), Path("C:/out.ovl"), None)
    assert argv[:2] == [sys.executable, "--run-ovl-inject"]
    assert argv[2:] == ["new", "C:\\cobra", "C:\\src", "C:\\out.ovl"]
