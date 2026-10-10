#!/usr/bin/env bash
set -euo pipefail

# Move to the project root
cd "$(dirname "$0")/.."

# Unset Git internal environment variables that may leak from git hooks
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_PREFIX GIT_GRAFT_FILE GIT_SUPER_PREFIX

echo "========================================="
echo "Running Orchestune Local CI Check..."
echo "========================================="

# Invalidate prior evidence before any prerequisite checks or setup
if [ -n "${ORCHESTUNE_CI_EVIDENCE_PATH:-}" ]; then
  EVIDENCE_FILE="${ORCHESTUNE_CI_EVIDENCE_PATH}"
else
  EVIDENCE_FILE=".orchestune/ci/ci_evidence.json"
fi
if [ -e "${EVIDENCE_FILE}" ]; then
  rm -f "${EVIDENCE_FILE}" 2>/dev/null || true
  if [ -e "${EVIDENCE_FILE}" ]; then
    echo "ERROR: Failed to remove prior CI evidence at ${EVIDENCE_FILE}." >&2
    exit 1
  fi
fi
EVIDENCE_DIR="$(dirname "${EVIDENCE_FILE}")"
if [ -d "${EVIDENCE_DIR}" ]; then
  rm -f "${EVIDENCE_DIR}/ci_evidence.json.tmp."* 2>/dev/null || true
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required for local CI. Install it from https://docs.astral.sh/uv/." >&2
  exit 2
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: Node.js and npm are required for local CI (the Quint dependency-liveness check)." >&2
  echo "Install the Node.js major version listed under \"engines\" in package.json; see CONTRIBUTING.md (Node.js and Quint)." >&2
  exit 2
fi

CI_START_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
if ! CI_START_HEAD=$(git rev-parse HEAD 2>/dev/null) || [ -z "${CI_START_HEAD}" ]; then
  echo "ERROR: Failed to resolve initial HEAD before starting CI." >&2
  exit 1
fi
if ! CI_START_TREE=$(git rev-parse 'HEAD^{tree}' 2>/dev/null) || [ -z "${CI_START_TREE}" ]; then
  echo "ERROR: Failed to resolve initial tree SHA before starting CI." >&2
  exit 1
fi
if [ -n "${ORCHESTUNE_BASE_SHA:-}" ]; then
  CI_START_BASE="${ORCHESTUNE_BASE_SHA}"
else
  BASE_RESOLVE_ARGS=()
  if [ -n "${ORCHESTUNE_BASE_REF:-}" ]; then
    BASE_RESOLVE_ARGS+=("--base-ref" "${ORCHESTUNE_BASE_REF}")
  fi
  if [ -n "${ORCHESTUNE_STATE_PATH:-}" ]; then
    BASE_RESOLVE_ARGS+=("--state-path" "${ORCHESTUNE_STATE_PATH}")
  fi
  if [ -n "${ORCHESTUNE_ISSUE_NUMBER:-}" ]; then
    BASE_RESOLVE_ARGS+=("--issue" "${ORCHESTUNE_ISSUE_NUMBER}")
  fi
  if ! CI_START_BASE=$(uv run --no-sync python -m orchestune.complete.ci_evidence resolve-base "${BASE_RESOLVE_ARGS[@]}" 2>/dev/null) || [ -z "${CI_START_BASE}" ]; then
    echo "ERROR: Failed to resolve initial base SHA before starting CI." >&2
    exit 1
  fi
fi
uv run --no-sync python -m orchestune.complete.ci_evidence invalidate 2>/dev/null || true

# Ensure virtual environment and dependencies are installed
if ! uv run python -c "import pytest, ruff, mypy, yaml, xdist, pytest_cov" >/dev/null 2>&1; then
  echo "Virtual environment or dependencies not found; running uv sync..."
  uv sync
fi

echo "[1/7] Checking code format (ruff format)..."
uv run ruff format --check

echo "[2/7] Running lint (ruff check)..."
uv run ruff check

