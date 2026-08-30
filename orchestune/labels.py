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

__all__ = [
    "ALL_STATUS_LABELS",
    "STATUS_LABEL_PREFIX",
    "StatusLabel",
]
