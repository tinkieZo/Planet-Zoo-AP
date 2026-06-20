"""ovl - install / verify / restore the AP scenario shell (all-authored sources).

The shell is TWO artifacts, both built on the user's machine from
``pz_ap_client/ovl_src/`` via a vendored cobra-tools - the release never
contains Frontier data, and neither do the sources (the career entry is
ADDITIVE through the engine's content-pack hot-plug, the park settings are
minimal overrides on engine defaults):

  1. ``ovldata\\PZArchipelago\\`` - a standalone content pack (Manifest.xml +
     a small Main.ovl from ``ovl_src/pack/``, cobra ``new``, seconds): career
     entry, scenario script, park/objective settings.
  2. ``Content0\\Main.ovl`` - ONE module injected from ``ovl_src/content0/``
     (the scenarioscriptutils script-table hijack must REPLACE the vanilla
     file; cobra ``inject`` from the vanilla backup, ~minutes). Binary deltas
     were measured useless (cobra repacks the whole archive - 16 bytes of
     common prefix), hence building locally instead of shipping patches.

State machine (one ``status()`` call drives the whole UX):

    no-game       Planet Zoo install not found (set PZAP_GAME_DIR to override)
    vanilla       unpatched (fresh install, post-update, or Steam-verified)
    installed     our shell, current sources, pack intact
    stale         our shell, but ovl_src changed or the pack is missing/modified
    game-updated  a stamp exists but Content0 matches neither recorded hash
                  (game patch overwrote us) -> reinstall re-backs-up the new vanilla
    ambiguous     no stamp, but an .apbak exists that differs from the live ovl -
                  the live file may be modified; restore first, then install

Files next to the Content0 ``Main.ovl``:
    Main.ovl.apbak        vanilla backup (made before first patch, refreshed on game update)
    Main.ovl.apstamp.json install receipt: vanilla/patched/pack hashes + ovl_src digest

cobra runs in a SUBPROCESS (a crash inside it must not take the client down):
in dev, ``python -m pz_ap_client.ovl <op> <cobra> <src> <out> [base]``; frozen,
the exe re-invokes itself with ``--run-ovl-inject`` (see pz_client_main.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

STEAM_APP_ID = 703080  # Planet Zoo
MAIN_OVL_NAME = "Main.ovl"
OVL_REL_PATH = Path("win64") / "ovldata" / "Content0" / MAIN_OVL_NAME
BACKUP_SUFFIX = ".apbak"
STAMP_SUFFIX = ".apstamp.json"
COBRA_GAME_LABEL = "Planet Zoo"
# cobra-tools needs the proprietary Oodle compressor to read/write Planet Zoo ovls, and loads it from a
# FIXED path inside its own tree (oodle.py: os.path.dirname(__file__)/oo2core_8_win64.dll). We do NOT
# redistribute it (it's RAD/Epic's, shipped with the game) - the release bundles cobra-tools WITHOUT it
# and we copy it from the user's own Planet Zoo install at /pz_install (see _ensure_oodle).
OODLE_DLL_NAME = "oo2core_8_win64.dll"
OODLE_REL_IN_COBRA = Path("modules") / "formats" / "utils" / "oodle" / OODLE_DLL_NAME
# Steam browser-protocol launch with the intro-skip arg (the engine otherwise
# hard-falls-back to the Scenario_01 intro cinematic for our career entry).
LAUNCH_URL = f"steam://run/{STEAM_APP_ID}//-skipScenarioIntro/"

# The AP shell is TWO artifacts:
#  1. A standalone content pack ovldata\PZArchipelago\ (Manifest.xml + a small
#     Main.ovl built from ovl_src/pack/ with cobra `new`, seconds): career entry,
#     scenario script, park/objective settings - all additive, the engine
#     discovers packs by folder scan.
#  2. One module injected into Content0\Main.ovl from ovl_src/content0/ (the
#     scenarioscriptutils script-table hijack - it must REPLACE the vanilla file).
PACK_NAME = "PZArchipelago"
PACK_REL_DIR = Path("win64") / "ovldata" / PACK_NAME
PACK_MANIFEST = f"""<ContentPack version="1">
  <Name>{PACK_NAME}</Name>
  <ID>e58f7b9c-1d2a-4a77-8c3e-aa90b4f1c2d7</ID>
  <Version>1</Version>
  <Type>Game</Type>
