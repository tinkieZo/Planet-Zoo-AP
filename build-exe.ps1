# Build the distributable Planet Zoo AP client exe (PyInstaller one-dir). See docs/PACKAGING.md.
# Prereqs: a venv (Python 3.11.9+; .venv313 preferred over .venv when both are staged) with deps +
# pyinstaller, and an Archipelago tree to bundle. -VenvPath <dir> forces a specific venv.
#
#   .\build-exe.ps1                      # bundle .\vendor\Archipelago (default)
#   .\build-exe.ps1 -ApSource D:\Archipelago   # bundle an Archipelago install from elsewhere
#   $env:PZ_AP_SOURCE = 'D:\Archipelago'; .\build-exe.ps1   # same, via env var
param([string]$ApSource, [string]$CobraSource, [switch]$SkipSelfTest, [string]$VenvPath)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
# Which venv to freeze. -VenvPath overrides; otherwise prefer the staged Python 3.13 venv (.venv313)
# over the 3.11 one (.venv): 3.11.9 is the LAST Python 3.11 with a Windows installer (3.11.10-3.11.13
# are source-only), so AP's ModuleUpdate "Python ... has security issues" warning (it wants >=3.11.13)
# can only be cleared by building on 3.12/3.13. A relative path resolves against the repo root. (The
# whole script invokes the venv via `& $venvPy -m ...`, never a Scripts\*.exe launcher, so the venv
# also survives being moved.)
if (-not $VenvPath) {
    $VenvPath = @('.venv313', '.venv') |
        Where-Object { Test-Path (Join-Path $root (Join-Path $_ 'Scripts\python.exe')) } |
        Select-Object -First 1
    if (-not $VenvPath) { $VenvPath = '.venv' }   # neither staged: fall through to the error below
}
$venvDir = if ([System.IO.Path]::IsPathRooted($VenvPath)) { $VenvPath } else { Join-Path $root $VenvPath }
$venvPy = Join-Path $venvDir 'Scripts\python.exe'

if (-not (Test-Path $venvPy)) {
    Write-Error "venv Python not found at $venvPy. Set up the venv first (see docs/PACKAGING.md)."
    exit 1
}
# Verify PyInstaller is importable. We invoke it below as `python -m PyInstaller`, NOT via the
# Scripts\pyinstaller.exe launcher: that launcher embeds an ABSOLUTE path to the venv's python.exe, so it
# breaks if the venv/workspace is moved ("Fatal error in launcher: Unable to create process"). `python.exe`
# itself relocates fine (it resolves its base interpreter via pyvenv.cfg), so `-m` survives a move.
$prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& $venvPy -c "import PyInstaller" *> $null
$pyiOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEAP
if (-not $pyiOk) {
    Write-Error "PyInstaller not installed in the venv. Fix: `"$venvPy`" -m pip install pyinstaller (see docs/PACKAGING.md)."
    exit 1
}
Write-Host ("Build venv: $venvDir (Python " + (& $venvPy -c "import sys; print('%d.%d.%d' % sys.version_info[:3])").Trim() + ")")

# REFUSE a python-build-standalone interpreter (what `uv`/`uv venv` installs). PyInstaller builds
# without error against it but produces a BROKEN GUI exe - runs fine from source, but the frozen GUI
# dies on startup (confirmed on a clean machine 2026-06-19: a uv `cpython-3.13-...-none` build closed
# right after Kivy init; the identical spec+deps under a python.org interpreter worked). Detect it via
# the base interpreter's path and stop, since the resulting exe looks fine but isn't shippable.
$basePrefix = (& $venvPy -c "import sys; print(sys.base_prefix)").Trim()
if ($basePrefix -match '\\uv\\' -or $basePrefix -match 'cpython-.*-none') {
    Write-Error ("This .venv is built on a python-build-standalone interpreter (uv's Python):`n  $basePrefix`n" +
                 "PyInstaller produces a broken GUI exe with it. Recreate the venv with a python.org Python:`n" +
                 "  install Python from python.org (or 'winget install -e --id Python.Python.3.11 --scope user'),`n" +
                 "  then  py -3.11 -m venv .venv  and reinstall deps. See docs/PACKAGING.md.")
    exit 1
}

