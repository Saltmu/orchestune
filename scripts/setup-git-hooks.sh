#!/usr/bin/env bash
set -euo pipefail

# Move to the project root directory
cd "$(dirname "$0")/.."

echo "Setting up Git hooks..."

# Make sure local-ci.sh is executable
chmod +x scripts/local-ci.sh
chmod +x scripts/install-gitleaks.sh

# Proactively install gitleaks so local CI runs don't
# fail on first use. Non-fatal: local-ci.sh retries this automatically.
./scripts/install-gitleaks.sh || echo "WARNING: gitleaks auto-install failed; local-ci.sh will retry on execution."

# Resolve Git hooks directory dynamically to support standard repos, worktrees, and submodules
HOOKS_DIR="$(git rev-parse --git-path hooks 2>/dev/null || echo ".git/hooks")"
mkdir -p "$HOOKS_DIR"

# Create the pre-commit hook
PRE_COMMIT_HOOK="${HOOKS_DIR}/pre-commit"

cat << 'EOF' > "$PRE_COMMIT_HOOK"
#!/usr/bin/env bash
# Git pre-commit hook to block force-added ignored files

set -euo pipefail

staged_files="$(git diff --cached --name-only --diff-filter=ACMR)"
[ -z "$staged_files" ] && exit 0

bad_files=()
while IFS= read -r path; do
  [ -z "$path" ] && continue
  if git check-ignore -q "$path"; then
    bad_files+=("$path")
  fi
done <<< "$staged_files"

if [ "${#bad_files[@]}" -gt 0 ]; then
  echo "Refusing to commit files ignored by .gitignore:" >&2
  printf '  - %s\n' "${bad_files[@]}" >&2
  echo "Remove them from the index or commit them only with explicit manual override." >&2
  exit 1
fi
EOF

chmod +x "$PRE_COMMIT_HOOK"

# Clean up legacy pre-push hook if present
PRE_PUSH_HOOK="${HOOKS_DIR}/pre-push"
if [ -f "$PRE_PUSH_HOOK" ]; then
  rm -f "$PRE_PUSH_HOOK"
  echo "Removed deprecated pre-push hook at $PRE_PUSH_HOOK."
fi

echo "Git pre-commit hook installed successfully at $PRE_COMMIT_HOOK."
