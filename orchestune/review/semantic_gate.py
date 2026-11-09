"""Layer-2 (integration PR) semantic review contracts (no network or filesystem).

The single-task fast path and the round state machine of the integration
PR review/fix loop. Domain types and v1 markers live in ``semantic_records``
and are re-exported here. Callers verify comment
authors and observe GitHub/CI/jobs; this module only decides from those verified
inputs. Prose is never judged here: review outcomes come from the structured
`orchestune-review-judgments` table, whose structure and coverage `judgment`
validates.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from orchestune.read_dependencies.contracts import ReadValidationV1
from orchestune.read_dependencies.validation import validate_read_validation
from orchestune.review.judgment import (
    judgment_digest,
    parse_judgments,
    validate_coverage,
    validate_judgments,
)
from orchestune.review.semantic_records import (
    ACTIVE_PENDING_PHASES as ACTIVE_PENDING_PHASES,
)
from orchestune.review.semantic_records import (
    IN_FLIGHT_OPERATION_STATUSES as IN_FLIGHT_OPERATION_STATUSES,
)
from orchestune.review.semantic_records import (
    TERMINAL_PASSED_PHASES as TERMINAL_PASSED_PHASES,
)
from orchestune.review.semantic_records import (
    VALID_SEMANTIC_REVIEW_MODES as VALID_SEMANTIC_REVIEW_MODES,
)
from orchestune.review.semantic_records import (
    ChildSourceRef as ChildSourceRef,
)
from orchestune.review.semantic_records import (
    IntegrationAttemptIdentity as IntegrationAttemptIdentity,
)
from orchestune.review.semantic_records import (
    SemanticFixResultV1 as SemanticFixResultV1,
)
from orchestune.review.semantic_records import (
    SemanticFixStatus as SemanticFixStatus,
)
from orchestune.review.semantic_records import (
    SemanticOperationKind as SemanticOperationKind,
)
from orchestune.review.semantic_records import (
    SemanticOperationStatus as SemanticOperationStatus,
)
from orchestune.review.semantic_records import (
    SemanticRetryKind as SemanticRetryKind,
)
from orchestune.review.semantic_records import (
    SemanticReviewHumanOverrideV1 as SemanticReviewHumanOverrideV1,
)
from orchestune.review.semantic_records import (
    SemanticReviewMode as SemanticReviewMode,
)
from orchestune.review.semantic_records import (
    SemanticReviewPhase as SemanticReviewPhase,
)
from orchestune.review.semantic_records import (
    SemanticReviewRetryV1 as SemanticReviewRetryV1,
)
from orchestune.review.semantic_records import (
    SemanticReviewStateV1 as SemanticReviewStateV1,
)
from orchestune.review.semantic_records import (
    _require_int,
)
from orchestune.review.semantic_records import (
    format_semantic_fix_result_marker as format_semantic_fix_result_marker,
)
from orchestune.review.semantic_records import (
    format_semantic_review_override_marker as format_semantic_review_override_marker,
)
from orchestune.review.semantic_records import (
    format_semantic_review_retry_marker as format_semantic_review_retry_marker,
)
from orchestune.review.semantic_records import (
    format_semantic_review_state_marker as format_semantic_review_state_marker,
)
from orchestune.review.semantic_records import (
    normalize_child_sources as normalize_child_sources,
)
from orchestune.review.semantic_records import (
    parse_semantic_fix_result_marker as parse_semantic_fix_result_marker,
)
from orchestune.review.semantic_records import (
    parse_semantic_review_override_marker as parse_semantic_review_override_marker,
)
from orchestune.review.semantic_records import (
    parse_semantic_review_retry_marker as parse_semantic_review_retry_marker,
)
from orchestune.review.semantic_records import (
    parse_semantic_review_state_marker as parse_semantic_review_state_marker,
)
from orchestune.review.semantic_records import (
    restore_latest_fix_result as restore_latest_fix_result,
)
from orchestune.review.semantic_records import (
    restore_latest_human_override as restore_latest_human_override,
)
from orchestune.review.semantic_records import (
    restore_latest_retry_request as restore_latest_retry_request,
)
from orchestune.review.semantic_records import (
    restore_latest_semantic_review_state as restore_latest_semantic_review_state,
)
from orchestune.review.semantic_records import (
    semantic_fix_output_branch as semantic_fix_output_branch,
)


@dataclass(frozen=True)
class FastPathDecision:
    eligible: bool
    reason: str
    phase: SemanticReviewPhase


def evaluate_single_task_fast_path(
    *,
    child_count: int,
    force_integration_review: bool,
    base_sha_matches: bool,
    tree_matches_without_conflict: bool,
    layer1_verdict: str | None,
    layer1_reviewed_sha: str | None,
    source_sha: str,
) -> FastPathDecision:
    """Skip layer-2 review only when the PR is exactly the layer-1-reviewed tree."""
    not_started = SemanticReviewPhase.NOT_STARTED
    if force_integration_review:
        return FastPathDecision(False, "forced_integration_review", not_started)
    if child_count != 1:
        return FastPathDecision(False, "multiple_children", not_started)
    if not base_sha_matches:
        return FastPathDecision(False, "base_sha_mismatch", not_started)
    if not tree_matches_without_conflict:
        return FastPathDecision(False, "tree_or_conflict_diff", not_started)
    if layer1_verdict != "pass" or not source_sha or layer1_reviewed_sha != source_sha:
        return FastPathDecision(False, "layer1_evidence_insufficient", not_started)
    return FastPathDecision(
        True, "single_task_fast_path", SemanticReviewPhase.FAST_PATH_PASSED
    )


#: The only accepted judgment/status pairs; anything else is a broken contract.
ALLOWED_JUDGMENT_STATUSES: Mapping[str, frozenset[str]] = {
    "adopt": frozenset({"unresolved", "resolved"}),
    "decline": frozenset({"declined"}),
    "already_addressed": frozenset({"resolved"}),
    "duplicate": frozenset({"resolved"}),
    "needs_information": frozenset({"deferred"}),
}


@dataclass(frozen=True)
class SemanticJudgmentDecision:
    """Structural verdict on one round's judgment table (never on its prose)."""

    valid: bool
    reason: str
    round: int | None = None
    digest: str | None = None
    rows: int = 0
    unresolved: int = 0
    deferred: int = 0

    @property
    def requires_fix(self) -> bool:
        return self.valid and self.unresolved > 0

    @property
    def passable(self) -> bool:
        """Every finding judged and settled; an empty table proves nothing."""
        return (
            self.valid and self.rows > 0 and self.unresolved == 0 and self.deferred == 0
        )