# Preflight: the GUI (kivy/kivymd) and the ovl installer (numpy, used by the vendored
# cobra-tools) are runtime-loaded, so the spec can only bundle what the venv has.
# Missing ones degrade the exe (console UI / no /pz_install) - warn with the fix.
$optional = @(
    @{ mod = 'kivy';   pip = 'kivy==2.3.1 kivy_deps.sdl2 kivy_deps.glew' },
    @{ mod = 'kivymd'; pip = '"kivymd @ git+https://github.com/kivymd/KivyMD@5ff9d0d"' },
    @{ mod = 'numpy';  pip = 'numpy' }
)
# (kivy logs to stderr on a SUCCESSFUL import; under -ErrorAction Stop PowerShell would
# treat that as a terminating error, so relax it around the import probes.)
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
foreach ($dep in $optional) {
    & $venvPy -c "import $($dep.mod)" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "$($dep.mod) not in .venv - exe will be degraded. Fix: .venv\Scripts\pip install $($dep.pip)"
    }
}
$ErrorActionPreference = $prevEAP

# Resolve the Archipelago source to bundle: -ApSource arg > $env:PZ_AP_SOURCE > the vendored clone.
if (-not $ApSource) { $ApSource = $env:PZ_AP_SOURCE }
if (-not $ApSource) { $ApSource = Join-Path $root 'vendor\Archipelago' }
if (-not (Test-Path (Join-Path $ApSource 'CommonClient.py'))) {
    Write-Error "No Archipelago tree at '$ApSource' (CommonClient.py not found). Pass -ApSource <dir>, set `$env:PZ_AP_SOURCE, or clone vendor/Archipelago (see docs/PACKAGING.md)."
    exit 1
}
$env:PZ_AP_SOURCE = (Resolve-Path $ApSource).Path
Write-Host "Bundling Archipelago from: $env:PZ_AP_SOURCE"

