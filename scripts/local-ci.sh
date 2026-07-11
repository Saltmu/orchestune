#!/usr/bin/env bash
set -euo pipefail

# Move to the project root
cd "$(dirname "$0")/.."

echo "========================================="
echo "Running Orchestune Local CI Check..."
echo "========================================="

echo "[1/5] Checking code format (ruff format)..."
poetry run ruff format --check

echo "[2/5] Running lint (ruff check)..."
poetry run ruff check

echo "[3/5] Checking types (mypy)..."
poetry run mypy orchestune tests

echo "[4/5] Running tests with coverage (pytest)..."
poetry run pytest -n auto --cov=orchestune --cov-fail-under=75

echo "[5/5] Scanning for secrets and local paths (gitleaks)..."
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --redact -v
else
  echo "ERROR: gitleaks is not installed locally." >&2
  echo "Install it before pushing: https://github.com/gitleaks/gitleaks#installing" >&2
  exit 1
fi

echo "========================================="
echo "✨ Local CI passed successfully!"
echo "========================================="
