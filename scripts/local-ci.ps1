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

# Invalidate prior evidence before any prerequisite checks or setup
if ($env:ORCHESTUNE_CI_EVIDENCE_PATH) {
    $EvidenceFile = $env:ORCHESTUNE_CI_EVIDENCE_PATH
} else {
    $EvidenceFile = Join-Path $ProjectRoot ".orchestune\ci\ci_evidence.json"
}
if (Test-Path $EvidenceFile) {
    Remove-Item -Force $EvidenceFile -ErrorAction SilentlyContinue
    if (Test-Path $EvidenceFile) {
        Write-Host "ERROR: Failed to remove prior CI evidence at $EvidenceFile." -ForegroundColor Red
        exit 1
    }
}
$EvidenceParent = Split-Path -Parent $EvidenceFile
if ($EvidenceParent -and (Test-Path $EvidenceParent)) {
    Get-ChildItem -Path $EvidenceParent -Filter "ci_evidence.json.tmp.*" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: uv is required for local CI. Install it from https://docs.astral.sh/uv/." -ForegroundColor Red
    exit 2
}

if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Node.js and npm are required for local CI (the Quint dependency-liveness check)." -ForegroundColor Red
    Write-Host 'Install the Node.js major version listed under "engines" in package.json; see CONTRIBUTING.md (Node.js and Quint).' -ForegroundColor Red
    exit 2
}

$CiStartTime = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
$CiStartHead = (git rev-parse HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $CiStartHead) {
    Write-Host "ERROR: Failed to resolve initial HEAD before starting CI." -ForegroundColor Red
    exit 1
}
$CiStartHead = $CiStartHead.Trim()
$CiStartTree = (git rev-parse 'HEAD^{tree}' 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $CiStartTree) {
    Write-Host "ERROR: Failed to resolve initial tree SHA before starting CI." -ForegroundColor Red
    exit 1
}
$CiStartTree = $CiStartTree.Trim()
$CiStartBase = $env:ORCHESTUNE_BASE_SHA
if (-not $CiStartBase) {
    $BaseResolveArgs = @()
    if ($env:ORCHESTUNE_BASE_REF) { $BaseResolveArgs += @("--base-ref", $env:ORCHESTUNE_BASE_REF) }
    if ($env:ORCHESTUNE_STATE_PATH) { $BaseResolveArgs += @("--state-path", $env:ORCHESTUNE_STATE_PATH) }
    if ($env:ORCHESTUNE_ISSUE_NUMBER) { $BaseResolveArgs += @("--issue", $env:ORCHESTUNE_ISSUE_NUMBER) }
    $resolvedBase = (uv run --no-sync python -m orchestune.complete.ci_evidence resolve-base @BaseResolveArgs 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $resolvedBase) {
        Write-Host "ERROR: Failed to resolve initial base SHA before starting CI." -ForegroundColor Red
        exit 1
    }
    $CiStartBase = $resolvedBase.Trim()
}
uv run --no-sync python -m orchestune.complete.ci_evidence invalidate 2>$null

