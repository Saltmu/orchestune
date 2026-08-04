#!/usr/bin/env bash
set -euo pipefail

# Move to the project root directory
cd "$(dirname "$0")/.."

echo "Setting up Git hooks..."

# Make sure local-ci.sh is executable
chmod +x scripts/local-ci.sh
chmod +x scripts/install-gitleaks.sh

# Proactively install gitleaks so the pre-push hook's local CI run doesn't
# fail on first use. Non-fatal: local-ci.sh retries this automatically.
./scripts/install-gitleaks.sh || echo "WARNING: gitleaks auto-install failed; local-ci.sh will retry on push."

# Create the pre-commit hook
PRE_COMMIT_HOOK=".git/hooks/pre-commit"

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

# Create the pre-push hook
PRE_PUSH_HOOK=".git/hooks/pre-push"

cat << 'EOF' > "$PRE_PUSH_HOOK"
#!/usr/bin/env bash
# Git pre-push hook to enforce local CI run before pushing

# Run local CI script
./scripts/local-ci.sh
EOF

chmod +x "$PRE_PUSH_HOOK"

echo "Git pre-commit hook installed successfully at $PRE_COMMIT_HOOK."
echo "Git pre-push hook installed successfully at $PRE_PUSH_HOOK."
