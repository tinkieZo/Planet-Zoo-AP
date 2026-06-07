# Run a whole-workspace SonarQube analysis against the local Community Build server.
#
#   1. Make sure the server is up:  http://localhost:9000  (see docs/SONARQUBE.md to start it)
#   2. From this folder:            .\sonar-scan.ps1
#
# The token is read from $env:SONAR_TOKEN, else from the gitignored .sonar-token file.
# Analysis config (sources/exclusions/python version) lives in sonar-project.properties.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$scanner = 'D:\sonar-scanner\bin\sonar-scanner.bat'

$token = $env:SONAR_TOKEN
if (-not $token -and (Test-Path "$root\.sonar-token")) {
    $token = (Get-Content "$root\.sonar-token" -Raw).Trim()
}
if (-not $token) {
    Write-Error "No SonarQube token. Set `$env:SONAR_TOKEN or create .sonar-token (see docs/SONARQUBE.md)."
    exit 1
}
if (-not (Test-Path $scanner)) {
    Write-Error "SonarScanner not found at $scanner. See docs/SONARQUBE.md."
    exit 1
}

& $scanner "-Dsonar.projectBaseDir=$root" "-Dsonar.token=$token"
if ($LASTEXITCODE -eq 0) {
    Write-Host "`nDashboard: http://localhost:9000/dashboard?id=planet-zoo-ap" -ForegroundColor Green
}
exit $LASTEXITCODE
