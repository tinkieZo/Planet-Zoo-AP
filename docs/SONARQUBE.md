# Whole-workspace static analysis with SonarQube Community Build

The "SonarQube for IDE" plugin analyses one open file at a time. To analyse the **entire
workspace** with the same rules (cognitive complexity, code smells, bugs, duplication, …) we run a
local **SonarQube Community Build** server and push a scan to it with **SonarScanner CLI**.

## Components

| Component | Version (as set up) | Notes |
|---|---|---|
| SonarQube Community Build | 26.5.0.122743 | extracted to a dir of your choice (the server) |
| SonarScanner CLI | 8.1.0.6389 (bundles its own JRE) | extracted to a dir of your choice |
| Server runtime (JDK) | Temurin JDK 21 (17 also supported) | point `SONAR_JAVA_PATH` at its `java.exe` |

Set the install locations once per shell (substitute your own paths):

```powershell
$SQ      = "<sonarqube-dir>"          # the dir containing bin\windows-x86-64\StartSonar.bat
$SCANNER = "<sonar-scanner-dir>"      # the dir containing bin\sonar-scanner.bat
$JAVA    = "<jdk-dir>\bin\java.exe"   # a JDK 17 or 21 java.exe
```

Project config lives in [`../sonar-project.properties`](../sonar-project.properties): project key
`planet-zoo-ap`, sources `pz_ap_client` + `tools`, tests `tests`, plus the Python version and the
exclusions (`tools/_attic/`, the vendored Archipelago tree, byte-caches, binary scan dumps).

## Start the server

```powershell
$env:SONAR_JAVA_PATH = $JAVA
& "$SQ\bin\windows-x86-64\StartSonar.bat"
```

Leave that window open (it runs in the foreground). It's ready when the log prints
`SonarQube is operational` — or poll `http://localhost:9000/api/system/status` until it returns
`{"status":"UP"}` (first boot takes ~2 min: Elasticsearch → web → compute engine).

Web UI / dashboard: **http://localhost:9000** → project **`planet-zoo-ap`**.

## Run an analysis

From the repo root, with the server up:

```powershell
.\sonar-scan.ps1
```

It reads the auth token from `.sonar-token` (gitignored) or `$env:SONAR_TOKEN`, then runs the
scanner against `sonar-project.properties`. Results: http://localhost:9000/dashboard?id=planet-zoo-ap

Equivalent raw command:

```powershell
& "$SCANNER\bin\sonar-scanner.bat" "-Dsonar.token=<token>"
```

## Stop the server

Press `Ctrl+C` in the StartSonar window, or:

```powershell
& "$SQ\bin\windows-x86-64\StopSonar.bat"
```

## Credentials / security notes

- The server starts with the default **`admin` / `admin`** account. It's **localhost-only**, but
  you should still change the password in the UI (top-right avatar → *My Account* → *Security*),
  or via API: `POST /api/users/change_password?login=admin&previousPassword=admin&password=<new>`.
- The scanner uses a user **token** (`squ_…`) stored in the gitignored `.sonar-token`. Revoke /
  regenerate it under *My Account → Security → Tokens* if needed, then update `.sonar-token`.
- `.scannerwork/` (per-run scanner state) and `.sonar-token` are gitignored;
  `sonar-project.properties`, `sonar-scan.ps1` and this doc are safe to commit.

## First-run baseline (2026-06-04)

Full Sonar way profile (332 active Python rules) over 50 files / 5,317 LOC:
**0 bugs, 0 code smells, 0 vulnerabilities, 0 security hotspots, 0 min technical debt,
0.5% duplicated lines** — including **0** `python:S3776` (cognitive-complexity) violations.

The only remaining duplication (40 lines in `pz_ap_client/memory/hook.py`) is **intentional**: the
permit-gate and facility-gate trampolines are independently-audited hand-assembled machine code
(one live, one an unfinished placeholder), where explicit duplication is safer than a shared emitter.
