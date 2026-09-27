#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/create-session-dir.sh [prefix] [issue_or_task]
# Generates a collision-safe session directory under .orchestune/tmp/
# without requiring inline Python execution.

PREFIX="${1-task}"
TASK="${2-scratch}"

if [[ ! "$PREFIX" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "Error: Invalid prefix '$PREFIX'. prefix must contain only ASCII alphanumeric characters, underscores, and hyphens." >&2
    exit 1
fi

if [[ ! "$TASK" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "Error: Invalid task '$TASK'. task must contain only ASCII alphanumeric characters, underscores, hyphens, and dots." >&2
    exit 1
fi


TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")

if command -v od >/dev/null 2>&1 && [ -r /dev/urandom ]; then
    RANDOM_HEX=$(od -vAn -N4 -tx1 /dev/urandom | tr -d ' \n' | cut -c1-8)
elif command -v openssl >/dev/null 2>&1; then
    RANDOM_HEX=$(openssl rand -hex 4)
else
    RANDOM_HEX=$(printf "%04x%04x" "$RANDOM" "$RANDOM")
fi

DIR=".orchestune/tmp/${PREFIX}-${TASK}-${TIMESTAMP}-${RANDOM_HEX}"
mkdir -p "$DIR"
echo "$DIR"
