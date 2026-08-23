# Review Loop Reference (Step 11)

This document provides detailed procedures for automated LLM PR reviews and feedback resolution cycles.

Keep the review loop in the same worktree used to create the PR. Apply feedback,
run CI, commit, and push only from that worktree.

---

## 11. Automated LLM PR Review Loop (Review Cycle)

Execute `scripts/wait_for_review.py` synchronously to request a review from a reviewer bot (Claude / Codex), wait for completion, and analyze feedback. Double-posting is prevented by the script's internal wait controls. The cumulative round count is tracked via `@<bot> review` comments and `Round X/5` notations, preserving count across session interruptions.

### Review Loop Control Flow (Pseudocode)

```text
Loop (up to 5 rounds):
  1. Acquire review state and execute the shared verdict evaluator:
     - CLI/gh, initial round:
       poetry run python scripts/wait_for_review.py --pr <PR_NUMBER> --bot-name <bot>
     - CLI/gh, subsequent rounds: attach --body-file /tmp/review_reply.md.
     - GitHub MCP / GitHub App: retrieve `issue_comments`, `reviews`, and
       `inline_comments`, write the normalized JSON snapshot, then run:
       poetry run python scripts/wait_for_review.py --bot-name <bot> --review-state-file <STATE.json>
  2. Evaluate exit code, then carefully read the entire result:
     - Exit 10: actionable findings are present. This includes **any Codex inline
       comment**, even with a boilerplate summary such as "Here are some automated
       review suggestions". Read every `Inline Finding` block (path, line, full body).
     - Exit 0: clean pass / no findings. Exit 11: reviewer still in progress.
     - Exit 20: timeout; retry once with --no-post --timeout 300, then escalate.
     - Exit 30: ambiguous verdict; inspect summary and inline findings before
       requesting another review or escalating. Exit 2 or 12: record and escalate.
     - Exit 10: fix code and add tests, verify local CI, commit and push, then
       create /tmp/review_reply.md (Round X/5) and return to step 1.
     - Exit 0: terminate the loop and proceed to Step 12 (Outcome).
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
