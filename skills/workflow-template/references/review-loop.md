# Review Loop Reference (Step 11)

This document provides detailed procedures for automated LLM PR reviews and feedback resolution cycles.

Keep the review loop in the same worktree used to create the PR. Apply feedback,
run CI, commit, and push only from that worktree.

---

## 11. Automated LLM PR Review Loop (Review Cycle)

After creating a PR, conduct automated LLM PR reviews using the reviewer bot decided in Step 1 (or resolved by dispatch/context) for objective quality verification and iterate until all actionable findings are resolved.

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
   - Run local CI (`<CI_ENTRYPOINT>`) to ensure all checks pass.
   - Commit and push fixes to the PR branch; record commit hash and summary for re-review reply.
3. **Out-of-Scope Findings**:
   - **Do NOT implement** out-of-scope changes in the current PR (prevent scope creep).
   - If genuinely valuable for future work, file a follow-up Issue via selected backend.
   - If speculative, unoccurred, or unneeded (YAGNI), decline with explicit rationale without filing an Issue.
   - In re-review reply, document decline rationale (e.g. `[Declined - YAGNI] Unoccurred edge case: exceeds PR acceptance criteria` or `[Declined - Out of Scope] Exceeds PR acceptance criteria; deferred to #...`).
4. **Re-Review Reply Documentation**:
   - Always include detailed resolution summary (commit hashes for fixes, rationales for declines) in `<session-dir>/review-reply.md`.

### Review Loop Control Flow (Pseudocode)

```text
Loop (up to 5 rounds):
  1. Acquire review state and execute the shared verdict evaluator:
     - In wait_for_review.py environment:
         Initial round: uv run python scripts/wait_for_review.py --pr <PR_NUMBER> --bot-name <bot>
         Subsequent rounds: attach `--body-file <session-dir>/review-reply.md` (must include commit hash and fix summary)
     - In GitHub MCP / GitHub App environment: retrieve `issue_comments`, `reviews`,
       and `inline_comments`, write the normalized JSON snapshot, then run:
       uv run python scripts/wait_for_review.py --bot-name <bot> --review-state-file <STATE.json>
  2. Evaluate exit code, then carefully read the entire result:
     - Exit 10: actionable findings are present. Read every `Inline Finding` block (path, line, full body).
     - Exit 0: clean pass / no findings. Exit 11: reviewer still in progress.
     - Exit 20: timeout; retry once, then escalate (outcome: blocked).
     - Exit 30: ambiguous verdict; inspect summary and inline findings before another review request or escalation. Exit 2 or 12: record and escalate.
     - Exit 10:
       a. Classify findings: adopt ONLY module contradictions and unmet Acceptance Criteria/regressions. Decline unoccurred edge cases (YAGNI) and out-of-scope items.
       b. For in-scope findings: fix code and add tests, verify local CI (<CI_ENTRYPOINT>), commit and push.
       c. For out-of-scope findings: do NOT modify code; file a follow-up Issue only if valuable, otherwise decline with rationale.
       d. Create `<session-dir>/review-reply.md` with fix details, commit hashes, rationales, and optional follow-up Issue references (Round X/5).
       e. Return to step 1.
     - Exit 0: terminate the loop and proceed to Step 12 (Outcome).
```

### Creating Review Reply File (`<session-dir>/review-reply.md`)
After addressing feedback and committing fixes, write a summary reply file explicitly detailing the modifications, commit hashes, and any out-of-scope follow-up Issues:
```markdown
## Addressing Review Feedback (Round 2/5)

### Changes & Resolutions
- [Addressed] Fixed bug in Finding A and added regression tests (commit: abc1234)
- [Declined - Out of Scope] Refactoring module X is out of scope for this Issue; filed follow-up Issue #123 (reason: ...)
- [Declined - YAGNI] Unoccurred edge case: Edge case Y has not occurred and exceeds PR acceptance criteria (reason: ...)
- [Declined] Preserved Finding B behavior as it conforms to intended specification (reason: ...)

@claude review
```

After writing the reply file, request re-review via `wait_for_review.py` (or manual PR comment).

### Diagnosing Exit 20 vs Exit 30 (Bot-Authored Trigger Failures)

`Exit 20` (no review activity / timeout) and `Exit 30` (activity exists but the
verdict could not be determined) look similar from the caller's side but have
different root causes and require different diagnosis:

- **Exit 20 (no activity at all)**: if the trigger comment was posted from a
  bot-authored execution environment (e.g. a hosted Claude Code environment,
  where GitHub records the actor as `claude[bot]` rather than a human
  account), first check the review workflow run for this PR on GitHub
  Actions:
  - If the run never appears, or the job shows `skipped`: the actor was not
    an allow-listed bot identity, or the trigger comment was missing the
    Orchestune trigger marker (`<!-- orchestune:review-trigger bot=claude -->`)
    that `wait_for_review.py` normally stamps automatically — a bot-authored
    trigger requires both an allow-listed actor and the marker (see the
    project's `claude-code-review.yml`-equivalent workflow). A missing marker
    usually means the trigger comment was posted by some other path than
    `post_review_trigger()` in `scripts/wait_for_review.py`.
  - If the job shows `failure` with `Workflow initiated by non-human actor`:
    the actor is a bot not present in the workflow's bot allow-list — this is
    expected for any other bot identity and is not a bug.
- **Exit 30 (activity present, verdict undetermined)**: the review did run and
  post something, so the bot-actor gate above already passed; look at the
  actual review body / inline comments returned by `wait_for_review.py`
  instead of the Actions run.

In both cases, `gh run list --workflow <review-workflow-file> --json databaseId,event,status,conclusion`
finds the run, then `gh api repos/{owner}/{repo}/actions/runs/<run-id> --jq '.actor.login'`
shows the triggering actor (neither `gh run list --json` nor `gh run view --json`
exposes an actor field) and `gh run view <run-id> --json jobs` shows each job's
conclusion.
