# Pull Request Reference (Step 10)

Prepare and submit from the task worktree after local CI passes.

## Description
Copy `.github/pull_request_template.md` to a temporary body file and complete:
- Acceptance Criteria: transcribe the Issue criteria and check those actually satisfied.
- Scope Guard: confirm the change fulfills the Issue without speculative additions.
- Walkthrough: architectural/module changes and the impact-scope table with reconciliation.
- Reproducer & Fix Confirmation: Step 3 evidence; N/A for new features or minor changes.
- Baseline Diff: comparison and pre-existing failures; N/A due to minor change if skipped.
- Verification Evidence: local CI results; docs-only changes still require local CI.

During #822 observation, add the [measurement record](measurement.md) and capture initial
review conditions before requesting review. Preserve initial values after later pushes.

## Pre-PR verification (MCP writes)
Re-fetch each file and blob SHA; reconcile against the verified worktree.
Check the cumulative diff across commits for missing content, escape errors, and partial/duplicate/unrelated writes before PR creation.

## Base selection and submission
Use the selected GitHub backend from Step 0. Base precedence:
1. Unmerged stacked dependency: its actual branch, even with a parent Issue.
2. Orchestune parent mode (`parent_issue_number` in Footprint/prompt or worktree branched
   from `parent/issue-{N}`): `parent/issue-{parent_issue_number}`.
3. Independent task: `main`. A GitHub Sub-Issue relationship alone does not select parent mode.
For CLI, run `gh pr create --base <resolved-base> --title "PR Title" --body-file <body-file>`.
For MCP, pass the same head/base/title/body via `create_pull_request`; Web UI is a fallback.
See [worktree.md](worktree.md) for branch naming; never assume a prefix.

## Post-creation verification
After PR creation on either backend, compare head diff and changed files with the worktree
and CI target. Record the PR number and proceed to Step 11 only after they match.
