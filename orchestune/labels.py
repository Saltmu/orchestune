"""Constants and Enums for GitHub issue and PR labels."""

from __future__ import annotations

from enum import StrEnum

STATUS_LABEL_PREFIX = "status:"


class StatusLabel(StrEnum):
    """The canonical status labels managed by Orchestune."""

    QUEUED = "status:queued"
    BLOCKED = "status:blocked"
    BLOCKED_RECOMPUTE = "status:blocked-recompute"
    BLOCKED_HUMAN_REVIEW = "status:blocked-human-review"
    DONE = "status:done"
    EXTERNAL_LOCK = "status:external-lock"
    FORCE_SERIAL = "status:force-serial"
    IN_PROGRESS = "status:in-progress"
    MANUAL_MERGE_REQUIRED = "status:manual-merge-required"
    NOT_NEEDED = "status:not-needed"


ALL_STATUS_LABELS: tuple[StatusLabel, ...] = tuple(StatusLabel)

# Related labels used across orchestration workflows
CI_BASE_BRANCH_RED = "ci:base-branch-red"
INTEGRATION_INCLUDED = "integration:included"
INTEGRATION_PARENT_BRANCH_STALE = "integration:parent-branch-stale"
NOT_NEEDED_REVIEW_PASSED = "not-needed-review:passed"
NOT_NEEDED_REVIEW_FAILED = "not-needed-review:failed"

__all__ = [
    "ALL_STATUS_LABELS",
    "CI_BASE_BRANCH_RED",
    "INTEGRATION_INCLUDED",
    "INTEGRATION_PARENT_BRANCH_STALE",
    "NOT_NEEDED_REVIEW_FAILED",
    "NOT_NEEDED_REVIEW_PASSED",
    "STATUS_LABEL_PREFIX",
    "StatusLabel",
]
