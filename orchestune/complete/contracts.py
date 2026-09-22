"""Contracts and domain models for the task completion and outcome lifecycle (#997)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

from orchestune.claim.contracts import OwnerKind
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


_VALID_STAGE_TRANSITIONS: dict[CompleteStage, frozenset[CompleteStage]] = {
    CompleteStage.INITIALIZING: frozenset({CompleteStage.PREFLIGHT_VALIDATING}),
    CompleteStage.PREFLIGHT_VALIDATING: frozenset({CompleteStage.EVIDENCE_VERIFYING}),
    CompleteStage.EVIDENCE_VERIFYING: frozenset({CompleteStage.JOURNALING}),
    CompleteStage.JOURNALING: frozenset({CompleteStage.POSTING}),
    CompleteStage.POSTING: frozenset({CompleteStage.HANDED_OFF_TO_GC}),
    CompleteStage.HANDED_OFF_TO_GC: frozenset(),
}


def can_transition(from_stage: CompleteStage, to_stage: CompleteStage) -> bool:
    """Return whether transitioning directly from from_stage to to_stage is allowed."""
    allowed = _VALID_STAGE_TRANSITIONS.get(from_stage, frozenset())
    return to_stage in allowed


class CompleteExitCode(IntEnum):
    """Exit codes for complete operations."""

    SUCCESS = 0
    GENERIC_ERROR = 1

    # Preflight / Validation errors (10-19)
    INVALID_REQUEST = 10
    CLAIM_NOT_FOUND = 11
    OWNER_TOKEN_MISMATCH = 12
    INVALID_RESULT_PAYLOAD = 13
    PR_REQUIRED = 14
    PR_PROHIBITED = 15
    REASON_REQUIRED = 16
    DIRTY_WORKTREE = 17
    EVIDENCE_MISSING = 18

    # Concurrency / State errors (20-29)
    STATE_LOCK_FAILED = 20
    CONCURRENT_COMPLETION = 21
    INVALID_STAGE_TRANSITION = 22

    # Infrastructure / Network / Forge errors (30-39)
    FORGE_POST_FAILED = 30
    STATE_SAVE_FAILED = 31


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


_FAILURE_REASON_TO_EXIT_CODE: dict[CompleteFailureReason, CompleteExitCode] = {
    CompleteFailureReason.INVALID_REQUEST: CompleteExitCode.INVALID_REQUEST,
    CompleteFailureReason.CLAIM_NOT_FOUND: CompleteExitCode.CLAIM_NOT_FOUND,
    CompleteFailureReason.OWNER_TOKEN_MISMATCH: CompleteExitCode.OWNER_TOKEN_MISMATCH,
    CompleteFailureReason.INVALID_RESULT_PAYLOAD: CompleteExitCode.INVALID_RESULT_PAYLOAD,
    CompleteFailureReason.PR_REQUIRED: CompleteExitCode.PR_REQUIRED,
    CompleteFailureReason.PR_PROHIBITED: CompleteExitCode.PR_PROHIBITED,
    CompleteFailureReason.REASON_REQUIRED: CompleteExitCode.REASON_REQUIRED,
    CompleteFailureReason.DIRTY_WORKTREE: CompleteExitCode.DIRTY_WORKTREE,
    CompleteFailureReason.EVIDENCE_MISSING: CompleteExitCode.EVIDENCE_MISSING,
    CompleteFailureReason.STATE_LOCK_FAILED: CompleteExitCode.STATE_LOCK_FAILED,
    CompleteFailureReason.CONCURRENT_COMPLETION: CompleteExitCode.CONCURRENT_COMPLETION,
    CompleteFailureReason.INVALID_STAGE_TRANSITION: CompleteExitCode.INVALID_STAGE_TRANSITION,
    CompleteFailureReason.FORGE_POST_FAILED: CompleteExitCode.FORGE_POST_FAILED,
    CompleteFailureReason.STATE_SAVE_FAILED: CompleteExitCode.STATE_SAVE_FAILED,
}


def failure_reason_to_exit_code(reason: CompleteFailureReason) -> CompleteExitCode:
    """Map a CompleteFailureReason to its corresponding CompleteExitCode."""
    if (
        not isinstance(reason, CompleteFailureReason)
        or reason not in _FAILURE_REASON_TO_EXIT_CODE
    ):
        raise KeyError(f"Unmapped complete failure reason: {reason!r}")
    return _FAILURE_REASON_TO_EXIT_CODE[reason]


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


@dataclass(frozen=True)
class CompleteResult:
    """Outcome of a complete attempt.

    Boundary invariant: success means advancing through complete stages
    up to GC handoff (`handed_off_to_gc=True`). It strictly excludes
    CompletionReceipt (settlement receipt), which is owned by GC.
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
            if self.stage != CompleteStage.HANDED_OFF_TO_GC:
                raise ValueError(
                    "Successful CompleteResult requires CompleteStage.HANDED_OFF_TO_GC, "
                    f"got: {self.stage!r}"
                )
            if not self.handed_off_to_gc:
                raise ValueError(
                    "Successful CompleteResult requires handed_off_to_gc=True"
                )
            if self.failure is not None:
                raise ValueError(
                    "Successful CompleteResult cannot have a failure object"
                )
        else:
            if self.stage == CompleteStage.HANDED_OFF_TO_GC:
                raise ValueError(
                    "Failed CompleteResult cannot be at CompleteStage.HANDED_OFF_TO_GC"
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
    ) -> CompleteResult:
        """Construct a failed CompleteResult with diagnostic information."""
        if result not in VALID_RESULTS:
            raise ValueError(f"Invalid complete result: {result!r}")
        if stage == CompleteStage.HANDED_OFF_TO_GC:
            raise ValueError(
                "Failed CompleteResult cannot be at CompleteStage.HANDED_OFF_TO_GC"
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
        )
