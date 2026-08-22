# Review Loop Reference (Step 11)

This document provides detailed procedures for automated LLM PR reviews and feedback resolution cycles.

Keep the review loop in the same worktree used to create the PR. Apply feedback,
run CI, commit, and push only from that worktree.

---

## 11. Automated LLM PR Review Loop (Review Cycle)

After creating a PR, conduct automated LLM PR reviews for objective quality verification and iterate until all actionable findings are resolved.

### Review Loop Control Flow (Pseudocode)

```text
Loop (up to 5 rounds):
  1. Execute wait command:
     - In wait_for_review.py environment:
         Initial round: poetry run python scripts/wait_for_review.py --pr <PR_NUMBER> --bot-name <bot>
         Subsequent rounds: attach --body-file /tmp/review_reply.md to request re-review
     - In fallback environment (without script):
         Post PR comment (e.g. @claude review) and monitor review completion via Web UI / notifications
  2. Evaluate output and context:
     - On timeout / no response:
         -> Retry once. If still unresolved, escalate (outcome: blocked).
     - On review result obtained:
         -> LLM carefully reads latest summary and inline comments.
         -> (a) Actionable findings exist:
             Fix code and add tests according to feedback.
             Verify with local CI (<CI_ENTRYPOINT>), then commit & push.
             Create /tmp/review_reply.md (with Round X/5 header) and return to start of loop (1).
         -> (b) No actionable findings (LGTM / All checks passed / No blocking issues):
             Terminate loop. Proceed to Step 12 (Outcome).
     - On reaching round limit (Round 5):
         -> Stop automated iteration. Summarize discussion and escalate on PR.
```

### Creating Review Reply File (`/tmp/review_reply.md`)
After addressing feedback, write a summary reply file:
```markdown
## Addressing Review Feedback (Round 2/5)

### Changes
- [Addressed] Fixed bug in Finding A and added regression tests (commit: abc1234)
- [Declined] Preserved Finding B behavior as it conforms to intended specification (reason: ...)

@claude review
```

After writing the reply file, request re-review via `wait_for_review.py` (or manual PR comment).
