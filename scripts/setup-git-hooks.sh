#!/usr/bin/env bash
set -euo pipefail

# Move to the project root directory
cd "$(dirname "$0")/.."

echo "Setting up Git hooks..."

# Make sure local-ci.sh is executable
chmod +x scripts/local-ci.sh

# Create the pre-push hook
HOOK_FILE=".git/hooks/pre-push"

cat << 'EOF' > "$HOOK_FILE"
#!/usr/bin/env bash
# Git pre-push hook to enforce local CI run before pushing

# Run local CI script
./scripts/local-ci.sh
EOF

# Make the hook executable
chmod +x "$HOOK_FILE"

echo "Git pre-push hook installed successfully at $HOOK_FILE."
