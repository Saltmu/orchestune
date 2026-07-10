#!/usr/bin/env bash
set -euo pipefail

# Move to the project root
cd "$(dirname "$0")/.."

echo "========================================="
echo "Running Orchestune Local CI Check..."
echo "========================================="

echo "[1/4] Checking code format (ruff format)..."
poetry run ruff format --check

echo "[2/4] Running lint (ruff check)..."
poetry run ruff check

echo "[3/4] Checking types (mypy)..."
poetry run mypy orchestune tests

echo "[4/4] Running tests with coverage (pytest)..."
poetry run pytest --cov=orchestune --cov-fail-under=75

echo "========================================="
echo "✨ Local CI passed successfully!"
echo "========================================="
