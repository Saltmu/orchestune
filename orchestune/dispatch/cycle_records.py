"""Frozen receipts issued by GC completion decisions (#882).

A `CompletionReceipt` is minted only by the two GC completion decisions that
are genuinely confirmed: a normal `"completed"` action (a `RESULT_DONE`
Outcome Record backed by new commits) and a *verified* `"already_merged"`
action (`decide_prior_parent_merge_completion`'s double-checked historical
merge). It must never be inferred from a `"completed"`-prefix match on the
action string (`"completed_no_commits"` / `"completed_without_outcome"` also
share that prefix but are not confirmed completions) or from diffing
`completed_worktrees` history.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    """Evidence that one Issue's GC completion decision is confirmed."""

    issue_number: int


__all__ = ["CompletionReceipt"]