# Stage a TRIMMED cobra-tools into vendor\cobra-tools (the ovl installer's inject engine;
# shipped as on-disk data like the Archipelago tree). Source: -CobraSource arg >
# $env:PZ_COBRA_SOURCE > a cobra-tools-master checkout next to the repo. If none is found we
# auto-clone it (same self-vendoring as the Archipelago tree) so a build on a fresh machine is
# never silently shipped WITHOUT /pz_install (that was the "cobra tools not found" failure).
$cobraStage = Join-Path $root 'vendor\cobra-tools'
if (-not $CobraSource) { $CobraSource = $env:PZ_COBRA_SOURCE }
# A prior build in THIS shell sets $env:PZ_COBRA_SOURCE to the STAGE dir (so the PyInstaller child can locate
# cobra). Re-reading it as the SOURCE on the next build makes source==destination - and the Remove-Item below
# nukes the stage dir before robocopy runs, so robocopy finds no source (exit 16). Ignore the stale value when
# it points at the stage dir and fall back to a real checkout. (This was the "every other build crashes, retry
# works" nuisance: the failed build deletes the stage, forcing the next build to re-clone.)
if ($CobraSource -and ($CobraSource.TrimEnd('\', '/') -ieq $cobraStage.TrimEnd('\', '/'))) { $CobraSource = $null }
if (-not $CobraSource) { $CobraSource = Join-Path (Split-Path $root -Parent) 'cobra-tools-master' }
if (-not (Test-Path (Join-Path $CobraSource 'ovl_tool_cmd.py'))) {
    $autoClone = Join-Path (Split-Path $root -Parent) 'cobra-tools-master'
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "cobra-tools not found at '$CobraSource' - cloning OpenNaja/cobra-tools to '$autoClone'..."
        $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        git clone --depth 1 https://github.com/OpenNaja/cobra-tools.git $autoClone 2>&1 | Out-Null
        $ErrorActionPreference = $prevEAP
        if (Test-Path (Join-Path $autoClone 'ovl_tool_cmd.py')) { $CobraSource = $autoClone }
        else { Write-Warning "cobra-tools clone failed (network/git?) - building WITHOUT the ovl installer (/pz_install)." }
    } else {
        Write-Warning "git not found - cannot fetch cobra-tools; building WITHOUT the ovl installer (/pz_install)."
    }
}
if (Test-Path (Join-Path $CobraSource 'ovl_tool_cmd.py')) {
    Write-Host "Staging cobra-tools from: $CobraSource"
    if (Test-Path $cobraStage) { Remove-Item -Recurse -Force $cobraStage }
    # Only what the headless inject path needs: core code + the Planet Zoo hash tables.
    # (docs/gui/tests/codegen and the other games' constants are dead weight.)
    $skipDirs = @('.git', '.github', '.vscode', '__pycache__', 'codegen', 'docs', 'dumps',
                  'experimentals', 'gui', 'icons', 'logs', 'Modding', 'plugin', 'source',
                  'sql_commands', 'tests')
    robocopy $CobraSource $cobraStage /E /NFL /NDL /NJH /NJS /XD $skipDirs | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Error "robocopy failed staging cobra-tools (exit $LASTEXITCODE)."; exit 1 }
    # constants/: keep only the Planet Zoo tables.
    Get-ChildItem (Join-Path $cobraStage 'constants') -Directory |
        Where-Object { $_.Name -ne 'Planet Zoo' } |
        Remove-Item -Recurse -Force
    # Do NOT ship the proprietary Oodle DLL (oo2core_8_win64.dll, RAD/Epic). The client copies it from the
    # user's OWN Planet Zoo install at /pz_install (the game ships the byte-identical DLL). See ovl._ensure_oodle.
    $oodleDll = Join-Path $cobraStage 'modules\formats\utils\oodle\oo2core_8_win64.dll'
    if (Test-Path $oodleDll) {
        Remove-Item -Force $oodleDll
        Write-Host "  stripped proprietary Oodle DLL from the bundle (sourced from the user's game at /pz_install)."
    }
    $env:PZ_COBRA_SOURCE = $cobraStage
} else {
    Write-Warning "No cobra-tools at '$CobraSource' - building WITHOUT the ovl installer (/pz_install)."
}

# Run from the repo root so the spec's other relative paths (data.json, pz_ap_client\...) resolve.
# PyInstaller logs progress to stderr; under -ErrorAction Stop PowerShell would abort on the first
# such line, so relax it for the build call and trust the real exit code instead.
Push-Location $root
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $venvPy -m PyInstaller --noconfirm --clean (Join-Path $root 'pz-ap-client.spec')
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEAP
    Pop-Location
}
if ($code -ne 0) {
    Write-Error "PyInstaller failed (exit $code)."
    exit $code
}

$exe = Join-Path $root 'dist\pz-ap-client\pz-ap-client.exe'
$internal = Join-Path $root 'dist\pz-ap-client\_internal'
Write-Host "`nBuilt: $exe" -ForegroundColor Green

# Refresh the bundled VC++/UCRT runtime from System32 when System32's copy is NEWER. This is THE fix for
# the silent "frozen GUI closes on startup" bug: PyInstaller sometimes bundles a STALE msvcp140 / vcruntime
# / ucrtbase that a binary wheel or an old Windows SDK vendored into the build env, even when the machine's
# actual runtime (System32) is current - and that stale bundled copy is what fails to load Python 3.13 /
# kivy / SDL2 at frozen runtime. We can't always force a newer VC++ redist (if one is already installed the
# installer aborts with 0x80070666), so instead we ship the System32 copy the machine really runs with.
# Strictly version-gated, so it NEVER downgrades a bundle built on an up-to-date machine.
function Get-DllVersion([string]$path) {
    $vi = (Get-Item $path).VersionInfo
    return [version]::new($vi.FileMajorPart, $vi.FileMinorPart, $vi.FileBuildPart, $vi.FilePrivatePart)
}
$sys32 = Join-Path $env:WINDIR 'System32'
$runtimeDlls = @('ucrtbase.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
                 'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll', 'vcruntime140.dll',
                 'vcruntime140_1.dll', 'concrt140.dll', 'vccorlib140.dll')
