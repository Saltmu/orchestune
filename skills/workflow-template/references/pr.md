# Pull Request Reference (Step 10)

This document provides detailed procedures for finalizing and creating Pull Requests (PRs) after passing local CI.

Run the preparation and PR submission commands from the Issue's prepared
worktree, so the PR contains only that worktree branch's commits.

---

## 10. PR Creation Procedure (Pull Request Finalization)

### 1. Preparing the PR Description File
Copy the project's PR template (e.g. `.github/pull_request_template.md`) to a temporary working file (e.g. `/tmp/pr_body.md`) and complete all sections:
- **Walkthrough**: Summary of architectural changes and module edits performed.
- **Reproducer & Fix Confirmation**: Verification results of the Reproducer from Step 3 (state "N/A" for new features or minor changes).
- **Baseline Diff**: Comparison against the baseline recorded in Step 4 (no new regressions, note any base-branch pre-existing failures).
- **Verification Evidence**: Evidence that local CI (`<CI_ENTRYPOINT>`) passed.

> [!NOTE]
> For minor changes (typo or documentation fixes only), explicitly state "N/A due to minor change" in the Reproducer and test result sections.

### 2. Submitting the PR
Create the PR using the `gh` CLI:
```bash
gh pr create --title "PR Title" --body-file /tmp/pr_body.md
```
*(Note: If `gh` CLI is unavailable, submit the same content via GitHub MCP or the Web UI.)*

Once the PR is created, record the issued PR number and proceed to Step 11 (Review Loop).