</ContentPack>
"""

LogFn = Callable[[str], None]

# The complete shell - all authored. Everything checks this manifest and fails
# loudly on an incomplete source tree (see ovl_src/README.md).
# FRESH-START ANIMAL RESEARCH CONFIG (D2/c-define, live-confirmed 2026-06-20): the engine binds a
# scenario's animal start-unlocked state by CODE (vanilla scenario_NN configs; careerdata has no research
# field). Our code "Scenario_15_Empty" had no config -> fell back to `default` (sandbox: all vet research
# pre-done). We ship an EMPTY one so animal/vet research starts at level 0 (no welfare false-fires).
#   MECHANIC research is keyed by a separate per-scenario type, scenario_<code>.scenariomechanicresearchsettings
#   (RE'd via headless Ghidra). We tried shipping an empty one (cobra handler added,
#   modules/formats/SCENARIOMECHANICRESEARCHSETTINGS.py) but it CRASHED on scenario load 2026-06-20: the
#   format is NOT a plain ResearchRoot (extract hit a UIntPair BufferError on one vanilla file), so cobra's
#   ResearchRoot-based create produced an INVALID file. Reverted. Needs the exact struct before re-attempting.
# Order MUST equal sorted src_files (test_bundled_src_complete).
PACK_SRC_FILES = (
    "database.archipelagocareerdata.lua",
    "database.pzarchipelagoluadatabase.lua",
    "objectivesettings.scenario_ap_objectives.lua",
    "parksettings.scenario_ap_parksettings.lua",
    "scenario_15_empty.animalresearchstartunlockedsettings",
    "scenarioscripts.scenario_ap_script.lua",
)
# Loc keys referenced by the careerdata (one .txt per key; built into a Loc.ovl
# that install() mirrors into every language leaf Content0 ships - the career UI
# localises title/label/description, plain strings render empty).
PACK_LOC_SRC_FILES = (
    "frontendmenu_scenariodetails_scenario_ap.txt",
    "frontendmenu_scenarioname_scenario_ap.txt",
    "frontendmenu_scenariotitle_scenario_ap.txt",
)
CONTENT0_SRC_FILES = (
    "scenarioscripts.scenarioscriptutils.lua",
)
SRC_SUFFIXES = (".lua", ".txt", ".animalresearchstartunlockedsettings")


# ---------------------------------------------------------------------------
# Hashing / bundled sources
# ---------------------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                return h.hexdigest()
            h.update(block)


def src_dir() -> Path:
    """The bundled authored ovl sources. Works in dev and frozen (the spec ships
    ``pz_ap_client/ovl_src`` at the same package-relative path)."""
    return Path(__file__).resolve().parent / "ovl_src"


def src_files(directory: Path) -> "List[Path]":
    """The source files of one shell subdir (sorted). Only SRC_SUFFIXES are
    built/digested - the tree also holds a README that must never reach an ovl."""
    if not directory.is_dir():
        return []
    return sorted(f for f in directory.iterdir() if f.is_file() and f.suffix in SRC_SUFFIXES)


def check_src_complete() -> None:
    """Fail loudly when the bundled shell sources are incomplete."""
    root = src_dir()
    missing = []
    for sub, names in (("pack", PACK_SRC_FILES), ("pack_loc", PACK_LOC_SRC_FILES),
                       ("content0", CONTENT0_SRC_FILES)):
        have = {f.name for f in src_files(root / sub)}
        missing += [f"{sub}/{n}" for n in names if n not in have]
    if missing:
        raise OvlInstallError("Shell sources incomplete - missing: %s "
                              "(see pz_ap_client/ovl_src/README.md)." % ", ".join(missing))


def src_digest(directory: Optional[Path] = None) -> str:
    """Stable digest over the authored sources (both subdirs) - the 'shell
    version'. A reinstall is needed exactly when this differs from the stamp."""
    root = directory or src_dir()
    h = hashlib.sha256()
    for sub in sorted(d.name for d in root.iterdir() if d.is_dir()):
        for f in src_files(root / sub):
            h.update(f"{sub}/{f.name}".encode("utf-8"))
            h.update(b"\x00")
            h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Game / tool discovery
