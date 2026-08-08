$ErrorActionPreference = "Stop"

# Move to the project root directory
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "========================================="
Write-Host "Running Orchestune Local CI Check (PowerShell)..."
Write-Host "========================================="

Write-Host "[1/5] Checking code format (ruff format)..."
poetry run ruff format --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/5] Running lint (ruff check)..."
poetry run ruff check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/5] Checking types (mypy)..."
poetry run mypy orchestune tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/5] Running tests with coverage (pytest)..."
# Note: On Windows subshell environments (e.g. agy CLI / ConPTY), pytest-xdist (-n auto) spawns multiple worker
# processes that inherit pipe handles, which can cause pipe destruction crashes when workers exit.
# We default to single-process execution (-n 0) for safe Windows execution. Override via PYTEST_ADDOPTS if needed.
poetry run pytest -n 0 --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[5/5] Scanning for secrets and local paths (gitleaks)..."
$GitleaksInstallDir = if ($env:GITLEAKS_INSTALL_DIR) { $env:GITLEAKS_INSTALL_DIR } else { Join-Path $HOME ".local\bin" }
$env:PATH = "$GitleaksInstallDir;$env:PATH"

if (-not (Get-Command gitleaks -ErrorAction SilentlyContinue)) {
    Write-Host "gitleaks not found; attempting automatic installation..."
    try {
        & (Join-Path $PSScriptRoot "install-gitleaks.ps1")
    } catch {
        Write-Warning "Automatic gitleaks installation failed: $_"
    }
}

if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
    gitleaks detect --source . --redact -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "ERROR: gitleaks is not installed locally and automatic installation failed." -ForegroundColor Red
    Write-Host "Install it before pushing: https://github.com/gitleaks/gitleaks#installing" -ForegroundColor Red
    exit 1
}

Write-Host "========================================="
Write-Host "✨ Local CI passed successfully!"
Write-Host "========================================="
