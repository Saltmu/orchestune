"""Durable hold publication and release with live generation evidence."""

from __future__ import annotations

from typing import Any

from orchestune.complete.contracts import DownstreamPolicyRecord
from orchestune.labels import NOT_NEEDED_REVIEW_PENDING, StatusLabel
from orchestune.not_needed_review_contract import (
    NotNeededReview,
    review_operation_id,
)
from orchestune.outcome_record import OUTCOME_MARKER, OutcomeRecord, parse_from_comments


def review_requirement(
    policies: tuple[DownstreamPolicyRecord, ...],
) -> NotNeededReview:
    review = [p for p in policies if p.policy_kind == "not-needed-review"]
    if not review:
        return NotNeededReview("exempt-local")
    if len(review) != 1:
        raise ValueError("ambiguous review policy")
    return NotNeededReview("required", review_operation_id(review[0].policy_key))


def ensure_review_pending(forge: Any, issue: int) -> None:
    if NOT_NEEDED_REVIEW_PENDING not in forge.get_issue_labels(issue):
        forge.add_label(issue, NOT_NEEDED_REVIEW_PENDING)
    if NOT_NEEDED_REVIEW_PENDING not in forge.get_issue_labels(issue):
        raise RuntimeError("review hold not confirmed")


def current_review_outcome(forge: Any, issue: int) -> OutcomeRecord:
    comments = forge.list_all_issue_comments(issue)
    candidates = [c for c in comments if OUTCOME_MARKER in str(c.get("body", ""))]
    if not candidates:
        raise ValueError("current Outcome unavailable")
    # Forge returns all pages in chronological order. Parse only the newest
    # marker, never fall back past corruption or missing timestamps.
    latest = candidates[-1]
    outcome = parse_from_comments([latest])
    if outcome is None or outcome.issue != issue:
        raise ValueError("current Outcome malformed or mismatched")
    return outcome


def confirm_review_generation(
    forge: Any, issue: int, policy: DownstreamPolicyRecord
) -> None:
    outcome = current_review_outcome(forge, issue)
    operation = review_operation_id(policy.policy_key)
    if (
        outcome.result != "not-needed"
        or outcome.completion_id != policy.completion_id
        or outcome.claim_id
        != (
            None
            if policy.generation_id.startswith("unclaimed-")
            else policy.generation_id
        )
        or policy.metadata.get("operation_id") != operation
        or (
            outcome.not_needed_review is not None
            and outcome.not_needed_review != NotNeededReview("required", operation)
        )
    ):
        raise ValueError("current review generation mismatch")


def release_review_pending(
    forge: Any, issue: int, policy: DownstreamPolicyRecord, *, verdict: str
) -> None:
    confirm_review_generation(forge, issue, policy)
    labels = set(forge.get_issue_labels(issue))
    if verdict == "passed":
        comments = forge.list_all_issue_comments(issue)
        operation = review_operation_id(policy.policy_key)
        passed = f"<!-- orchestune:policy-review {operation} verdict=passed -->"
        failed = f"<!-- orchestune:policy-review {operation} verdict=failed -->"
        if (
            forge.get_issue_state(issue).upper() != "CLOSED"
            or not any(passed in str(c.get("body", "")) for c in comments)
            or any(failed in str(c.get("body", "")) for c in comments)
        ):
            raise RuntimeError("review pass and close not confirmed")
    else:
        target = (
            StatusLabel.QUEUED
            if verdict == "failed"
            else StatusLabel.BLOCKED_HUMAN_REVIEW
        )
        if target not in labels or StatusLabel.NOT_NEEDED in labels:
            raise RuntimeError("review withdrawal not confirmed")
    if NOT_NEEDED_REVIEW_PENDING in labels:
        forge.remove_label(issue, NOT_NEEDED_REVIEW_PENDING)
    if NOT_NEEDED_REVIEW_PENDING in forge.get_issue_labels(issue):
        raise RuntimeError("review hold removal not confirmed")
