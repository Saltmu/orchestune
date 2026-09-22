# Review Loop Reference (Step 11)

This document provides detailed procedures for automated LLM PR reviews and feedback resolution cycles.

Keep the review loop in the same worktree used to create the PR. Apply feedback,
run CI, commit, and push only from that worktree.

During #822 observation, apply [measurement.md](measurement.md) to every round: capture
the reviewed SHA, deduplicate and classify findings, and record re-review reasons.
Before Step 12, finalize the record even for zero findings, timeout, or blocked work.

---

## 11. Automated LLM PR Review Loop (Review Cycle)

Execute `scripts/wait_for_review.py` synchronously using the reviewer bot decided in Step 1 (or resolved by dispatch/context), wait for completion, and analyze feedback. Double-posting is prevented by the script's internal wait controls. The cumulative round count is tracked via `@<bot> review` comments and `Round X/5` notations, preserving count across session interruptions.

### Handling Review Findings and Scope Management

When findings are returned (Exit 10):
1. **Adoption Verification (Essential Findings vs. Speculative Edge Cases)**:
   - **Adopt (In-Scope)** ONLY essential review findings:
     - **Module Implementation Contradictions**: Contract mismatches, interface/signature discrepancies, or invariant violations between collaborating modules/components.
     - **Unmet Acceptance Criteria / Regressions**: Findings necessary to fulfill the PR's declared **Acceptance Criteria** or fix a regression introduced by this PR.
   - **Decline (Out-of-Scope / YAGNI)**:
     - **Unoccurred / Speculative Edge Cases**: Hypothetical corner cases that have not occurred in practice, cannot be reproduced, or assume invalid states outside system boundaries. Do NOT implement speculative safeguards, defensive recovery paths, or extra checks for unoccurred edge cases.
     - Speculative failure recoveries, extra abstraction, unrelated refactoring, or enhancements beyond stated Acceptance Criteria.
2. **In-Scope Findings**:
   - Address feedback by updating code and adding/modifying tests in the worktree.
   - Run local CI (`./scripts/local-ci.sh` / `.\\scripts\\local-ci.ps1`) to ensure all checks pass.
   - Commit and push fixes to the PR branch; record commit hash and summary for re-review reply.
3. **Out-of-Scope Findings**:
   - **Do NOT implement** out-of-scope changes in the current PR (prevent scope creep).
   - If genuinely valuable for future work, file a follow-up Issue via selected backend.
   - If speculative, unoccurred, or unneeded (YAGNI), decline with explicit rationale without filing an Issue.
   - In re-review reply, document decline rationale (e.g. `[Declined - YAGNI] Unoccurred edge case: exceeds PR acceptance criteria` or `[Declined - Out of Scope] Exceeds PR acceptance criteria; deferred to #...`).
4. **Re-Review Reply Documentation**:
   - Include detailed resolution summary (commit hashes for fixes, rationales for declines) in `<session-dir>/review-reply.md`.

### Review Loop Control Flow (Pseudocode)

```text
Loop (up to 5 rounds):
  1. Acquire review state and execute shared verdict evaluator:
     - CLI/gh initial round: uv run python scripts/wait_for_review.py --pr <PR_NUMBER> --bot-name <bot>
     - Subsequent rounds: attach `--body-file <session-dir>/review-reply.md` (with commit hash & fix summary).
     - GitHub MCP / App: retrieve comments/reviews snapshot, then run:
       uv run python scripts/wait_for_review.py --bot-name <bot> --review-state-file <STATE.json>
  2. Evaluate exit code, then carefully read the entire result:
     - Exit 10: actionable findings are present. Read every Inline Finding block (path, line, full body).
     - Exit 0: clean pass / no findings. Exit 11: reviewer still in progress.
     - Exit 20: timeout (default 1800s); retry once with --no-post --timeout 1800. If still timed out, post Outcome Record with result: "blocked", reason: "review-timeout", review.bot set to reviewer bot, and attempt count.
     - Exit 21: stalled — in-progress tracker stopped changing past grace window (default 600s); run ended without posting final result. Re-run wait_for_review.py normally for next round; Exit 12 escalates.
     - Exit 30: ambiguous verdict; inspect summary and inline findings before requesting another review or escalating. Exit 2 or 12: record and escalate.
     - Exit 10:
       a. Classify findings: adopt ONLY module contradictions and unmet Acceptance Criteria/regressions. Decline unoccurred edge cases (YAGNI) and out-of-scope items.
       b. For in-scope findings: fix code and add tests, verify local CI, commit and push.
       c. For out-of-scope findings: do NOT modify code; file a follow-up Issue only if valuable, otherwise decline with rationale.
       d. Create `<session-dir>/review-reply.md` with fix details, commit hashes, rationales, and optional follow-up Issue references (Round X/5).
       e. Return to step 1.
     - Exit 0: terminate loop and proceed to Step 12 (Outcome).
```

### Review reply
Use `<session-dir>/review-reply.md` with `Round X/5`, addressed findings and commit hashes,
declined findings and reasons (e.g. `[Declined - YAGNI] Unoccurred edge case: ...`),
and any follow-up Issue links. Pass it with `--body-file` to `wait_for_review.py`; do not post a separate trigger comment.

### Diagnosing Exit 20 vs Exit 21 vs Exit 30 (Bot-Authored Trigger Failures)

`Exit 20` (no review activity at all within timeout), `Exit 21` (in-progress tracker stopped changing),
and `Exit 30` (activity exists but verdict undetermined) have different root causes:

- **Exit 20 (no activity at all)**: if trigger comment was posted from a bot-authored execution environment (e.g. Claude Code on the Web, where actor is `claude[bot]`), check `Claude Code Review` workflow run on GitHub Actions:
  - If run never appears or job shows `skipped`: actor was not `claude[bot]`, or trigger comment lacked Orchestune marker (`<!-- orchestune:review-trigger bot=claude -->`) stamped by `wait_for_review.py` (issue #692).
  - If job shows `failure` with `Workflow initiated by non-human actor`: actor is a bot not in `allowed_bots` (`claude[bot]` only).
- **Exit 30 (activity present, verdict undetermined)**: review ran and posted content; inspect actual review body and inline comments from `wait_for_review.py`.

In all three cases, find the run with `gh run list --workflow claude-code-review.yml --json databaseId,event,status,conclusion`, inspect triggering actor with `gh api repos/{owner}/{repo}/actions/runs/<run-id> --jq '.actor.login'`, and check job conclusions with `gh run view <run-id> --json jobs`.