# Ensure virtual environment and dependencies are installed
& uv run python -c "import pytest, ruff, mypy, yaml, xdist, pytest_cov" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Virtual environment or dependencies not found; running uv sync..." -ForegroundColor Cyan
    uv sync
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: uv sync failed." -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Write-Host "[1/7] Checking code format (ruff format)..."
uv run ruff format --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/7] Running lint (ruff check)..."
uv run ruff check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/7] Checking types (mypy)..."
uv run mypy orchestune tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/7] Installing and verifying the pinned Quint toolchain (Node.js)..."
& (Join-Path $PSScriptRoot "quint-check.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# One session directory per run: the bounded exploration writes its traces and
# summary here and the replay reads them back (see tests/quint_replay.py).
$QuintReplayDir = (& (Join-Path $PSScriptRoot "create-session-dir.ps1") "quint-replay" "1276")
if ($LASTEXITCODE -ne 0 -or -not $QuintReplayDir) { exit 1 }
$env:ORCHESTUNE_QUINT_REPLAY_DIR = "$QuintReplayDir"

Write-Host "[5/7] Running tests with coverage (pytest)..."
# Note: On Windows, pyproject.toml defaults to -n 2. Historically, -n auto caused ConPTY pipe leak crashes (#273).
# We allow configuring workers via ORCHESTUNE_TEST_WORKERS or PYTEST_ADDOPTS, defaulting to -n 2.
$PytestWorkerArgs = @()
if ($env:ORCHESTUNE_TEST_WORKERS) {
    $PytestWorkerArgs += @("-n", $env:ORCHESTUNE_TEST_WORKERS)
} elseif ($env:PYTEST_ADDOPTS -and ($env:PYTEST_ADDOPTS -match '(^|\s)(-n(\s*(\d+|auto|logical)|=|\s|$)|--numprocesses(\s*|=|\b|$))')) {
    # Respect concurrency already configured in PYTEST_ADDOPTS
} else {
    $PytestWorkerArgs += @("-n", "2")
}
$CiContextVars = @(
    "ORCHESTUNE_EXPECTED_HEAD", "ORCHESTUNE_EXPECTED_TREE",
    "ORCHESTUNE_BASE_SHA", "ORCHESTUNE_BASE_REF", "ORCHESTUNE_STATE_PATH",
    "ORCHESTUNE_ISSUE_NUMBER", "ORCHESTUNE_CI_EVIDENCE_PATH"
)
$SavedCiContext = @{}
foreach ($var in $CiContextVars) {
    $value = [Environment]::GetEnvironmentVariable($var, "Process")
    if ($null -ne $value) { $SavedCiContext[$var] = $value }
    Remove-Item "Env:$var" -ErrorAction SilentlyContinue
}
try {
    uv run pytest @PytestWorkerArgs --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing:skip-covered
    $PytestExitCode = $LASTEXITCODE
} finally {
    foreach ($var in $SavedCiContext.Keys) {
        Set-Item "Env:$var" $SavedCiContext[$var]
    }
}
if ($PytestExitCode -ne 0) { exit $PytestExitCode }

$QuintSummary = Join-Path $QuintReplayDir "test_bounded_exploration_replays_on_production/exploration-summary.json"
if (-not (Test-Path $QuintSummary) -or (Get-Item $QuintSummary).Length -eq 0) {
    Write-Host "ERROR: the Quint exploration did not run (no $QuintSummary)." -ForegroundColor Red
    exit 1
}
Write-Host "Quint exploration (seed, bounds, tool versions, replayed counts):"
Get-Content -Raw $QuintSummary

Write-Host "[6/7] Detecting new or worsened code and skill bloat..."
uv run python scripts/detect_bloat.py --baseline .orchestune/bloat-baseline.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[7/7] Scanning for secrets and local paths (gitleaks)..."
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

$RecordArgs = @("--started-at", $CiStartTime)
if ($CiStartHead) {
    $RecordArgs += @("--expected-head", $CiStartHead)
}
if ($CiStartTree) {
    $RecordArgs += @("--expected-tree", $CiStartTree)
}
if ($CiStartBase -and -not $env:ORCHESTUNE_BASE_SHA) {
    $RecordArgs += @("--base-sha", $CiStartBase)
}
if ($env:ORCHESTUNE_BASE_SHA) {
    $RecordArgs += @("--base-sha", $env:ORCHESTUNE_BASE_SHA)
}
if ($env:ORCHESTUNE_BASE_REF) {
    $RecordArgs += @("--base-ref", $env:ORCHESTUNE_BASE_REF)
}
if ($env:ORCHESTUNE_STATE_PATH) {
    $RecordArgs += @("--state-path", $env:ORCHESTUNE_STATE_PATH)
}
if ($env:ORCHESTUNE_ISSUE_NUMBER) {
    $RecordArgs += @("--issue", $env:ORCHESTUNE_ISSUE_NUMBER)
}
uv run python -m orchestune.complete.ci_evidence record @RecordArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
