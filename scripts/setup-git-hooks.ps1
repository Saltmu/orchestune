$ErrorActionPreference = "Stop"

# Move to the project root directory
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Setting up Git hooks (PowerShell)..."

# Proactively install gitleaks so the pre-push hook's local CI run doesn't
# fail on first use. Non-fatal: local-ci.ps1 retries this automatically.
try {
    & (Join-Path $PSScriptRoot "install-gitleaks.ps1")
} catch {
    Write-Warning "gitleaks auto-install failed; local-ci.ps1 will retry on push. ($_)"
}

$HooksDir = Join-Path $ProjectRoot ".git\hooks"
if (-not (Test-Path $HooksDir)) {
    New-Item -ItemType Directory -Path $HooksDir -Force | Out-Null
}

$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

# Create pre-commit hook (using single-quoted here-string to avoid PowerShell variable/command expansion)
$PreCommitHook = Join-Path $HooksDir "pre-commit"
$PreCommitContent = @'
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
'@

[System.IO.File]::WriteAllText($PreCommitHook, $PreCommitContent.Replace("`r`n", "`n"), $Utf8NoBom)

# Create pre-push hook
$PrePushHook = Join-Path $HooksDir "pre-push"
$PrePushContent = @'
#!/usr/bin/env bash
# Git pre-push hook to enforce local CI run before pushing

if command -v powershell.exe >/dev/null 2>&1; then
  powershell.exe -ExecutionPolicy Bypass -File ./scripts/local-ci.ps1
elif [ -f ./scripts/local-ci.sh ]; then
  ./scripts/local-ci.sh
fi
'@

[System.IO.File]::WriteAllText($PrePushHook, $PrePushContent.Replace("`r`n", "`n"), $Utf8NoBom)

Write-Host "Git pre-commit hook installed successfully at $PreCommitHook."
Write-Host "Git pre-push hook installed successfully at $PrePushHook."