# ---------------------------------------------------------------------------

def _steam_root() -> Optional[Path]:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path, _ = winreg.QueryValueEx(key, "SteamPath")
        root = Path(path)
        return root if root.is_dir() else None
    except OSError:
        return None


def _steam_libraries(root: Path) -> List[Path]:
    """All Steam library roots (including the main one) from libraryfolders.vdf.
    Minimal VDF parse: every  "path"  "X:\\\\lib"  line."""
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    if vdf.is_file():
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p.is_dir() and p not in libs:
                    libs.append(p)
        except OSError:
            pass
    return libs


def find_game_dir() -> Optional[Path]:
    """Locate the Planet Zoo install. Order: PZAP_GAME_DIR env override, then the
    Steam library that has appmanifest_703080.acf."""
    env = os.environ.get("PZAP_GAME_DIR")
    if env:
        p = Path(env)
        return p if (p / OVL_REL_PATH).is_file() else None
    root = _steam_root()
    if root is None:
        return None
    for lib in _steam_libraries(root):
        if (lib / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf").is_file():
            game = lib / "steamapps" / "common" / "Planet Zoo"
            if (game / OVL_REL_PATH).is_file():
                return game
    return None


def find_cobra_dir() -> Optional[Path]:
    """Locate cobra-tools: PZAP_COBRA_DIR env, the shipped vendor copy, or (dev)
    a cobra-tools-master checkout next to the repo."""
    candidates = []
    env = os.environ.get("PZAP_COBRA_DIR")
    if env:
        candidates.append(Path(env))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "vendor" / "cobra-tools")
    candidates.append(repo_root.parent / "cobra-tools-master")
    for c in candidates:
        if (c / "ovl_tool_cmd.py").is_file():
            return c
    return None


def _ensure_oodle(cobra: Path, game_dir: Optional[Path] = None) -> None:
    """Make sure cobra-tools has the Oodle compressor it loads at import time. The release ships
    cobra-tools WITHOUT the proprietary ``oo2core_8_win64.dll`` (we don't own redistribution rights);
    instead we copy it from the user's OWN Planet Zoo install, which provides the byte-identical DLL,
    into cobra's fixed path on first use. Idempotent - a no-op once the DLL is in place."""
    target = cobra / OODLE_REL_IN_COBRA
    if target.is_file():
        return
    game_dir = game_dir or find_game_dir()
    if game_dir is None:
        raise OvlInstallError(
            "Planet Zoo install not found - it provides the Oodle compressor (%s) the ovl builder needs. "
            "Set PZAP_GAME_DIR if your install isn't auto-detected." % OODLE_DLL_NAME)
    src = game_dir / OODLE_DLL_NAME
    if not src.is_file():
        raise OvlInstallError(
            "%s not found in the Planet Zoo install (%s) - the game provides the Oodle compressor the ovl "
            "builder needs (a game update may have changed it)." % (OODLE_DLL_NAME, game_dir))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    except OSError as e:
        raise OvlInstallError(
            "Could not place the Oodle compressor at %s (%s). If the client is in a protected folder "
            "(e.g. Program Files), move it somewhere writable and retry." % (target, e)) from e
    logger.debug("Oodle: copied %s -> %s", src, target)


def game_running() -> bool:
    try:
        import pymem
        pymem.Pymem("PlanetZoo.exe")
        return True
    except Exception:
        return False


def launch_game() -> None:
    # Fixed steam:// protocol URL (no user input reaches the shell).
    os.startfile(LAUNCH_URL)  # noqa: S606


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@dataclass
class OvlStatus:
    state: str          # no-game | vanilla | installed | stale | game-updated | ambiguous
    detail: str
    game_dir: Optional[Path] = None

    @property
    def can_install(self) -> bool:
        return self.state in ("vanilla", "stale", "game-updated", "installed")


def _paths(game_dir: Path) -> "tuple[Path, Path, Path]":
    ovl = game_dir / OVL_REL_PATH
    return ovl, ovl.with_name(ovl.name + BACKUP_SUFFIX), ovl.with_name(ovl.name + STAMP_SUFFIX)