$refreshed = 0
foreach ($dll in $runtimeDlls) {
    $bundled = Join-Path $internal $dll
    $sysDll = Join-Path $sys32 $dll
    if ((Test-Path $bundled) -and (Test-Path $sysDll)) {
        $bv = Get-DllVersion $bundled
        $sv = Get-DllVersion $sysDll
        if ($sv -gt $bv) {
            Copy-Item $sysDll $bundled -Force
            Write-Host ("  refreshed {0,-18} {1} -> {2}  (from System32)" -f $dll, $bv, $sv) -ForegroundColor Yellow
            $refreshed++
        }
    }
}
if ($refreshed -gt 0) {
    Write-Host ("Refreshed $refreshed bundled runtime DLL(s) from System32 (build env had stale copies).") -ForegroundColor Yellow
}

# Forensics: record the (post-refresh) bundled Microsoft C/C++ runtime versions in the build log, so a
# future "works on my build, not theirs" question is a one-line diff instead of a multi-day hunt.
Write-Host "Bundled C/C++ runtime (shipped in the exe):"
foreach ($dll in @('ucrtbase.dll', 'MSVCP140.dll', 'VCRUNTIME140.dll', 'VCRUNTIME140_1.dll')) {
    $p = Join-Path $internal $dll
    if (Test-Path $p) {
        $item = Get-Item $p
        Write-Host ("  {0,-18} v{1,-22} {2,10:N0} bytes" -f $dll, $item.VersionInfo.ProductVersion, $item.Length)
    } else {
        Write-Host ("  {0,-18} (not bundled - provided by the target OS/redist)" -f $dll)
    }
}

# GUI self-test: run the freshly built exe's `--selftest`, which loads the SDL2/GL native stack using the
# BUNDLED runtime. A stale runtime fails HERE (on the build machine) instead of silently on a user's PC.
# A stale runtime may hard-exit with no marker, so treat a missing OK/SKIP as failure. -SkipSelfTest
# bypasses it (e.g. a headless CI agent with no display, where Window creation can't succeed).
if ($SkipSelfTest) {
    Write-Warning "GUI self-test SKIPPED (-SkipSelfTest). The bundled runtime was not verified to load the GUI."
} else {
    Write-Host "`nGUI self-test (loading the bundled runtime's SDL2/GL stack)..."
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $stOut = & $exe --selftest 2>&1 | Out-String
    $ErrorActionPreference = $prevEAP
    if ($stOut) { Write-Host $stOut.Trim() }
    if ($stOut -match 'PZAP_SELFTEST: (OK|SKIP)') {
        Write-Host "GUI self-test passed." -ForegroundColor Green
    } else {
        $bar = ('=' * 78)
        Write-Host "`n$bar" -ForegroundColor Red
        Write-Host "GUI SELF-TEST FAILED - the built exe could not load its GUI stack." -ForegroundColor Red
        Write-Host "This is almost always a STALE C/C++ RUNTIME on THIS build machine (the exe bundles" -ForegroundColor Red
        Write-Host "it). Shipped as-is, the GUI will close silently on startup for users. Fix:" -ForegroundColor Red
        Write-Host "  1. Run Windows Update (refreshes the System32 UCRT)." -ForegroundColor Red
        Write-Host "  2. Install the latest Microsoft Visual C++ Redistributable (x64):" -ForegroundColor Red
        Write-Host "       https://aka.ms/vs/17/release/vc_redist.x64.exe" -ForegroundColor Red
        Write-Host "  3. Rebuild. (Details: docs/PACKAGING.md. Bypass this check with -SkipSelfTest.)" -ForegroundColor Red
        Write-Host $bar -ForegroundColor Red
        exit 1
    }
}

Write-Host "`nRun:   .\dist\pz-ap-client\pz-ap-client.exe <host:port> --name <slot> [--memory]"
exit 0
