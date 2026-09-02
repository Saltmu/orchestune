# Pull Request Reference (Step 10)

This document provides detailed procedures for finalizing and creating Pull Requests (PRs) after passing local CI.

Run the PR preparation and submission commands from the Issue's prepared
worktree, so the PR contains only that worktree branch's commits.

---

## 10. PR Creation Procedure (Pull Request Finalization)

### 1. Preparing the PR Description File
Copy the repository's PR template (`.github/pull_request_template.md`) to a temporary working file (e.g. `/tmp/pr_body.md`) and complete all sections:
- **Acceptance Criteria & Scope Guard**:
  - Transcribe all acceptance criteria defined in the target Issue into the checklist (`- [x] ...`).
  - Perform a self-check on the **Scope Guard (YAGNI)** item, confirming that the PR contains only the minimal changes required to satisfy the Issue and introduces no speculative features or unrequested automatic recovery mechanisms.
- **Walkthrough**: Summary of architectural changes and module edits performed.
- **Reproducer & Fix Confirmation**: Verification results of the Reproducer from Step 3 (state "N/A" for new features or minor changes).
- **Baseline Diff**: Results from `scripts/ci_baseline.py` (no new regressions, note any base-branch pre-existing failures).
- **Verification Evidence**: Evidence that local CI passed.

> [!NOTE]
> For minor changes (typo or documentation fixes only), explicitly state "N/A due to minor change" in the Reproducer and test result sections.

### 2. Pre-PR Remote Branch & Blob Verification (GitHub MCP Route)
When files or branches are created or updated via GitHub MCP write tools rather than direct Git pushes from the worktree, execute the following post-write verification steps prior to opening a PR:

1. **Branch & Blob SHA Verification**:
   - Re-fetch the updated file contents and remote blob SHA for each modified file on the target branch.
   - Reconcile remote content against the locally verified worktree state to detect formatting discrepancies, missing characters, or corrupted escape sequences (e.g., string escaping issues during JSON-RPC tool calls).

2. **Pre-PR Cumulative Diff Check (Multi-Commit Writes)**:
   - When sequential file updates produce multiple separate commits on the remote branch, inspect the full cumulative diff against the base branch (e.g., `origin/main`) prior to PR creation.
   - Confirm that the cumulative diff contains only the intended changes and no partial or duplicate modifications.

### 3. Submitting the PR
Submit the PR using the fixed backend selected during Step 0 Preflight:

> [!IMPORTANT]
> **Base Branch for Child Subtask and Stacked PRs (Precedence Order)**:
> 1. **Stacked Subtask (Highest Precedence)**: If this task depends on another subtask whose PR has not yet merged into the parent/main branch, pass that dependency subtask's branch as `--base` (e.g. `gh pr create --base claude/issue-701-consistency-contract ...`). This takes precedence even if `parent_issue_number` is also set. The `claude/` prefix here is only an example — read the dependency's real branch name from its state/PR rather than assuming a prefix (see the naming convention note in [worktree.md](./worktree.md)).
> 2. **Parent Issue Mode**: If the Issue is a child subtask under an Epic / parent issue (`parent_issue_number` present in Footprint YAML / prompt, or worktree branched from `parent/issue-{N}`) and has no unmerged subtask dependency, pass `--base parent/issue-{parent_issue_number}` (e.g., `gh pr create --base parent/issue-700 ...`) to prevent accidental direct merges into `main`.
> 3. **Standalone Issue (Default Fallback)**: For independent tasks without parent or dependency branches, pass `--base main`.

- **When using `gh` CLI**:
  ```bash
  # Standalone issue:
  gh pr create --base main --title "PR Title" --body-file /tmp/pr_body.md

  # Child subtask of parent issue (e.g. #700):
  gh pr create --base parent/issue-700 --title "PR Title" --body-file /tmp/pr_body.md

  # Stacked subtask depending on an unmerged sibling branch:
  gh pr create --base claude/issue-701-consistency-contract --title "PR Title" --body-file /tmp/pr_body.md
  ```
- **When using GitHub MCP (or if `gh` CLI is unauthenticated/unavailable)**:
  - Call the GitHub MCP tool (e.g., `create_pull_request`) using the branch name, base branch (the stacked dependency branch, `parent/issue-{N}`, or `main`), title, and body content from `/tmp/pr_body.md`.
  - Or create the PR via the GitHub Web UI with the same title and body content.

### 4. Post-Creation PR Head Diff Verification
After PR submission (especially when operating via GitHub MCP), perform head diff verification:
- Retrieve and inspect the PR head diff and changed files list.
- Verify that the PR's cumulative diff matches the local worktree diff and local CI verification target exactly before proceeding to Step 11.

Once the PR is created and verified, record the issued PR number and proceed to Step 11 (Review Loop).