def _read_stamp(stamp_path: Path) -> Optional[dict]:
    try:
        return json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _status_stamped(game_dir: Path, current: str, stamp: dict) -> OvlStatus:
    """The post-install branch of status(): compare against the stamp receipt."""
    if current == stamp.get("patched_sha256"):
        if stamp.get("src_digest") != src_digest():
            return OvlStatus("stale", "AP shell installed, but a newer shell ships with this client - reinstall.",
                             game_dir)
        pack_ovl = game_dir / PACK_REL_DIR / MAIN_OVL_NAME
        if not pack_ovl.is_file() or sha256_file(pack_ovl) != stamp.get("pack_sha256"):
            return OvlStatus("stale", "The PZArchipelago content pack is missing or modified - reinstall.",
                             game_dir)
        return OvlStatus("installed", "AP scenario shell is installed and current.", game_dir)
    if current == stamp.get("vanilla_sha256"):
        return OvlStatus("vanilla", "Vanilla ovl (the AP shell was removed, e.g. by Steam file verification).",
                         game_dir)
    return OvlStatus("game-updated", "Main.ovl matches neither stamped hash - a game update likely replaced it. "
                                     "Install will re-backup the new vanilla and rebuild.", game_dir)


def status(game_dir: Optional[Path] = None) -> OvlStatus:
    game_dir = game_dir or find_game_dir()
    if game_dir is None:
        return OvlStatus("no-game", "Planet Zoo install not found (set PZAP_GAME_DIR to override).")
    ovl, backup, stamp_path = _paths(game_dir)
    current = sha256_file(ovl)
    stamp = _read_stamp(stamp_path)
    if stamp:
        return _status_stamped(game_dir, current, stamp)
    # No stamp: pre-stamp dev deploys / first run.
    if backup.is_file():
        if current == sha256_file(backup):
            return OvlStatus("vanilla", "Vanilla ovl (matches the existing backup).", game_dir)
        return OvlStatus("ambiguous", "No install stamp, but Main.ovl differs from the existing .apbak backup - "
                                      "it may already be modified. Restore vanilla first, then install.", game_dir)
    return OvlStatus("vanilla", "No AP install detected - treating the current ovl as vanilla.", game_dir)


# ---------------------------------------------------------------------------
# Install / restore
# ---------------------------------------------------------------------------

class OvlInstallError(RuntimeError):
    pass


def _ensure_vanilla_backup(st: OvlStatus, ovl: Path, backup: Path, stamp_path: Path,
                           log: LogFn) -> str:
    """Make sure ``backup`` holds the vanilla ovl and return its hash."""
    if st.state in ("installed", "stale"):
        # Current ovl is ours; the vanilla source of truth is the backup.
        if not backup.is_file():
            raise OvlInstallError("AP shell installed but the vanilla backup is missing - "
                                  "verify game files in Steam, then install again.")
        stamp = _read_stamp(stamp_path) or {}
        vanilla_hash = sha256_file(backup)
        if stamp.get("vanilla_sha256") not in (None, vanilla_hash):
            raise OvlInstallError("The vanilla backup no longer matches the install stamp - "
                                  "verify game files in Steam, then install again.")
        return vanilla_hash
    # vanilla / game-updated: the live ovl IS the (new) vanilla - (re)back it up.
    log("Backing up vanilla Main.ovl (one-time per game version, ~350 MB)...")
    shutil.copyfile(ovl, backup)
    return sha256_file(backup)


def _loc_leafs(game: Path) -> "List[Path]":
    """Content0's Localised language/region leaf dirs (the ones holding a Loc.ovl)
    - the set of languages this install ships; our pack mirrors it."""
    root = game / "win64" / "ovldata" / "Content0" / "Localised"
    if not root.is_dir():
        return []
    return sorted(p.parent.relative_to(root) for p in root.rglob("Loc.ovl"))


