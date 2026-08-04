# Installs the `gitleaks` binary locally if it is not already on PATH.
#
# Used by scripts/local-ci.ps1 (auto-recovery before the secret scan step) and
# scripts/setup-git-hooks.ps1 (proactive install during initial dev setup), so
# that fresh Windows environments don't need a manual gitleaks install before
# the mandatory local CI gitleaks scan can run.
#
# Env overrides:
#   GITLEAKS_VERSION      - pinned gitleaks release to install (default below)
#   GITLEAKS_INSTALL_DIR  - install destination (default: $HOME\.local\bin)
$ErrorActionPreference = "Stop"

$GitleaksVersion = if ($env:GITLEAKS_VERSION) { $env:GITLEAKS_VERSION } else { "8.30.1" }
$InstallDir = if ($env:GITLEAKS_INSTALL_DIR) { $env:GITLEAKS_INSTALL_DIR } else { Join-Path $HOME ".local\bin" }

if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
    Write-Host "gitleaks is already installed: $((Get-Command gitleaks).Source)"
    exit 0
}

$archName = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x32" }
$archive = "gitleaks_${GitleaksVersion}_windows_${archName}.zip"
$baseUrl = "https://github.com/gitleaks/gitleaks/releases/download/v${GitleaksVersion}"

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ([System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {
    Write-Host "Installing gitleaks v${GitleaksVersion} (windows/${archName}) to ${InstallDir}..."
    $archivePath = Join-Path $tmpDir $archive
    Invoke-WebRequest -Uri "${baseUrl}/${archive}" -OutFile $archivePath

    $checksumsPath = Join-Path $tmpDir "checksums.txt"
    Invoke-WebRequest -Uri "${baseUrl}/gitleaks_${GitleaksVersion}_checksums.txt" -OutFile $checksumsPath

    $checksumLine = Select-String -Path $checksumsPath -Pattern " $archive$"
    if (-not $checksumLine) {
        Write-Error "Could not find checksum for $archive in checksums.txt."
        exit 1
    }
    $expectedChecksum = ($checksumLine.Line -split '\s+')[0]
    $actualChecksum = (Get-FileHash -Path $archivePath -Algorithm SHA256).Hash.ToLower()
    if ($expectedChecksum -ne $actualChecksum) {
        Write-Error "Checksum mismatch for ${archive}: expected $expectedChecksum, got $actualChecksum"
        exit 1
    }

    Expand-Archive -Path $archivePath -DestinationPath $tmpDir -Force
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item -Path (Join-Path $tmpDir "gitleaks.exe") -Destination (Join-Path $InstallDir "gitleaks.exe") -Force

    Write-Host "gitleaks installed at $(Join-Path $InstallDir 'gitleaks.exe')"

    $pathEntries = $env:PATH -split ";"
    if ($pathEntries -notcontains $InstallDir) {
        Write-Warning "${InstallDir} is not currently on PATH. Add it, e.g.: `$env:PATH = \"${InstallDir};`$env:PATH\""
    }
} finally {
    Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