def evaluate_semantic_judgments(
    judgments: str | Mapping[str, Any] | None,
    review_result: Mapping[str, Any] | None,
    *,
    expected_round: int,
) -> SemanticJudgmentDecision:
    """Validate a table (comment body or parsed mapping) against one round.

    `review_result` is the acquired review of that round; without it coverage
    cannot be checked, so the table is not accepted. Adopt counts never decide:
    only unresolved/deferred statuses of valid pairs do.
    """
    if judgments is None:
        return SemanticJudgmentDecision(False, "judgments_missing")
    if review_result is None:
        return SemanticJudgmentDecision(False, "review_result_missing")
    try:
        table = (
            parse_judgments(judgments)
            if isinstance(judgments, str)
            else validate_judgments(dict(judgments))
        )
        validate_coverage(table, {**review_result, "round": expected_round})
    except ValueError as exc:
        return SemanticJudgmentDecision(False, f"judgments_invalid: {exc}")
    rows = table["findings"]
    if any(
        row["status"] not in ALLOWED_JUDGMENT_STATUSES[row["judgment"]] for row in rows
    ):
        return SemanticJudgmentDecision(False, "judgment_status_pair_invalid")
    digest, _ = judgment_digest(table)
    unresolved = sum(row["status"] == "unresolved" for row in rows)
    deferred = sum(row["status"] == "deferred" for row in rows)
    if deferred:
        reason = "deferred_judgments"
    elif unresolved:
        reason = "unresolved_adopt"
    else:
        reason = "judgments_settled" if rows else "empty_judgment_table"
    return SemanticJudgmentDecision(
        True, reason, expected_round, digest, len(rows), unresolved, deferred
    )