def _build_and_deploy(game: Path, ovl: Path, backup: Path, log: LogFn,
                      build: Callable[[Path, Path, LogFn], None],
                      build_pack: Callable[[Path, LogFn], None],
                      build_loc: Callable[[Path, LogFn], None]) -> Path:
    """Build ALL artifacts to temp paths, then deploy. A failure leaves the
    install untouched. The pack/loc builds are fast; do them first.
    Returns the pack dir."""
    pack_dir = game / PACK_REL_DIR
    pack_tmp = ovl.with_name("pack.ovl.apnew")
    loc_tmp = ovl.with_name("loc.ovl.apnew")
    out = ovl.with_name(ovl.name + ".apnew")
    try:
        log("Building the PZArchipelago content pack (career entry + scenario)...")
        build_pack(pack_tmp, log)
        if not pack_tmp.is_file() or pack_tmp.stat().st_size == 0:
            raise OvlInstallError("Content-pack build produced no output - aborting (game files untouched).")
        log("Building the pack localisation (menu name/description)...")
        build_loc(loc_tmp, log)
        if not loc_tmp.is_file() or loc_tmp.stat().st_size == 0:
            raise OvlInstallError("Localisation build produced no output - aborting (game files untouched).")
        log("Building the patched Content0 ovl from your install (takes a few minutes)...")
        build(backup, out, log)
        if not out.is_file() or out.stat().st_size < ovl.stat().st_size // 2:
            raise OvlInstallError("Inject produced no/short output - aborting (game files untouched).")
        log("Deploying...")
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "Manifest.xml").write_text(PACK_MANIFEST, encoding="utf-8")
        # Same (English) strings into every language leaf the install ships.
        for leaf in _loc_leafs(game):
            dest = pack_dir / "Localised" / leaf
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(loc_tmp, dest / "Loc.ovl")
        os.replace(pack_tmp, pack_dir / MAIN_OVL_NAME)
        os.replace(out, ovl)
    finally:
        for tmp in (pack_tmp, loc_tmp, out):
            if tmp.is_file():
                tmp.unlink()
    return pack_dir


def install(game_dir: Optional[Path] = None, log: LogFn = logger.info,
            build: Optional[Callable[[Path, Path, LogFn], None]] = None,
            build_pack: Optional[Callable[[Path, LogFn], None]] = None,
            build_loc: Optional[Callable[[Path, LogFn], None]] = None) -> OvlStatus:
    """Back up vanilla (if needed), build the shell artifacts from the bundled
    sources, and deploy: the PZArchipelago content pack + its localisation
    (fast, additive) and the Content0 inject (the scriptutils hijack, ~minutes).
    ``build``/``build_pack``/``build_loc`` are injectable for tests."""
    st = status(game_dir)
    if st.state == "no-game":
        raise OvlInstallError(st.detail)
    if not st.can_install:
        raise OvlInstallError(f"Cannot install from state '{st.state}': {st.detail}")
    if game_running():
        raise OvlInstallError("Planet Zoo is running - close the game first.")
    game = st.game_dir
    assert game is not None
    ovl, backup, stamp_path = _paths(game)
    vanilla_hash = _ensure_vanilla_backup(st, ovl, backup, stamp_path, log)
    pack_dir = _build_and_deploy(game, ovl, backup, log,
                                 build or build_patched, build_pack or build_pack_ovl,
                                 build_loc or build_loc_ovl)
    stamp = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "vanilla_sha256": vanilla_hash,
        "patched_sha256": sha256_file(ovl),
        "pack_sha256": sha256_file(pack_dir / MAIN_OVL_NAME),
        "src_digest": src_digest(),
    }
    stamp_path.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    log("AP scenario shell installed.")
    return status(game)


def restore(game_dir: Optional[Path] = None, log: LogFn = logger.info) -> OvlStatus:
    """Put the vanilla backup back and remove the PZArchipelago content pack.
    Keeps the backup so install() can run again."""
    st = status(game_dir)
    if st.state == "no-game":
        raise OvlInstallError(st.detail)
    if game_running():
        raise OvlInstallError("Planet Zoo is running - close the game first.")
    game = st.game_dir
    assert game is not None
    ovl, backup, stamp_path = _paths(game)
    if not backup.is_file():
        raise OvlInstallError("No vanilla backup found - nothing to restore "
                              "(verify game files in Steam to recover vanilla).")
    log("Restoring vanilla Main.ovl from backup...")
    shutil.copyfile(backup, ovl)
    pack_dir = game / PACK_REL_DIR
    if pack_dir.is_dir():
        log("Removing the PZArchipelago content pack...")
        shutil.rmtree(pack_dir)
    if stamp_path.is_file():
        stamp_path.unlink()
    log("Vanilla restored.")
    return status(game)


