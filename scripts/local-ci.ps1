$ErrorActionPreference = "Stop"

# Move to the project root directory
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Unset Git internal environment variables that may leak from git hooks
$GitEnvVars = @(
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_GRAFT_FILE",
    "GIT_SUPER_PREFIX"
)
foreach ($var in $GitEnvVars) {
    if (Test-Path "Env:$var") {
        Remove-Item "Env:$var"
    }
}

Write-Host "========================================="
Write-Host "Running Orchestune Local CI Check (PowerShell)..."
Write-Host "========================================="

if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Poetry is required for local CI. Install the version specified by poetry.lock." -ForegroundColor Red
    exit 2
}

# Ensure virtual environment and dependencies are installed
& poetry run python -c "import pytest, ruff, mypy, yaml, xdist, pytest_cov" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Virtual environment or dependencies not found; running poetry install..." -ForegroundColor Cyan
    poetry install
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: poetry install failed." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host "[1/6] Checking code format (ruff format)..."
poetry run ruff format --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/6] Running lint (ruff check)..."
poetry run ruff check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/6] Checking types (mypy)..."
poetry run mypy orchestune tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/6] Running tests with coverage (pytest)..."
# Note: On Windows subshell environments (e.g. agy CLI / ConPTY), pytest-xdist (-n auto) spawns multiple worker
# processes that inherit pipe handles, which can cause pipe destruction crashes when workers exit.
# We default to single-process execution (-n 0) for safe Windows execution. Override via PYTEST_ADDOPTS if needed.
poetry run pytest -n 0 --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[5/6] Detecting new or worsened code and skill bloat..."
poetry run python scripts/detect_bloat.py --baseline .orchestune/bloat-baseline.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[6/6] Scanning for secrets and local paths (gitleaks)..."
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
