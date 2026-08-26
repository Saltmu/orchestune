#!/bin/bash
# SessionStart hook for Claude Code on the web.
# Installs the toolchain CONTRIBUTING.md expects (Poetry deps on Python 3.12,
# git hooks + gitleaks, GitHub CLI) so tests/lint/gh work from the first turn.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# --- Python deps (pyproject.toml requires Python 3.12+) ---
if command -v poetry >/dev/null 2>&1; then
  if command -v python3.12 >/dev/null 2>&1; then
    poetry env use python3.12 >/dev/null
  fi
  poetry install
fi

# --- Git hooks + gitleaks (idempotent) ---
if [ -x ./scripts/setup-git-hooks.sh ]; then
  ./scripts/setup-git-hooks.sh
fi

# --- GitHub CLI ---
if ! command -v gh >/dev/null 2>&1; then
  keyring=/etc/apt/keyrings/githubcli-archive-keyring.gpg
  mkdir -p -m 755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$keyring"
  chmod go+r "$keyring"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=$keyring] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update
  apt-get install -y gh
fi