# ---------------------------------------------------------------------------
# The inject subprocess
# ---------------------------------------------------------------------------

def _child_argv(op: str, cobra: Path, src: Path, out: Path, base: Optional[Path]) -> List[str]:
    """Argv for the cobra child. Frozen: the exe re-invokes itself with the
    sentinel flag (handled in pz_client_main.py). Dev: run this module."""
    tail = [op, str(cobra), str(src), str(out)] + ([str(base)] if base else [])
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-ovl-inject"] + tail
    return [sys.executable, "-m", "pz_ap_client.ovl"] + tail


def _run_cobra_child(op: str, src: Path, out: Path, base: Optional[Path], log: LogFn) -> None:
    """Run one cobra-tools operation in a crash-isolated subprocess."""
    cobra = find_cobra_dir()
    if cobra is None:
        raise OvlInstallError("cobra-tools not found (vendor/cobra-tools missing and PZAP_COBRA_DIR unset).")
    _ensure_oodle(cobra)  # copy the Oodle compressor from the user's game (not redistributed) before cobra imports it
    check_src_complete()
    argv = _child_argv(op, cobra, src, out, base)
    logger.debug("cobra child: %s", argv)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace",
                            cwd=str(Path(__file__).resolve().parent.parent))
    assert proc.stdout is not None
    tail: List[str] = []
    for line in proc.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-25:]
        logger.debug("cobra: %s", line)
        # Surface the slow archive passes so the user sees progress, not a hang.
        if any(k in line for k in ("Loading archive", "Injecting", "Saving archive",
                                   "Injected files", "Created OVL", "Creating OVL")):
            log(line)
    rc = proc.wait(timeout=3600)
    if rc != 0:
        raise OvlInstallError("cobra-tools %s failed (exit %s). Last output:\n%s" % (op, rc, "\n".join(tail)))


def build_patched(base: Path, out: Path, log: LogFn = logger.info) -> None:
    """cobra inject: vanilla Content0 ovl + ovl_src/content0 -> patched ovl at ``out``."""
    _run_cobra_child("inject", src_dir() / "content0", out, base, log)


def build_pack_ovl(out: Path, log: LogFn = logger.info) -> None:
    """cobra new: ovl_src/pack -> the PZArchipelago content-pack Main.ovl at ``out``."""
    _run_cobra_child("new", src_dir() / "pack", out, None, log)


def build_loc_ovl(out: Path, log: LogFn = logger.info) -> None:
    """cobra new: ovl_src/pack_loc -> the pack's Loc.ovl at ``out``."""
    _run_cobra_child("new", src_dir() / "pack_loc", out, None, log)


def _inject_child_main(argv: List[str]) -> int:
    """Child-process entry: bootstrap the vendored cobra-tools and run one op.
    Isolated in its own process so a cobra crash can't take the client down.
    Passes the .lua modules explicitly (not --input <dir>) so stray files
    (READMEs etc.) can never reach an ovl."""
    op, cobra, src, out = argv[:4]
    base = argv[4] if len(argv) > 4 else None
    # Resolve before the chdir below - callers may pass relative paths.
    out = str(Path(out).resolve())
    base = str(Path(base).resolve()) if base else None
    lua = [f.resolve() for f in src_files(Path(src).resolve())]
    sys.path.insert(0, cobra)
    os.chdir(cobra)  # cobra resolves its hash tables / logs relative to its root
    import ovl_tool_cmd  # noqa: E402
    if op == "new":
        # `new` only takes an input dir - stage the .lua files into a clean temp
        # dir so nothing else can be packed.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="pzap_pack_") as tmp:
            for f in lua:
                shutil.copyfile(f, Path(tmp) / f.name)
            ovl_tool_cmd.main(["new", "-g", COBRA_GAME_LABEL, "-i", tmp, "-o", out, "--force"])
    else:
        args = ["inject", "-g", COBRA_GAME_LABEL, "-o", out, base]
        for f in lua:
            args += ["-f", str(f)]
        ovl_tool_cmd.main(args)
    return 0


if __name__ == "__main__":
    sys.exit(_inject_child_main(sys.argv[1:]))
