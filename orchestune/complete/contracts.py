"""Contracts and domain models for the task completion and outcome lifecycle (#997)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from orchestune.exit_codes import TaskExitCode, complete_failure_exit_code
from orchestune.outcome_record import (
    MAX_REASON_LENGTH,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    VALID_RESULTS,
    OutcomeRecord,
    ReviewSummary,
    is_known_reason,
)
from orchestune.ownership_contracts import OwnerKind


class CompleteStage(str, Enum):
    """Progression stages of complete side-effects.

    The complete CLI advances through these stages up to GC handoff.
    Physical worktree removal and completion receipts are reserved
    exclusively for the GC subsystem.
    """

    INITIALIZING = "initializing"
    PREFLIGHT_VALIDATING = "preflight_validating"
    EVIDENCE_VERIFYING = "evidence_verifying"
    JOURNALING = "journaling"
    POSTING = "posting"
    HANDED_OFF_TO_GC = "handed_off_to_gc"
    # Label-confirmed lifecycle stages. The legacy POSTING -> HANDED_OFF_TO_GC
    # path stays available until the new writer and readers switch together.
    RESERVED = "reserved"
    OUTCOME_POSTED = "outcome_posted"
    LABEL_CONFIRMED = "label_confirmed"
    HANDED_OFF = "handed_off"


_VALID_STAGE_TRANSITIONS: dict[CompleteStage, frozenset[CompleteStage]] = {
    CompleteStage.INITIALIZING: frozenset({CompleteStage.PREFLIGHT_VALIDATING}),
    CompleteStage.PREFLIGHT_VALIDATING: frozenset({CompleteStage.EVIDENCE_VERIFYING}),
    CompleteStage.EVIDENCE_VERIFYING: frozenset({CompleteStage.JOURNALING}),
    CompleteStage.JOURNALING: frozenset(
        {CompleteStage.POSTING, CompleteStage.RESERVED}
    ),
    CompleteStage.POSTING: frozenset({CompleteStage.HANDED_OFF_TO_GC}),
    CompleteStage.HANDED_OFF_TO_GC: frozenset(),
    CompleteStage.RESERVED: frozenset({CompleteStage.OUTCOME_POSTED}),
    CompleteStage.OUTCOME_POSTED: frozenset({CompleteStage.LABEL_CONFIRMED}),
    CompleteStage.LABEL_CONFIRMED: frozenset({CompleteStage.HANDED_OFF}),
    CompleteStage.HANDED_OFF: frozenset(),
}


def can_transition(from_stage: CompleteStage, to_stage: CompleteStage) -> bool:
    """Return whether transitioning directly from from_stage to to_stage is allowed."""
    allowed = _VALID_STAGE_TRANSITIONS.get(from_stage, frozenset())
    return to_stage in allowed


CompleteExitCode = TaskExitCode


class CompleteFailureReason(str, Enum):
    """Machine-readable failure reasons for rejected or faulted completion attempts."""

    INVALID_REQUEST = "invalid_request"
    CLAIM_NOT_FOUND = "claim_not_found"
    OWNER_TOKEN_MISMATCH = "owner_token_mismatch"
    INVALID_RESULT_PAYLOAD = "invalid_result_payload"
    PR_REQUIRED = "pr_required"
    PR_PROHIBITED = "pr_prohibited"
    REASON_REQUIRED = "reason_required"
    DIRTY_WORKTREE = "dirty_worktree"
    EVIDENCE_MISSING = "evidence_missing"
    STATE_LOCK_FAILED = "state_lock_failed"
    CONCURRENT_COMPLETION = "concurrent_completion"
    INVALID_STAGE_TRANSITION = "invalid_stage_transition"
    FORGE_POST_FAILED = "forge_post_failed"
    STATE_SAVE_FAILED = "state_save_failed"
    COMPLETION_RESERVATION_NOT_FOUND = "completion_reservation_not_found"
    REPOSITORY_IDENTITY_MISMATCH = "repository_identity_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    REQUEST_FINGERPRINT_MISMATCH = "request_fingerprint_mismatch"
    LABEL_ADD_FAILED = "label_add_failed"
    LABEL_CLEANUP_INCOMPLETE = "label_cleanup_incomplete"
    LABEL_STATE_UNKNOWN = "label_state_unknown"
    LABEL_CONFLICT = "label_conflict"
    PUBLICATION_POLICY_FAILED = "publication_policy_failed"


def failure_reason_to_exit_code(reason: CompleteFailureReason) -> CompleteExitCode:
    """Map a CompleteFailureReason to its corresponding CompleteExitCode."""
    if not isinstance(reason, CompleteFailureReason):
        raise KeyError(f"Unmapped complete failure reason: {reason!r}")
    return complete_failure_exit_code(reason.value)


class CompletionLabelStatus(str, Enum):
    """Outcome of a live, completion-specific status-label transition."""

    CONFIRMED = "confirmed"
    ADD_FAILED = "add_failed"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CompletionLabelTransitionResult:
    """Observed result returned by the completion label transition helper."""

    status: CompletionLabelStatus
    target_label: str
    observed_labels: tuple[str, ...] = ()
    failed_operation: str | None = None
    failure_reason: CompleteFailureReason | None = None

    @property
    def confirmed(self) -> bool:
        """Whether the requested target label was observed after the transition."""
        return self.status is CompletionLabelStatus.CONFIRMED

    @property
    def exit_code(self) -> CompleteExitCode:
        """Return success or the stable failure code for this label transition."""
        if self.confirmed:
            return CompleteExitCode.SUCCESS
        assert self.failure_reason is not None
        return failure_reason_to_exit_code(self.failure_reason)

    def __post_init__(self) -> None:
        if not isinstance(self.status, CompletionLabelStatus):
            raise ValueError("status must be a CompletionLabelStatus")
        if not isinstance(self.target_label, str) or not self.target_label.strip():
            raise ValueError("target_label must be a non-empty string")
        if any(
            not isinstance(label, str) or not label.strip()
            for label in self.observed_labels
        ):
            raise ValueError("observed_labels must contain non-empty strings")
        if self.failed_operation is not None and (
            not isinstance(self.failed_operation, str)
            or not self.failed_operation.strip()
        ):
            raise ValueError("failed_operation must be None or a non-empty string")
        if self.status is CompletionLabelStatus.CONFIRMED:
            if self.target_label not in self.observed_labels:
                raise ValueError(
                    "confirmed label result requires target_label in observed_labels"
                )
            if self.failure_reason is not None or self.failed_operation is not None:
                raise ValueError(
                    "confirmed label result cannot contain failure details"
                )
            return
        if self.failure_reason is None:
            raise ValueError("failed label result requires failure_reason")
        expected = {
            CompletionLabelStatus.ADD_FAILED: CompleteFailureReason.LABEL_ADD_FAILED,
            CompletionLabelStatus.CLEANUP_INCOMPLETE: CompleteFailureReason.LABEL_CLEANUP_INCOMPLETE,
            CompletionLabelStatus.UNKNOWN: CompleteFailureReason.LABEL_STATE_UNKNOWN,
            CompletionLabelStatus.CONFLICT: CompleteFailureReason.LABEL_CONFLICT,
        }[self.status]
        if self.failure_reason is not expected:
            raise ValueError(
                f"{self.status.value} requires failure_reason={expected.value}"
            )


def is_valid_positive_int(val: Any) -> bool:
    """Return whether val is a valid non-boolean positive integer."""
    return isinstance(val, int) and not isinstance(val, bool) and val > 0


def is_valid_pr_number(pr: Any) -> bool:
    """Return whether pr is a valid non-boolean positive integer."""
    return is_valid_positive_int(pr)


def is_valid_issue_number(issue: Any) -> bool:
    """Return whether issue is a valid non-boolean positive integer."""
    return is_valid_positive_int(issue)


def is_valid_attempt_number(attempt: Any) -> bool:
    """Return whether attempt is None or a valid non-boolean positive integer."""
    if attempt is None:
        return True
    return is_valid_positive_int(attempt)


def is_valid_rounds_number(rounds: Any) -> bool:
    """Return whether rounds is None or a valid non-boolean positive integer."""
    if rounds is None:
        return True
    return is_valid_positive_int(rounds)


def is_valid_review_summary(review: Any) -> bool:
    """Return whether review is a ReviewSummary with valid non-boolean rounds."""
    if not isinstance(review, ReviewSummary):
        return False
    return is_valid_rounds_number(review.rounds)


def sanitize_blocked_reason(reason: Any) -> str:
    """Sanitize and return reason string, stripping whitespace, control characters, and capping length."""
    if not isinstance(reason, str):
        return ""
    cleaned = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in reason)
    return " ".join(cleaned.split())[:MAX_REASON_LENGTH]


def _validate_record_reason(rec: OutcomeRecord) -> None:
    """Validate canonical form of reason in an embedded OutcomeRecord."""
    if rec.result == RESULT_BLOCKED:
        sanitized = sanitize_blocked_reason(rec.reason)
        if not sanitized or rec.reason != sanitized:
            raise ValueError(
                "Blocked outcome_record requires a canonical reason string equal to its sanitized form, "
                f"got: {rec.reason!r}"
            )
    else:
        if rec.reason is not None and not is_known_reason(rec.reason):
            raise ValueError(
                f"Non-blocked outcome_record reason must be None or in VALID_REASONS, got: {rec.reason!r}"
            )


@dataclass(frozen=True)
class DonePayload:
    """Input payload specific to successful done outcomes."""

    pr: int
    review: ReviewSummary = field(default_factory=ReviewSummary)
    ci: str | None = None
    baseline_regressions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_valid_pr_number(self.pr):
            raise ValueError(
                f"pr must be a valid positive non-boolean integer, got: {self.pr!r}"
            )
        if not is_valid_review_summary(self.review):
            raise ValueError(
                "review must be a ReviewSummary with rounds as None or a valid positive non-boolean integer, "
                f"got: {self.review!r}"
            )


@dataclass(frozen=True)
class NotNeededPayload:
    """Input payload specific to not-needed outcomes.

    not-needed outcomes carry no additional fields; the canonical OutcomeRecord
    schema for not-needed consists solely of issue and result.
    """


@dataclass(frozen=True)
class BlockedPayload:
    """Input payload specific to blocked outcomes."""

    reason: str
    base_sha: str | None = None
    attempt: int | None = None
    review: ReviewSummary = field(default_factory=ReviewSummary)
    ci: str | None = None

    def __post_init__(self) -> None:
        sanitized = sanitize_blocked_reason(self.reason)
        if not sanitized:
            raise ValueError(
                "reason must be a non-empty string containing non-whitespace characters, "
                f"got: {self.reason!r}"
            )
        object.__setattr__(self, "reason", sanitized)
        if not is_valid_attempt_number(self.attempt):
            raise ValueError(
                f"attempt must be None or a valid positive non-boolean integer, got: {self.attempt!r}"
            )
        if not is_valid_review_summary(self.review):
            raise ValueError(
                "review must be a ReviewSummary with rounds as None or a valid positive non-boolean integer, "
                f"got: {self.review!r}"
            )


CompletePayload = DonePayload | NotNeededPayload | BlockedPayload


@dataclass(frozen=True)
class CompleteFailure:
    """Structured diagnostics for a failed complete attempt."""

    reason: CompleteFailureReason
    message: str
    issue_number: int | None = None
    conflicting_stage: CompleteStage | None = None
    next_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.issue_number is not None and not is_valid_issue_number(
            self.issue_number
        ):
            raise ValueError(
                f"issue_number must be None or a valid positive non-boolean integer, got: {self.issue_number!r}"
            )

    @property
    def exit_code(self) -> CompleteExitCode:
        """Derive exit code directly from the machine-readable reason."""
        return failure_reason_to_exit_code(self.reason)


@dataclass(frozen=True)
class CompleteRequest:
    """Parameters required to complete a task."""

    issue_number: int
    result: str  # "done" | "not-needed" | "blocked"
    owner_token: str | None = field(default=None, repr=False)
    claim_id: str | None = None
    owner_kind: OwnerKind = OwnerKind.INTERACTIVE
    payload: CompletePayload | None = None
    dry_run: bool = False
    state_path: Path | None = None
    worktree_root: Path | None = None

    def __post_init__(self) -> None:
        if not is_valid_issue_number(self.issue_number):
            raise ValueError(
                f"issue_number must be a valid positive non-boolean integer, got: {self.issue_number!r}"
            )

    @classmethod
    def done(
        cls,
        issue_number: int,
        pr: int,
        *,
        owner_token: str | None = None,
        claim_id: str | None = None,
        owner_kind: OwnerKind = OwnerKind.INTERACTIVE,
        review: ReviewSummary | None = None,
        ci: str | None = None,
        baseline_regressions: tuple[str, ...] = (),
        dry_run: bool = False,
        state_path: Path | None = None,
        worktree_root: Path | None = None,
    ) -> CompleteRequest:
        """Construct a validated request for done outcomes."""
        payload = DonePayload(
            pr=pr,
            review=review or ReviewSummary(),
            ci=ci,
            baseline_regressions=tuple(baseline_regressions),
        )
        return cls(
            issue_number=issue_number,
            result=RESULT_DONE,
            owner_token=owner_token,
            claim_id=claim_id,
            owner_kind=owner_kind,
            payload=payload,
            dry_run=dry_run,
            state_path=state_path,
            worktree_root=worktree_root,
        )

    @classmethod
    def not_needed(
        cls,
        issue_number: int,
        *,
        owner_token: str | None = None,
        claim_id: str | None = None,
        owner_kind: OwnerKind = OwnerKind.INTERACTIVE,
        dry_run: bool = False,
        state_path: Path | None = None,
        worktree_root: Path | None = None,
    ) -> CompleteRequest:
        """Construct a validated request for not-needed outcomes."""
        return cls(
            issue_number=issue_number,
            result=RESULT_NOT_NEEDED,
            owner_token=owner_token,
            claim_id=claim_id,
            owner_kind=owner_kind,
            payload=NotNeededPayload(),
            dry_run=dry_run,
            state_path=state_path,
            worktree_root=worktree_root,
        )

    @classmethod
    def blocked(
        cls,
        issue_number: int,
        reason: str,
        *,
        base_sha: str | None = None,
        attempt: int | None = None,
        owner_token: str | None = None,
        claim_id: str | None = None,
        owner_kind: OwnerKind = OwnerKind.INTERACTIVE,
        review: ReviewSummary | None = None,
        ci: str | None = None,
        dry_run: bool = False,
        state_path: Path | None = None,
        worktree_root: Path | None = None,
    ) -> CompleteRequest:
        """Construct a validated request for blocked outcomes."""
        payload = BlockedPayload(
            reason=reason,
            base_sha=base_sha,
            attempt=attempt,
            review=review or ReviewSummary(),
            ci=ci,
        )
        return cls(
            issue_number=issue_number,
            result=RESULT_BLOCKED,
            owner_token=owner_token,
            claim_id=claim_id,
            owner_kind=owner_kind,
            payload=payload,
            dry_run=dry_run,
            state_path=state_path,
            worktree_root=worktree_root,
        )

    def validate(self) -> None:
        """Validate internal consistency of request fields and payload."""
        if not is_valid_issue_number(self.issue_number):
            raise ValueError(
                f"issue_number must be a valid positive non-boolean integer, got: {self.issue_number!r}"
            )

        if self.result not in VALID_RESULTS:
            raise ValueError(f"Invalid complete result: {self.result!r}")

        if self.result == RESULT_DONE:
            if (
                not isinstance(self.payload, DonePayload)
                or not is_valid_pr_number(self.payload.pr)
                or not is_valid_review_summary(self.payload.review)
            ):
                raise ValueError(
                    "Done request requires a DonePayload with a valid positive integer pr and valid review"
                )
        elif self.result == RESULT_NOT_NEEDED:
            if self.payload is not None and not isinstance(
                self.payload, NotNeededPayload
            ):
                raise ValueError(
                    "Not-needed request must only have NotNeededPayload or None"
                )
        elif self.result == RESULT_BLOCKED:
            if (
                not isinstance(self.payload, BlockedPayload)
                or not sanitize_blocked_reason(self.payload.reason)
                or not is_valid_attempt_number(self.payload.attempt)
                or not is_valid_review_summary(self.payload.review)
            ):
                raise ValueError(
                    "Blocked request requires a BlockedPayload with a non-empty, non-whitespace reason"
                )

    def to_outcome_record(self) -> OutcomeRecord:
        """Derive an OutcomeRecord corresponding to this validated complete request."""
        self.validate()
        if self.result == RESULT_DONE:
            assert isinstance(self.payload, DonePayload)
            return OutcomeRecord(
                result=RESULT_DONE,
                issue=self.issue_number,
                pr=self.payload.pr,
                review=self.payload.review,
                ci=self.payload.ci,
                baseline_regressions=self.payload.baseline_regressions,
            )
        elif self.result == RESULT_NOT_NEEDED:
            return OutcomeRecord(
                result=RESULT_NOT_NEEDED,
                issue=self.issue_number,
            )
        elif self.result == RESULT_BLOCKED:
            assert isinstance(self.payload, BlockedPayload)
            return OutcomeRecord(
                result=RESULT_BLOCKED,
                issue=self.issue_number,
                reason=self.payload.reason,
                base_sha=self.payload.base_sha,
                attempt=self.payload.attempt,
                review=self.payload.review,
                ci=self.payload.ci,
            )
        raise ValueError(f"Unsupported complete result: {self.result!r}")

    @property
    def request_fingerprint(self) -> str:
        """Canonical digest of caller intent, excluding generated completion data."""
        return completion_request_fingerprint(self)


def completion_request_fingerprint(request: CompleteRequest) -> str:
    """Hash stable user-supplied completion fields in canonical JSON form."""
    request.validate()
    payload: dict[str, Any] = {
        "issue": request.issue_number,
        "result": request.result,
    }
    if isinstance(request.payload, DonePayload):
        payload.update(
            {
                "pr": request.payload.pr,
                "review": request.payload.review.to_dict(),
                "ci": request.payload.ci,
                "baseline_regressions": list(request.payload.baseline_regressions),
            }
        )
    elif isinstance(request.payload, BlockedPayload):
        payload.update(
            {
                "reason": request.payload.reason,
                "base_sha": request.payload.base_sha,
                "attempt": request.payload.attempt,
                "review": request.payload.review.to_dict(),
                "ci": request.payload.ci,
            }
        )
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CompleteResult:
    """Outcome of a complete attempt.

    A successful apply advances through complete stages up to GC handoff
    (`handed_off_to_gc=True`). A successful preview is explicitly marked and
    stops at preflight without claiming that side effects occurred.
    Both strictly exclude CompletionReceipt, which is owned by GC.
    """

    success: bool
    issue_number: int
    result: str
    stage: CompleteStage
    claim_id: str | None = None
    owner_kind: OwnerKind | None = None
    pr: int | None = None
    outcome_record: OutcomeRecord | None = None
    failure: CompleteFailure | None = None
    handed_off_to_gc: bool = False
    preview: bool = False
    completion_id: str | None = None

    def __post_init__(self) -> None:
        self._validate_identifiers()
        self._validate_outcome_record()
        self._validate_stage_boundary()

    def _validate_identifiers(self) -> None:
        if not is_valid_issue_number(self.issue_number):
            raise ValueError(
                f"issue_number must be a valid positive non-boolean integer, got: {self.issue_number!r}"
            )
        if self.result not in VALID_RESULTS:
            raise ValueError(f"Invalid complete result: {self.result!r}")
        if self.pr is not None and not is_valid_pr_number(self.pr):
            raise ValueError(
                f"pr must be None or a valid positive non-boolean integer, got: {self.pr!r}"
            )
        if self.completion_id is not None and (
            not isinstance(self.completion_id, str) or not self.completion_id.strip()
        ):
            raise ValueError("completion_id must be None or a non-empty string")

    def _validate_outcome_record(self) -> None:
        if self.outcome_record is None:
            return
        rec = self.outcome_record
        if not isinstance(rec, OutcomeRecord):
            raise ValueError(
                f"outcome_record must be an OutcomeRecord or None, got: {rec!r}"
            )
        if not is_valid_issue_number(rec.issue):
            raise ValueError(
                f"outcome_record.issue must be a valid positive non-boolean integer, got: {rec.issue!r}"
            )
        if rec.issue != self.issue_number:
            raise ValueError(
                f"outcome_record.issue ({rec.issue}) does not match "
                f"CompleteResult.issue_number ({self.issue_number})"
            )
        if rec.result != self.result:
            raise ValueError(
                f"outcome_record.result ({rec.result!r}) does not match "
                f"CompleteResult.result ({self.result!r})"
            )
        if rec.pr is not None and not is_valid_pr_number(rec.pr):
            raise ValueError(
                f"outcome_record.pr must be None or a valid positive non-boolean integer, got: {rec.pr!r}"
            )
        if rec.pr != self.pr:
            raise ValueError(
                f"outcome_record.pr ({rec.pr}) does not match CompleteResult.pr ({self.pr})"
            )
        if not is_valid_attempt_number(rec.attempt):
            raise ValueError(
                "outcome_record.attempt must be None or a valid positive non-boolean integer, "
                f"got: {rec.attempt!r}"
            )
        if not is_valid_review_summary(rec.review):
            raise ValueError(
                "outcome_record.review must be a ReviewSummary with rounds as None or a valid positive non-boolean integer, "
                f"got: {rec.review!r}"
            )
        _validate_record_reason(rec)

    def _validate_stage_boundary(self) -> None:
        if self.success:
            if self.preview:
                if self.stage != CompleteStage.PREFLIGHT_VALIDATING:
                    raise ValueError(
                        "Preview CompleteResult requires CompleteStage.PREFLIGHT_VALIDATING, "
                        f"got: {self.stage!r}"
                    )
                if self.handed_off_to_gc:
                    raise ValueError("Preview CompleteResult cannot claim GC handoff")
            elif self.stage != CompleteStage.HANDED_OFF_TO_GC:
                raise ValueError(
                    "Successful CompleteResult requires CompleteStage.HANDED_OFF_TO_GC, "
                    f"got: {self.stage!r}"
                )
            elif not self.handed_off_to_gc:
                raise ValueError(
                    "Successful CompleteResult requires handed_off_to_gc=True"
                )
            if self.failure is not None:
                raise ValueError(
                    "Successful CompleteResult cannot have a failure object"
                )
        else:
            if self.preview:
                raise ValueError("Failed CompleteResult cannot be a preview")
            if self.stage == CompleteStage.HANDED_OFF_TO_GC:
                raise ValueError(
                    "Failed CompleteResult cannot be at CompleteStage.HANDED_OFF_TO_GC"
                )
            if self.stage == CompleteStage.HANDED_OFF:
                raise ValueError(
                    "Failed CompleteResult cannot be at CompleteStage.HANDED_OFF"
                )
            if self.handed_off_to_gc:
                raise ValueError(
                    "Failed CompleteResult cannot have handed_off_to_gc=True"
                )
            if self.failure is None:
                raise ValueError(
                    "Failed CompleteResult requires a CompleteFailure object"
                )
            if (
                self.failure.issue_number is not None
                and self.failure.issue_number != self.issue_number
            ):
                raise ValueError(
                    f"failure.issue_number ({self.failure.issue_number}) does not match "
                    f"CompleteResult.issue_number ({self.issue_number})"
                )

    @classmethod
    def success_result(
        cls,
        issue_number: int,
        result: str,
        stage: CompleteStage = CompleteStage.HANDED_OFF_TO_GC,
        *,
        claim_id: str | None = None,
        owner_kind: OwnerKind | None = None,
        pr: int | None = None,
        outcome_record: OutcomeRecord | None = None,
        completion_id: str | None = None,
    ) -> CompleteResult:
        """Construct a successful CompleteResult indicating GC handoff."""
        if result not in VALID_RESULTS:
            raise ValueError(f"Invalid complete result: {result!r}")
        if stage != CompleteStage.HANDED_OFF_TO_GC:
            raise ValueError(
                "Successful CompleteResult requires CompleteStage.HANDED_OFF_TO_GC, "
                f"got: {stage!r}"
            )
        return cls(
            success=True,
            issue_number=issue_number,
            result=result,
            stage=stage,
            claim_id=claim_id,
            owner_kind=owner_kind,
            pr=pr,
            outcome_record=outcome_record,
            failure=None,
            handed_off_to_gc=True,
            completion_id=completion_id,
        )

    @classmethod
    def failure_result(
        cls,
        issue_number: int,
        result: str,
        stage: CompleteStage,
        failure: CompleteFailure,
        *,
        claim_id: str | None = None,
        owner_kind: OwnerKind | None = None,
        completion_id: str | None = None,
    ) -> CompleteResult:
        """Construct a failed CompleteResult with diagnostic information."""
        if result not in VALID_RESULTS:
            raise ValueError(f"Invalid complete result: {result!r}")
        if stage == CompleteStage.HANDED_OFF_TO_GC:
            raise ValueError(
                "Failed CompleteResult cannot be at CompleteStage.HANDED_OFF_TO_GC"
            )
        if stage == CompleteStage.HANDED_OFF:
            raise ValueError(
                "Failed CompleteResult cannot be at CompleteStage.HANDED_OFF"
            )
        if failure.issue_number is not None and failure.issue_number != issue_number:
            raise ValueError(
                f"failure.issue_number ({failure.issue_number}) does not match "
                f"CompleteResult.issue_number ({issue_number})"
            )
        return cls(
            success=False,
            issue_number=issue_number,
            result=result,
            stage=stage,
            claim_id=claim_id,
            owner_kind=owner_kind,
            pr=None,
            outcome_record=None,
            failure=failure,
            handed_off_to_gc=False,
            completion_id=completion_id,
        )

    @classmethod
    def preview_result(
        cls,
        issue_number: int,
        result: str,
        *,
        claim_id: str | None = None,
        owner_kind: OwnerKind | None = None,
        pr: int | None = None,
        outcome_record: OutcomeRecord | None = None,
        completion_id: str | None = None,
    ) -> CompleteResult:
        """Construct a validated, side-effect-free completion preview."""
        return cls(
            success=True,
            issue_number=issue_number,
            result=result,
            stage=CompleteStage.PREFLIGHT_VALIDATING,
            claim_id=claim_id,
            owner_kind=owner_kind,
            pr=pr,
            outcome_record=outcome_record,
            failure=None,
            handed_off_to_gc=False,
            preview=True,
            completion_id=completion_id,
        )
