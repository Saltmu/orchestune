# Install and verify the pinned Quint toolchain (#1276).
#
#   Exit 0  Node.js matches package.json "engines" and the locked Quint is installed
#   Exit 2  Node.js / npm is missing or has the wrong major version (never skipped)
#   Exit 1  npm ci failed, or the installed Quint is not the pinned version
#
# Quint is never taken from a global install: `npm ci` installs exactly what
# package-lock.json locks into node_modules/, and the replay tests call that binary.
$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

function Show-InstallHelp {
    [Console]::Error.WriteLine("ERROR: Node.js and npm are required for local CI (the Quint dependency-liveness check).")
    [Console]::Error.WriteLine('Install the Node.js major version listed under "engines" in package.json, then rerun.')
    [Console]::Error.WriteLine("See CONTRIBUTING.md (Node.js and Quint) for the steps per OS.")
}

if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Show-InstallHelp
    exit 2
}

$package = Get-Content -Raw package.json | ConvertFrom-Json
if ($package.engines.node -match '^>=(\d+) <(\d+)$') {
    $lower = [int]$Matches[1]
    $upper = [int]$Matches[2]
} else {
    throw 'package.json engines.node must look like ">=24 <25"'
}
$nodeMajor = [int]((& node -p 'process.versions.node.split(".")[0]').Trim())
if ($nodeMajor -lt $lower -or $nodeMajor -ge $upper) {
    [Console]::Error.WriteLine("ERROR: Node.js $nodeMajor is installed; package.json requires >=$lower <$upper.")
    Show-InstallHelp
    exit 2
}

$pinned = $package.devDependencies.'@informalsystems/quint'
$lockHash = (Get-FileHash -Algorithm SHA256 package-lock.json).Hash.ToLowerInvariant()
$marker = "node_modules/.quint-check-lock"
$quint = if ($IsWindows -or $env:OS -eq "Windows_NT") { "node_modules/.bin/quint.cmd" } else { "node_modules/.bin/quint" }
$current = if (Test-Path $marker) { (Get-Content -Raw $marker).Trim() } else { "" }
if (-not (Test-Path $quint) -or $current -ne $lockHash) {
    Write-Host "Installing the locked Node.js tools (npm ci)..."
    & npm ci --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Set-Content -NoNewline -Path $marker -Value $lockHash
}

$installed = (& $quint --version).Trim()
if ($installed -ne $pinned) {
    [Console]::Error.WriteLine("ERROR: quint $installed is installed but package.json pins $pinned.")
    exit 1
}
Write-Host "Quint $installed (Node.js $((& node --version).Trim())) is ready."
