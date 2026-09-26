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
    $GitDir = (git rev-parse --git-dir 2>$null)
    if (-not $GitDir) { $GitDir = ".git" }
    $EvidenceFile = Join-Path $GitDir "ci_evidence.json"
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

Write-Host "[1/6] Checking code format (ruff format)..."
uv run ruff format --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[2/6] Running lint (ruff check)..."
uv run ruff check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[3/6] Checking types (mypy)..."
uv run mypy orchestune tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[4/6] Running tests with coverage (pytest)..."
# Note: On Windows subshell environments (e.g. agy CLI / ConPTY), pytest-xdist (-n auto) spawns multiple worker
# processes that inherit pipe handles, which can cause pipe destruction crashes when workers exit.
# We default to single-process execution (-n 0) for safe Windows execution. Override via PYTEST_ADDOPTS if needed.
$CiContextVars = @(
    "ORCHESTUNE_EXPECTED_HEAD", "ORCHESTUNE_EXPECTED_TREE", "ORCHESTUNE_EXPECTED_BASE",
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
    uv run pytest -n 0 --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing
    $PytestExitCode = $LASTEXITCODE
} finally {
    foreach ($var in $SavedCiContext.Keys) {
        Set-Item "Env:$var" $SavedCiContext[$var]
    }
}
if ($PytestExitCode -ne 0) { exit $PytestExitCode }

Write-Host "[5/6] Detecting new or worsened code and skill bloat..."
uv run python scripts/detect_bloat.py --baseline .orchestune/bloat-baseline.json
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
