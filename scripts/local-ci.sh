#!/usr/bin/env bash
set -euo pipefail

# Move to the project root
cd "$(dirname "$0")/.."

# Unset Git internal environment variables that may leak from git hooks
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_PREFIX GIT_GRAFT_FILE GIT_SUPER_PREFIX

echo "========================================="
echo "Running Orchestune Local CI Check..."
echo "========================================="

if ! command -v poetry >/dev/null 2>&1; then
  echo "ERROR: Poetry is required for local CI. Install the version specified by poetry.lock." >&2
  exit 2
fi

# Ensure virtual environment and dependencies are installed
if ! poetry run python -c "import pytest, ruff, mypy, yaml, xdist, pytest_cov" >/dev/null 2>&1; then
  echo "Virtual environment or dependencies not found; running poetry install..."
  poetry install
fi

echo "[1/6] Checking code format (ruff format)..."
poetry run ruff format --check

echo "[2/6] Running lint (ruff check)..."
poetry run ruff check

echo "[3/6] Checking types (mypy)..."
poetry run mypy orchestune tests

echo "[4/6] Running tests with coverage (pytest)..."
poetry run pytest --cov=orchestune --cov-branch --cov-fail-under=90 --cov-report=term-missing

echo "[5/6] Detecting new or worsened code and skill bloat..."
poetry run python scripts/detect_bloat.py --baseline .orchestune/bloat-baseline.json

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