@dataclass(frozen=True)
class SemanticReviewLimits:
    max_rounds: int = 3
    max_fix_retries: int = 2
    review_timeout_seconds: int = 3600
    fix_timeout_seconds: int = 3600
    ci_timeout_seconds: int = 3600
    attempt_timeout_seconds: int = 6 * 3600

    def __post_init__(self) -> None:
        for f in fields(self):
            _require_int(getattr(self, f.name), f.name, minimum=1)


class SemanticCiConclusion(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"


@dataclass(frozen=True)
class SemanticCiObservation:
    """CI of one commit; only a result for the current head is ever accepted."""

    sha: str
    conclusion: SemanticCiConclusion


@dataclass(frozen=True)
class SemanticOperationObservation:
    """Observed job state; ignored unless it names the reserved operation id.

    `output_sha` equal to the job's input means it changed nothing; `input_sha`,
    when known, must be the head the job was reserved for.
    """

    operation_id: str
    status: SemanticOperationStatus
    output_sha: str | None = None
    input_sha: str | None = None

    @classmethod
    def from_fix_result(
        cls, result: SemanticFixResultV1
    ) -> SemanticOperationObservation:
        if result.status == "failed":
            return cls(
                result.operation_id,
                SemanticOperationStatus.FAILED,
                input_sha=result.input_sha,
            )
        return cls(
            result.operation_id,
            SemanticOperationStatus.COMPLETED,
            result.output_sha or result.input_sha,
            result.input_sha,
        )


@dataclass(frozen=True)
class SemanticTransitionObservation:
    """Verified inputs of one evaluation (authors/bindings checked by the caller)."""

    identity: IntegrationAttemptIdentity
    observed_head_sha: str
    now: datetime
    mode: SemanticReviewMode = SemanticReviewMode.REQUIRED
    limits: SemanticReviewLimits = SemanticReviewLimits()
    override: SemanticReviewHumanOverrideV1 | None = None
    retry_request: SemanticReviewRetryV1 | None = None
    review_complete: bool = False
    reviewed_sha: str | None = None
    judgments: SemanticJudgmentDecision | None = None
    ci: SemanticCiObservation | None = None
    operation: SemanticOperationObservation | None = None
    #: Enabled by the runner only for stale_read_gate=required.
    read_validation_required: bool = False
    read_report_digest: str | None = None
    read_candidate_ids: tuple[str, ...] = ()
    read_validation: ReadValidationV1 | None = None


@dataclass(frozen=True)
class SemanticTransitionDecision:
    next_phase: SemanticReviewPhase
    next_round: int
    next_fix_retries: int
    reason: str
    next_state: SemanticReviewStateV1
    requires_fix_dispatch: bool = False
    #: The completed job's output awaits promotion to the PR head (state unchanged).
    requires_fix_promotion: bool = False
    requires_review_trigger: bool = False
    requires_escalation: bool = False


_P = SemanticReviewPhase
_EVIDENCE_CLEARED: Mapping[str, Any] = {
    "reviewed_sha": None,
    "review_source_digest": None,
    "judgment_digest": None,
    "ci_verified_sha": None,
    "reviewed_read_report_digest": None,
}
_FRESH_REVIEW: Mapping[str, Any] = {
    **_EVIDENCE_CLEARED,
    "operation_id": None,
    "operation_kind": None,
    "operation_status": None,
    "provider_handle": None,
    "fix_input_sha": None,
    "fix_output_sha": None,
    "fix_completed": False,
}
_PHASE_TIMEOUTS: Mapping[SemanticReviewPhase, str] = {
    _P.REVIEW_PENDING: "review_timeout_seconds",
    _P.FIX_IN_PROGRESS: "fix_timeout_seconds",
    _P.CI_VERIFICATION: "ci_timeout_seconds",
}
_RETRYABLE_PHASES = frozenset(
    {_P.NOT_STARTED, _P.REVIEW_PENDING, _P.FIX_IN_PROGRESS, _P.CI_VERIFICATION}
)


def semantic_operation_id(
    attempt_id: str,
    round_num: int,
    kind: SemanticOperationKind,
    retry: int,
    restart: int = 0,
) -> str:
    """Deterministic, so re-evaluating one step reserves the same operation.

    The retry/restart generation keeps a late result of a job reserved before
    an explicit retry/restart from being taken for the job reserved after it.
    """
    return f"{attempt_id[:12]}-g{restart}-r{round_num}-{kind.value}-{retry}"


@dataclass(frozen=True)
class _Step:
    state: SemanticReviewStateV1
    obs: SemanticTransitionObservation

    @property
    def now(self) -> str:
        return self.obs.now.isoformat().replace("+00:00", "Z")

    def hold(
        self, reason: str, *, promotion: bool = False
    ) -> SemanticTransitionDecision:
        """No state change (start times are never renewed); only promotion asked."""
        s = self.state
        return SemanticTransitionDecision(
            s.phase, s.round, s.fix_retries, reason, s, requires_fix_promotion=promotion
        )

    def move(
        self, phase: SemanticReviewPhase, reason: str, **changes: Any
    ) -> SemanticTransitionDecision:
        s, now = self.state, self.now
        changes.setdefault("round", s.round)
        changes.setdefault("fix_retries", s.fix_retries)
        if phase != s.phase:
            changes["phase_started_at"] = now
        if changes["round"] != s.round:
            changes["round_started_at"] = now
        if phase == _P.ESCALATED:
            changes["escalation_reason"] = reason
            changes["escalated_from"] = s.phase
        nxt = replace(
            s,
            phase=phase,
            state_revision=s.state_revision + 1,
            updated_at=now,
            **changes,
        )
        return SemanticTransitionDecision(
            phase,
            nxt.round,
            nxt.fix_retries,
            reason,
            nxt,
            requires_fix_dispatch=nxt.operation_status
            == SemanticOperationStatus.RESERVED
            and nxt.operation_id != s.operation_id,
            requires_review_trigger=phase == _P.REVIEW_PENDING,
            requires_escalation=phase == _P.ESCALATED,
        )

    def escalate(self, reason: str) -> SemanticTransitionDecision:
        return self.move(_P.ESCALATED, reason)

    def reserve(
        self, reason: str, *, retry: int, **changes: Any
    ) -> SemanticTransitionDecision:
        """Reserve the round's judgment/fix job for the current head."""
        s = self.state
        kind = SemanticOperationKind.FIX
        restart = changes.get("restart_count", s.restart_count)
        return self.move(
            _P.FIX_IN_PROGRESS,
            reason,
            fix_retries=retry,
            operation_id=semantic_operation_id(
                s.attempt_id, s.round, kind, retry, restart
            ),
            operation_kind=kind,
            operation_status=SemanticOperationStatus.RESERVED,
            provider_handle=None,
            fix_input_sha=s.head_sha,
            fix_output_sha=None,
            fix_completed=False,
            **changes,
        )

    def retry(self, reason: str, limit_reason: str) -> SemanticTransitionDecision:
        """One more bounded job, or escalation once retries are exhausted."""
        if self.state.fix_retries >= self.obs.limits.max_fix_retries:
            return self.escalate(limit_reason)
        return self.reserve(reason, retry=self.state.fix_retries + 1)


def evaluate_semantic_review_transition(
    state: SemanticReviewStateV1, observation: SemanticTransitionObservation
) -> SemanticTransitionDecision:
    """Next phase, round, retry count, state and side effects of one evaluation.

    Pure: the caller performs the requested dispatch/promotion/trigger/escalation
    and posts `next_state` only when it differs from `state`.
    """
    if observation.now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    step = _Step(state, observation)
    if state.phase == _P.INVALIDATED:
        return step.hold("invalidated")
    changed = _input_change(step)
    if changed is not None:
        return step.move(_P.INVALIDATED, changed)
    read_change = _read_report_change(step)
    if read_change is not None:
        return read_change
    if state.phase in TERMINAL_PASSED_PHASES:
        return step.hold("terminal_passed")
    override = observation.override
    if (
        override is not None
        and not observation.read_validation_required
        and override.attempt_id == state.attempt_id
        and override.head_sha == observation.observed_head_sha
    ):
        return step.move(_P.SKIPPED_BY_HUMAN, "human_override")
    if state.phase == _P.ESCALATED:
        return _on_escalated(step)
    if observation.mode == SemanticReviewMode.OFF:
        return step.move(_P.SKIPPED_BY_HUMAN, "mode_off")
    expired = _expired(step)
    if expired is not None:
        return step.escalate(expired)
    return _PHASE_HANDLERS[state.phase](step)


def _input_change(step: _Step) -> str | None:
    """Base/child-set/head changes the attempt itself did not produce."""
    s, obs = step.state, step.obs
    if (
        not obs.identity.matches_state(s)
        or obs.identity.child_sources != s.child_sources
    ):
        return "attempt_inputs_changed"
    allowed = {s.head_sha}
    output = _completed_output(step)
    if output is not None:
        allowed.add(output)
    return None if obs.observed_head_sha in allowed else "head_changed"


def _read_report_change(step: _Step) -> SemanticTransitionDecision | None:
    """Fail closed on report replacement; reuse explicit restart generations."""
    s, obs = step.state, step.obs
    if not obs.read_validation_required:
        return None
    if obs.mode != SemanticReviewMode.REQUIRED:
        return step.escalate("read_validation_requires_semantic_review")
    digest = obs.read_report_digest
    if digest is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        if s.phase == _P.ESCALATED:
            return step.hold("read_report_missing")
        return step.escalate("read_report_missing")
    promoted = (
        _completed_output(step) == obs.observed_head_sha
        and obs.observed_head_sha != s.head_sha
    )
    if s.phase != _P.NOT_STARTED and s.read_report_digest != digest and not promoted:
        return step.move(
            _P.ESCALATED,
            "read_report_changed",
            read_report_digest=digest,
            **_EVIDENCE_CLEARED,
        )
    if s.phase in TERMINAL_PASSED_PHASES:
        error = _read_validation_error(step)
        if error is None and s.reviewed_read_report_digest != digest:
            error = "read_validation_invalid: reviewed report mismatch"
        if error is not None:
            return step.escalate(error)
    return None


def _read_validation_error(step: _Step) -> str | None:
    if not step.obs.read_validation_required:
        return None
    validation, digest = step.obs.read_validation, step.obs.read_report_digest
    if validation is None or digest is None:
        return "read_validation_invalid: missing validation/report"
    try:
        validate_read_validation(
            validation,
            round_number=step.state.round,
            head_sha=step.state.head_sha,
            report_digest=digest,
            candidate_ids=step.obs.read_candidate_ids,
        )
    except ValueError as error:
        return f"read_validation_invalid: {error}"
    return None


def _current_operation(step: _Step) -> SemanticOperationObservation | None:
    op, s = step.obs.operation, step.state
    if op is None or s.operation_id is None or op.operation_id != s.operation_id:
        return None
    return op


def _completed_output(step: _Step) -> str | None:
    if step.state.phase != _P.FIX_IN_PROGRESS:
        return None
    op = _current_operation(step)
    if op is None or op.status != SemanticOperationStatus.COMPLETED:
        return None
    return op.output_sha


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _expired(step: _Step) -> str | None:
    """Timeouts from recorded start times; missing/unparsable ones fail closed."""
    s, obs = step.state, step.obs
    if s.phase == _P.NOT_STARTED:
        started = _parse_time(s.attempt_started_at)
        if s.attempt_started_at is not None and started is None:
            return "attempt_started_at_invalid"
        phase_limit = None
    else:
        started = _parse_time(s.attempt_started_at)
        if started is None:
            return "attempt_started_at_invalid"
        phase_started = _parse_time(s.phase_started_at)
        if phase_started is None:
            return "phase_started_at_invalid"
        phase_limit = getattr(obs.limits, _PHASE_TIMEOUTS[s.phase])
        if (obs.now - phase_started).total_seconds() >= phase_limit:
            return f"{s.phase.value}_timeout"
    if started is not None and (
        (obs.now - started).total_seconds() >= obs.limits.attempt_timeout_seconds
    ):
        return "attempt_timeout"
    return None


def _on_escalated(step: _Step) -> SemanticTransitionDecision:
    """Apply one unconsumed retry/restart request for this attempt and head.

    Neither renews the consumed rounds, retries or attempt start unless the
    request explicitly resets the limits; a new generation keeps late results
    of earlier jobs from being taken for the jobs reserved afterwards.
    """
    s, request = step.state, step.obs.retry_request
    if (
        request is None
        or request.attempt_id != s.attempt_id
        or request.head_sha != s.head_sha
        or request.request_id == s.last_retry_request_id
    ):
        return step.hold("escalated")
    resumed: dict[str, Any] = {
        "restart_count": s.restart_count + 1,
        "last_retry_request_id": request.request_id,
        "escalation_reason": None,
        "escalated_from": None,
        "attempt_started_at": s.attempt_started_at or step.now,
    }
    if request.reset_limits:
        resumed |= {"attempt_started_at": step.now}
        return step.move(
            _P.REVIEW_PENDING,
            "limits_reset",
            round=1,
            fix_retries=0,
            limits_reset_reason=request.reason,
            **_FRESH_REVIEW,
            **resumed,
        )
    if request.kind == "restart":
        if s.round >= step.obs.limits.max_rounds:
            return step.hold("restart_max_rounds")
        return step.move(
            _P.REVIEW_PENDING,
            "restart_requested",
            round=s.round + 1,
            **_FRESH_REVIEW,
            **resumed,
        )
    phase = s.escalated_from
    if phase is None or phase not in _RETRYABLE_PHASES:
        return step.hold("retry_phase_unknown")
    if phase == _P.FIX_IN_PROGRESS:
        return step.reserve("retry_requested", retry=s.fix_retries, **resumed)
    if phase == _P.NOT_STARTED:
        resumed["attempt_started_at"] = s.attempt_started_at
    return step.move(phase, "retry_requested", **resumed)


def _on_not_started(step: _Step) -> SemanticTransitionDecision:
    return step.move(
        _P.REVIEW_PENDING,
        "review_requested",
        round=1,
        attempt_started_at=step.state.attempt_started_at or step.now,
        read_report_digest=step.obs.read_report_digest,
    )


def _review_evidence(step: _Step) -> bool:
    obs = step.obs
    return obs.review_complete and obs.reviewed_sha == step.state.head_sha


def _on_review_pending(step: _Step) -> SemanticTransitionDecision:
    if not _review_evidence(step):
        return step.hold("review_pending")
    return step.reserve("review_completed", retry=step.state.fix_retries)


def _on_fix(step: _Step) -> SemanticTransitionDecision:
    """Wait for the reserved job; CI of the pre-job head never completes it."""
    s = step.state
    op = _current_operation(step)
    if s.operation_kind is None or s.operation_status is None:
        return step.escalate("operation_missing")
    if op is None or op.status in IN_FLIGHT_OPERATION_STATUSES:
        return step.hold("operation_in_flight")
    if op.status == SemanticOperationStatus.INDETERMINATE:
        return step.escalate("operation_indeterminate")
    if op.status == SemanticOperationStatus.FAILED:
        return step.retry("operation_failed", "fix_retry_limit")
    if op.input_sha is not None and op.input_sha != s.fix_input_sha:
        return step.escalate("operation_input_mismatch")
    if op.output_sha is None:
        return step.escalate("operation_output_unknown")
    if op.output_sha == s.head_sha:
        if not _review_evidence(step):
            return step.hold("review_evidence_missing")
        return _route_judgments(step)
    if step.obs.observed_head_sha != op.output_sha:
        return step.hold("awaiting_fix_promotion", promotion=True)
    return step.move(
        _P.CI_VERIFICATION,
        "head_changed_by_operation",
        head_sha=op.output_sha,
        operation_status=SemanticOperationStatus.COMPLETED,
        fix_output_sha=op.output_sha,
        fix_completed=True,
        read_report_digest=step.obs.read_report_digest,
        **_EVIDENCE_CLEARED,
    )


def _route_judgments(step: _Step) -> SemanticTransitionDecision:
    """Route the table the unchanged-head job posted for this round."""
    s, verdict = step.state, step.obs.judgments
    if verdict is None:
        return step.escalate("judgments_missing")
    if not verdict.valid:
        return step.escalate(verdict.reason)
    if verdict.round != s.round:
        return step.escalate("judgments_round_mismatch")
    if verdict.deferred:
        return step.escalate("deferred_judgments")
    if verdict.unresolved:
        return step.retry("unresolved_after_fix", "fix_retry_limit")
    if not verdict.passable:
        return step.escalate(verdict.reason)
    error = _read_validation_error(step)
    if error is not None:
        return step.escalate(error)
    evidence = {
        "reviewed_sha": s.head_sha,
        "judgment_digest": verdict.digest,
        "operation_status": SemanticOperationStatus.COMPLETED,
        "reviewed_read_report_digest": (
            step.obs.read_report_digest if step.obs.read_validation_required else None
        ),
    }
    if s.ci_verified_sha == s.head_sha:
        return step.move(_P.PASSED, "passed", **evidence)
    return step.move(_P.CI_VERIFICATION, "review_passed", **evidence)


def _on_ci_verification(step: _Step) -> SemanticTransitionDecision:
    s, obs = step.state, step.obs
    ci = obs.ci
    if ci is None or ci.sha != s.head_sha or ci.conclusion == "pending":
        return step.hold("ci_pending")
    if ci.conclusion == SemanticCiConclusion.FAILURE:
        return step.retry("ci_failed", "ci_retry_limit")
    if s.reviewed_sha is None:
        if s.round >= obs.limits.max_rounds:
            return step.escalate("max_rounds")
        return step.move(
            _P.REVIEW_PENDING,
            "re_review_changed_head",
            round=s.round + 1,
            ci_verified_sha=s.head_sha,
        )
    verdict = obs.judgments
    error = _read_validation_error(step)
    if error is not None:
        return step.escalate(error)
    if (
        obs.read_validation_required
        and s.reviewed_read_report_digest != obs.read_report_digest
    ):
        return step.escalate("read_validation_invalid: reviewed report mismatch")
    if (
        s.reviewed_sha == s.head_sha
        and _review_evidence(step)
        and verdict is not None
        and verdict.passable
        and verdict.round == s.round
        and verdict.digest == s.judgment_digest
    ):
        return step.move(_P.PASSED, "passed", ci_verified_sha=s.head_sha)
    return step.escalate("pass_evidence_invalid")


_PHASE_HANDLERS = {
    _P.NOT_STARTED: _on_not_started,
    _P.REVIEW_PENDING: _on_review_pending,
    _P.FIX_IN_PROGRESS: _on_fix,
    _P.CI_VERIFICATION: _on_ci_verification,
}
