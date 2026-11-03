"""Release-to-review status transition and target validation (#1643).

Helper module for operator recovery to transition an abandoned interactive claim
to status:blocked-human-review without creating circular dependencies with the recovery service.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import yaml

from orchestune.issue_parsing import FOOTPRINT_BLOCK_PATTERN
from orchestune.labels import StatusLabel
from orchestune.ledger.status_labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)


def _check_target_labels(labels: tuple[str, ...]) -> None:
    if StatusLabel.DONE in labels:
        raise ValueError("Cannot release completed issue (has status:done)")
    if StatusLabel.NOT_NEEDED in labels:
        raise ValueError("Cannot release cancelled issue (has status:not-needed)")
    other_terminal = [
        label
        for label in TERMINAL_ESCALATION_LABELS
        if label in labels and label != StatusLabel.BLOCKED_HUMAN_REVIEW
    ]
    if other_terminal:
        raise ValueError(
            f"Cannot overwrite existing terminal escalation: {other_terminal[0]}"
        )
    if (
        StatusLabel.IN_PROGRESS not in labels
        and StatusLabel.BLOCKED_HUMAN_REVIEW not in labels
    ):
        raise ValueError(
            "Issue must have status:in-progress or status:blocked-human-review"
        )


def _check_footprint_claim_id(body: str | None, claim_id: str) -> None:
    if not body:
        return
    match = FOOTPRINT_BLOCK_PATTERN.search(body)
    if not match:
        return
    try:
        data = yaml.safe_load(match.group(1))
        if isinstance(data, dict):
            issue_claim_id = data.get("claim_id")
            if issue_claim_id and issue_claim_id != claim_id:
                raise ValueError(
                    f"Issue body claim_id mismatch: expected {claim_id}, found {issue_claim_id}"
                )
    except ValueError:
        raise
    except Exception:
        pass


def verify_release_review_target(
    forge: Any, issue_number: int, claim_id: str
) -> tuple[Any, tuple[str, ...]]:
    """Validate that the Issue is eligible for release-to-review transition."""
    if forge is None:
        raise ValueError("Forge is required for release-to-review")
    issue = forge.get_issue(issue_number)
    if issue is None:
        raise ValueError(f"Issue #{issue_number} not found")

    state = (
        forge.get_issue_state(issue_number)
        if hasattr(forge, "get_issue_state")
        else getattr(issue, "state", "OPEN")
    )
    if str(state).upper() != "OPEN":
        raise ValueError(f"Release to review requires an OPEN issue, got {state}")

    labels = (
        tuple(forge.get_issue_labels(issue_number))
        if hasattr(forge, "get_issue_labels")
        else tuple(getattr(issue, "labels", ()))
    )
    _check_target_labels(labels)
    _check_footprint_claim_id(getattr(issue, "body", None), claim_id)

    old_labels = tuple(label for label in labels if label in PRIMARY_STATUS_LABELS)
    return issue, old_labels


def apply_release_review_label(
    forge: Any, issue_number: int, old_labels: Iterable[str]
) -> None:
    """Transition the issue to status:blocked-human-review, removing old primary status labels."""
    transition_status_label(
        forge,
        issue_number,
        StatusLabel.BLOCKED_HUMAN_REVIEW,
        old_labels,
    )


__all__ = [
    "apply_release_review_label",
    "verify_release_review_target",
]
