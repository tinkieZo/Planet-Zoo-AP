# Build the distributable Planet Zoo AP client exe (PyInstaller one-dir). See docs/PACKAGING.md.
# Prereqs: .venv (Python 3.11.9+) with deps + pyinstaller, and an Archipelago tree to bundle.
#
#   .\build-exe.ps1                      # bundle .\vendor\Archipelago (default)
#   .\build-exe.ps1 -ApSource D:\Archipelago   # bundle an Archipelago install from elsewhere
#   $env:PZ_AP_SOURCE = 'D:\Archipelago'; .\build-exe.ps1   # same, via env var
param([string]$ApSource, [string]$CobraSource)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\pyinstaller.exe'
$venvPy = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Error "PyInstaller not found at $py. Set up the venv first (see docs/PACKAGING.md)."
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
# $env:PZ_COBRA_SOURCE > a cobra-tools-master checkout next to the repo. Skipped (with a
# warning) if none found - the exe then ships without /pz_install.
if (-not $CobraSource) { $CobraSource = $env:PZ_COBRA_SOURCE }
if (-not $CobraSource) { $CobraSource = Join-Path (Split-Path $root -Parent) 'cobra-tools-master' }
$cobraStage = Join-Path $root 'vendor\cobra-tools'
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
    & $py --noconfirm --clean (Join-Path $root 'pz-ap-client.spec')
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEAP
    Pop-Location
}
if ($code -eq 0) {
    $exe = Join-Path $root 'dist\pz-ap-client\pz-ap-client.exe'
    Write-Host "`nBuilt: $exe" -ForegroundColor Green
    Write-Host "Run:   .\dist\pz-ap-client\pz-ap-client.exe <host:port> --name <slot> [--memory]"
} else {
    Write-Error "PyInstaller failed (exit $code)."
}
exit $code
