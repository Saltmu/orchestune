# Review Loop Reference (Step 11)

This document provides detailed procedures for automated LLM PR reviews and feedback resolution cycles.

---

## 11. Automated LLM PR Review Loop (Review Cycle)

Execute `scripts/wait_for_review.py` synchronously to request a review from a reviewer bot (Claude / Codex), wait for completion, and analyze feedback. Double-posting is prevented by the script's internal wait controls. The cumulative round count is tracked via `@<bot> review` comments and `Round X/5` notations, preserving count across session interruptions.

### Review Loop Control Flow (Pseudocode)

```text
Loop (up to 5 rounds):
  1. Execute wait command:
     - Initial round: poetry run python scripts/wait_for_review.py --pr <PR_NUMBER> --bot-name <bot>
     - Subsequent rounds: attach --body-file /tmp/review_reply.md to request re-review
  2. Evaluate output and context:
     - On timeout (exit 1):
         -> Retry once with --no-post --timeout 300. If still unresolved, escalate.
     - On review result obtained (exit 0):
         -> LLM carefully reads latest summary and inline comments.
         -> (a) Actionable findings exist:
             Fix code and add tests according to feedback.
             Verify with local CI (./scripts/local-ci.sh / .\\scripts\\local-ci.ps1), then commit & push.
             Create /tmp/review_reply.md (with Round X/5 header) and return to start of loop (1).
         -> (b) No actionable findings (LGTM / All checks passed / No blocking issues):
             Terminate loop. Proceed to Step 12 (Outcome).
     - On reaching round limit (Round 5):
         -> Stop automated iteration. Summarize discussion and escalate on PR.
     - On internal error (exit 2):
         -> Terminate with error. Record error output and stop.
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

After writing the reply file, run `wait_for_review.py` with `--body-file /tmp/review_reply.md` to trigger and wait for re-review.
