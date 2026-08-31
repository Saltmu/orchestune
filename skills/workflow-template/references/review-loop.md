# Review Loop Reference (Step 11)

This document provides detailed procedures for automated LLM PR reviews and feedback resolution cycles.

Keep the review loop in the same worktree used to create the PR. Apply feedback,
run CI, commit, and push only from that worktree.

---

## 11. Automated LLM PR Review Loop (Review Cycle)

After creating a PR, conduct automated LLM PR reviews using the reviewer bot decided in Step 1 (or resolved by dispatch/context) for objective quality verification and iterate until all actionable findings are resolved.

### Handling Review Findings and Scope Management

When findings are returned (Exit 10):
1. **Scope Verification against Acceptance Criteria (Anchor)**:
   - Cross-check each finding against the PR's declared **Acceptance Criteria**.
   - If a finding is necessary to fulfill the Acceptance Criteria or fixes a regression introduced by the PR, classify it as **In-Scope**.
   - If a finding proposes speculative failure recoveries, extra abstraction, unrelated refactoring, or features beyond the stated Acceptance Criteria, classify it as **Out-of-Scope (YAGNI)**.
2. **In-Scope Findings**:
   - Address the feedback by updating code and adding/modifying tests in the worktree.
   - Run local CI (`<CI_ENTRYPOINT>`) to ensure all checks pass.
   - Commit and push the fixes to the PR branch.
   - Note the commit hash and summary of changes for inclusion in the re-review reply.
3. **Out-of-Scope Findings**:
   - **Do NOT implement** out-of-scope changes in the current PR (avoid scope creep).
   - If the suggestion is genuinely valuable for future work, file a new follow-up GitHub Issue to track it separately via the selected backend.
   - If the suggestion is speculative or unneeded (YAGNI), decline it with an explicit rationale without filing a follow-up Issue.
   - In the re-review reply, document the decline rationale referencing the PR's Acceptance Criteria and any optional follow-up Issue number (e.g. `[Declined - Out of Scope] Exceeds PR acceptance criteria; deferred to follow-up Issue #...` or `[Declined - YAGNI] ...`).
4. **Re-Review Reply Documentation**:
   - Always include the detailed resolution summary (with commit hashes for addressed items, rationales, and optional follow-up Issue numbers for out-of-scope items) in `/tmp/review_reply.md`.

### Review Loop Control Flow (Pseudocode)

```text
Loop (up to 5 rounds):
  1. Acquire review state and execute the shared verdict evaluator:
     - In wait_for_review.py environment:
         Initial round: poetry run python scripts/wait_for_review.py --pr <PR_NUMBER> --bot-name <bot>
         Subsequent rounds: attach --body-file /tmp/review_reply.md (must include commit hash and fix summary)
     - In GitHub MCP / GitHub App environment: retrieve `issue_comments`, `reviews`,
       and `inline_comments`, write the normalized JSON snapshot, then run:
       poetry run python scripts/wait_for_review.py --bot-name <bot> --review-state-file <STATE.json>
  2. Evaluate exit code, then carefully read the entire result:
     - Exit 10: actionable findings are present. This includes **any Codex inline
       comment**, even with a boilerplate summary such as "Here are some automated
       review suggestions". Read every `Inline Finding` block (path, line, full body).
     - Exit 0: clean pass / no findings. Exit 11: reviewer still in progress.
     - Exit 20: timeout; retry once, then escalate (outcome: blocked).
     - Exit 30: ambiguous verdict; inspect summary and inline findings before another
       review request or escalation. Exit 2 or 12: record and escalate.
     - Exit 10:
       a. Classify findings into in-scope vs out-of-scope (against Acceptance Criteria & YAGNI).
       b. For in-scope findings: fix code and add tests, verify local CI (<CI_ENTRYPOINT>), commit and push.
       c. For out-of-scope findings: do NOT modify code; file a follow-up Issue only if valuable, otherwise decline with rationale.
       d. Create /tmp/review_reply.md with fix details, commit hashes, rationales, and optional follow-up Issue references (Round X/5).
       e. Return to step 1.
     - Exit 0: terminate the loop and proceed to Step 12 (Outcome).
```

### Creating Review Reply File (`/tmp/review_reply.md`)
After addressing feedback and committing fixes, write a summary reply file explicitly detailing the modifications, commit hashes, and any out-of-scope follow-up Issues:
```markdown
## Addressing Review Feedback (Round 2/5)

### Changes & Resolutions
- [Addressed] Fixed bug in Finding A and added regression tests (commit: abc1234)
- [Declined - Out of Scope] Refactoring module X is out of scope for this Issue; filed follow-up Issue #123 (reason: ...)
- [Declined - YAGNI] Speculative recovery mechanism for Edge Case Y exceeds PR acceptance criteria (reason: ...)
- [Declined] Preserved Finding B behavior as it conforms to intended specification (reason: ...)

@claude review
```

After writing the reply file, request re-review via `wait_for_review.py` (or manual PR comment).