echo "[3/7] Checking types (mypy)..."
uv run mypy orchestune tests

echo "[4/7] Installing and verifying the pinned Quint toolchain (Node.js)..."
./scripts/quint-check.sh
# One session directory per run: the bounded exploration writes its traces and
# summary here and the replay reads them back (see tests/quint_replay.py).
QUINT_REPLAY_DIR="$(./scripts/create-session-dir.sh quint-replay 1276)"
export ORCHESTUNE_QUINT_REPLAY_DIR="${QUINT_REPLAY_DIR}"

echo "[5/7] Running tests with coverage (pytest)..."
(
  # Tests create independent Git repositories; completion's CI context belongs only
  # to this worktree and must not become those repositories' evidence defaults.
  unset ORCHESTUNE_EXPECTED_HEAD ORCHESTUNE_EXPECTED_TREE
  unset ORCHESTUNE_BASE_SHA ORCHESTUNE_BASE_REF ORCHESTUNE_STATE_PATH
  unset ORCHESTUNE_ISSUE_NUMBER ORCHESTUNE_CI_EVIDENCE_PATH
  uv run pytest --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing
)

QUINT_SUMMARY="${QUINT_REPLAY_DIR}/test_bounded_exploration_replays_on_production/exploration-summary.json"
if [ ! -s "${QUINT_SUMMARY}" ]; then
  echo "ERROR: the Quint exploration did not run (no ${QUINT_SUMMARY})." >&2
  exit 1
fi
echo "Quint exploration (seed, bounds, tool versions, replayed counts):"
cat "${QUINT_SUMMARY}"

echo "[6/7] Detecting new or worsened code and skill bloat..."
uv run python scripts/detect_bloat.py --baseline .orchestune/bloat-baseline.json

echo "[7/7] Scanning for secrets and local paths (gitleaks)..."
GITLEAKS_INSTALL_DIR="${GITLEAKS_INSTALL_DIR:-$HOME/.local/bin}"
export PATH="${GITLEAKS_INSTALL_DIR}:${PATH}"

if ! command -v gitleaks >/dev/null 2>&1; then
  echo "gitleaks not found; attempting automatic installation..."
  ./scripts/install-gitleaks.sh || true
fi

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --redact -v
else
  echo "ERROR: gitleaks is not installed locally and automatic installation failed." >&2
  echo "Install it before pushing: https://github.com/gitleaks/gitleaks#installing" >&2
  exit 1
fi

echo "========================================="
echo "✨ Local CI passed successfully!"
echo "========================================="

RECORD_ARGS=("--started-at" "${CI_START_TIME}")
if [ -n "${CI_START_HEAD}" ]; then
  RECORD_ARGS+=("--expected-head" "${CI_START_HEAD}")
fi
if [ -n "${CI_START_TREE}" ]; then
  RECORD_ARGS+=("--expected-tree" "${CI_START_TREE}")
fi
if [ -n "${CI_START_BASE}" ] && [ -z "${ORCHESTUNE_BASE_SHA:-}" ]; then
  RECORD_ARGS+=("--base-sha" "${CI_START_BASE}")
fi
if [ -n "${ORCHESTUNE_BASE_SHA:-}" ]; then
  RECORD_ARGS+=("--base-sha" "${ORCHESTUNE_BASE_SHA}")
fi
if [ -n "${ORCHESTUNE_BASE_REF:-}" ]; then
  RECORD_ARGS+=("--base-ref" "${ORCHESTUNE_BASE_REF}")
fi
if [ -n "${ORCHESTUNE_STATE_PATH:-}" ]; then
  RECORD_ARGS+=("--state-path" "${ORCHESTUNE_STATE_PATH}")
fi
if [ -n "${ORCHESTUNE_ISSUE_NUMBER:-}" ]; then
  RECORD_ARGS+=("--issue" "${ORCHESTUNE_ISSUE_NUMBER}")
fi

uv run python -m orchestune.complete.ci_evidence record "${RECORD_ARGS[@]}"
