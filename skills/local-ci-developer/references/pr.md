# Pull Request Reference (Step 10)

This document provides detailed procedures for finalizing and creating Pull Requests (PRs) after passing local CI.

Run the PR preparation and submission commands from the Issue's prepared
worktree, so the PR contains only that worktree branch's commits.

---

## 10. PR Creation Procedure (Pull Request Finalization)

### 1. Preparing the PR Description File
Copy the repository's PR template (`.github/pull_request_template.md`) to a temporary working file (e.g. `/tmp/pr_body.md`) and complete all sections:
- **Walkthrough**: Summary of architectural changes and module edits performed.
- **Reproducer & Fix Confirmation**: Verification results of the Reproducer from Step 3 (state "N/A" for new features or minor changes).
- **Baseline Diff**: Results from `scripts/ci_baseline.py` (no new regressions, note any base-branch pre-existing failures).
- **Verification Evidence**: Evidence that local CI passed.

> [!NOTE]
> For minor changes (typo or documentation fixes only), explicitly state "N/A due to minor change" in the Reproducer and test result sections.

### 2. Submitting the PR
Submit the PR using the fixed backend selected during Step 0 Preflight:

- **When using `gh` CLI**:
  ```bash
  gh pr create --title "PR Title" --body-file /tmp/pr_body.md
  ```
- **When using GitHub MCP (or if `gh` CLI is unauthenticated/unavailable)**:
  - Call the GitHub MCP tool (e.g., `create_pull_request`) using the branch name, title, and body content from `/tmp/pr_body.md`.
  - Or create the PR via the GitHub Web UI with the same title and body content.

Once the PR is created, record the issued PR number and proceed to Step 11 (Review Loop).
