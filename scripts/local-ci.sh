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
  GIT_DIR="$(git rev-parse --git-dir 2>/dev/null || echo ".git")"
  EVIDENCE_FILE="${GIT_DIR}/ci_evidence.json"
fi
rm -f "${EVIDENCE_FILE}" "${EVIDENCE_FILE}.tmp."* 2>/dev/null || true

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required for local CI. Install it from https://docs.astral.sh/uv/." >&2
  exit 2
fi

CI_START_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
uv run --no-sync python -m orchestune.complete.ci_evidence invalidate 2>/dev/null || true

# Ensure virtual environment and dependencies are installed
if ! uv run python -c "import pytest, ruff, mypy, yaml, xdist, pytest_cov" >/dev/null 2>&1; then
  echo "Virtual environment or dependencies not found; running uv sync..."
  uv sync
fi

echo "[1/6] Checking code format (ruff format)..."
uv run ruff format --check

echo "[2/6] Running lint (ruff check)..."
uv run ruff check

echo "[3/6] Checking types (mypy)..."
uv run mypy orchestune tests

echo "[4/6] Running tests with coverage (pytest)..."
uv run pytest --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing

echo "[5/6] Detecting new or worsened code and skill bloat..."
uv run python scripts/detect_bloat.py --baseline .orchestune/bloat-baseline.json

echo "[6/6] Scanning for secrets and local paths (gitleaks)..."
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

