# Build the distributable Planet Zoo AP client exe (PyInstaller one-dir). See docs/PACKAGING.md.
# Prereqs: .venv (Python 3.11.9+) with deps + pyinstaller, and an Archipelago tree to bundle.
#
#   .\build-exe.ps1                      # bundle .\vendor\Archipelago (default)
#   .\build-exe.ps1 -ApSource D:\Archipelago   # bundle an Archipelago install from elsewhere
#   $env:PZ_AP_SOURCE = 'D:\Archipelago'; .\build-exe.ps1   # same, via env var
param([string]$ApSource)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\pyinstaller.exe'

if (-not (Test-Path $py)) {
    Write-Error "PyInstaller not found at $py. Set up the venv first (see docs/PACKAGING.md)."
    exit 1
}

# Resolve the Archipelago source to bundle: -ApSource arg > $env:PZ_AP_SOURCE > the vendored clone.
if (-not $ApSource) { $ApSource = $env:PZ_AP_SOURCE }
if (-not $ApSource) { $ApSource = Join-Path $root 'vendor\Archipelago' }
if (-not (Test-Path (Join-Path $ApSource 'CommonClient.py'))) {
    Write-Error "No Archipelago tree at '$ApSource' (CommonClient.py not found). Pass -ApSource <dir>, set `$env:PZ_AP_SOURCE, or clone vendor/Archipelago (see docs/PACKAGING.md)."
    exit 1
}
$env:PZ_AP_SOURCE = (Resolve-Path $ApSource).Path
Write-Host "Bundling Archipelago from: $env:PZ_AP_SOURCE"

# Run from the repo root so the spec's other relative paths (data.json, pz_ap_client\...) resolve.
Push-Location $root
try {
    & $py --noconfirm --clean (Join-Path $root 'pz-ap-client.spec')
} finally {
    Pop-Location
}
if ($LASTEXITCODE -eq 0) {
    $exe = Join-Path $root 'dist\pz-ap-client\pz-ap-client.exe'
    Write-Host "`nBuilt: $exe" -ForegroundColor Green
    Write-Host "Run:   .\dist\pz-ap-client\pz-ap-client.exe <host:port> --name <slot> [--memory]"
}
exit $LASTEXITCODE
