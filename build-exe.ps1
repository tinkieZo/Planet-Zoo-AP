# Build the distributable Planet Zoo AP client exe (PyInstaller one-dir). See docs/PACKAGING.md.
# Prereqs: .venv (Python 3.11.9+) with deps + pyinstaller, and vendor/Archipelago cloned.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\pyinstaller.exe'

if (-not (Test-Path $py)) {
    Write-Error "PyInstaller not found at $py. Set up the venv first (see docs/PACKAGING.md)."
    exit 1
}
if (-not (Test-Path (Join-Path $root 'vendor\Archipelago\CommonClient.py'))) {
    Write-Error "vendor/Archipelago is missing. Clone it first (see docs/PACKAGING.md)."
    exit 1
}

& $py --noconfirm --clean (Join-Path $root 'pz-ap-client.spec')
if ($LASTEXITCODE -eq 0) {
    $exe = Join-Path $root 'dist\pz-ap-client\pz-ap-client.exe'
    Write-Host "`nBuilt: $exe" -ForegroundColor Green
    Write-Host "Run:   .\dist\pz-ap-client\pz-ap-client.exe <host:port> --name <slot> [--memory]"
}
exit $LASTEXITCODE
