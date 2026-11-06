"""Layer-2 integration-PR review/fix loop advanced from the Dispatcher post-cycle (#1362).

The pure round state machine lives in :mod:`orchestune.review.semantic_gate`;
the #1361 adapters trigger reviews, launch the judgment/fix job and escalate;
the #1370 claim makes one worker the only writer of a parent's attempts. This
runner observes each pending attempt, evaluates one transition and performs its
side effects under the claim. It never moves the parent branch: it stops at a
saved ``PASSED`` / ``SKIPPED_BY_HUMAN`` that the Integrator finalizes in the
same post-cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.semantic_review.ack import AckAction, AckAssessment
from orchestune.dispatch.semantic_review.adapters import (
    SemanticReviewPreflightResult,
)
from orchestune.dispatch.semantic_review.ci import (
    RemoteHeadFn,
    remote_branch_sha,
    verify_semantic_ci,
)
from orchestune.dispatch.semantic_review.diagnostics import (
    ReviewDiagnostics,
    build_review_diagnostics,
    describe_reviewer,
)
from orchestune.dispatch.semantic_review.effects import (
    SemanticReviewEffects,
    SemanticReviewOperations,
)
from orchestune.dispatch.semantic_review.evidence import (
    SemanticReviewEvidence as SemanticReviewEvidence,
)
from orchestune.dispatch.semantic_review.evidence import (
    _Attempt,
    _Review,
    _Stop,
)
from orchestune.dispatch.semantic_review.lease import (
    CLAIM_TTL_SECONDS,
    SemanticClaimClient,
    SemanticReviewLease,
)
from orchestune.dispatch.semantic_review.postcycle_support import (
    default_worker_id,
    run_cycle_cleanup,
    semantic_ci_verifier,
)
from orchestune.dispatch.semantic_review.triggers import (
    parse_iso,
    round_triggers,
)
from orchestune.forge import Forge, ForgeAuthError
from orchestune.integrator.attempt import (
    OpenAttempt,
    find_open_attempts,
)
from orchestune.integrator.attempt_cleanup import RemoteRefPorts
from orchestune.integrator.semantic_claim import (
    DETAIL_FIX_NOT_DESCENDANT,
    PushStatus,
)
from orchestune.labels import StatusLabel
from orchestune.review.acquisition import collect_review_state, has_reviewer_activity
from orchestune.review.markers import derive_review_target
from orchestune.review.rounds import (
    precise_created_at,
    trigger_comment_ids,
)
from orchestune.review.semantic_gate import (
    ACTIVE_PENDING_PHASES,
    IN_FLIGHT_OPERATION_STATUSES,
    IntegrationAttemptIdentity,
    SemanticCiObservation,
    SemanticOperationObservation,
    SemanticOperationStatus,
    SemanticReviewMode,
    SemanticReviewPhase,
    SemanticReviewStateV1,
    SemanticTransitionDecision,
    SemanticTransitionObservation,
    evaluate_semantic_review_transition,
    semantic_fix_output_branch,
)

_P = SemanticReviewPhase
_TARGET_PHASES = ACTIVE_PENDING_PHASES | {_P.ESCALATED}
_REVIEW_PHASES = frozenset({_P.REVIEW_PENDING, _P.FIX_IN_PROGRESS, _P.CI_VERIFICATION})
_UNSETTLED_OPERATIONS = IN_FLIGHT_OPERATION_STATUSES | {
    SemanticOperationStatus.INDETERMINATE
}
#: A reviewer that showed no activity after its request (#1436).
_ACK_REASON = "review_not_acknowledged"
_SEMANTIC_MARKERS = (
    "orchestune:semantic-review-",
    "orchestune:semantic-fix-result",
    "```orchestune-review-judgments",
)
#: Report statuses of a cycle that went as designed (another worker holding
#: the claim is progress, not a failure).
SUCCESS_STATUSES = frozenset(
    {
        "no_pending_attempt",
        "not_pending",
        "held",
        "planned",
        "advanced",
        "claimed_by_other",
    }
)
#: Indeterminate outcomes a later cycle or a human settles; any other status
#: (`error`, a failed promotion, ...) is a definite failure to retry.
WARNING_STATUSES = frozenset(
    {
        "state_unreadable",
        "state_changed",
        "inputs_unknown",
        "claim_unknown",
        "claim_lost",
        "trigger_indeterminate",
        "ci_discarded",
    }
)

VerifyCiFn = Callable[[str, str], SemanticCiObservation | None]


@dataclass(frozen=True)
class SemanticReviewCycleReport:
    status: str
    attempt_id: str | None = None
    pr_number: int | None = None
    previous_phase: str | None = None
    next_phase: str | None = None
    round: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticReviewPorts:
    """Layer-2 semantic review runner port dependencies (#1666)."""

    forge: Forge
    verify_ci_fn: VerifyCiFn
    operations: SemanticReviewOperations | None = None
    claims: SemanticClaimClient | None = None
    remote_head_fn: RemoteHeadFn | None = None
    clock: Callable[[], datetime] | None = None


_PORT_FIELDS = frozenset(
    ("forge", "verify_ci_fn", "operations", "claims", "remote_head_fn", "clock")
)


def _resolve_semantic_review_ports(
    ports: SemanticReviewPorts | None, kwargs: dict[str, Any]
) -> SemanticReviewPorts:
    overrides = {k: kwargs.pop(k) for k in _PORT_FIELDS if k in kwargs}
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(
            f"SemanticReviewCycleRunner.__init__() got unexpected keyword argument(s): {unexpected}"
        )
    if ports is None:
        return SemanticReviewPorts(**overrides)
    return replace(ports, **overrides) if overrides else ports


def _is_target(attempt: OpenAttempt) -> bool:
    if attempt.state_error is not None:
        return True
    return attempt.state is not None and attempt.state.phase in _TARGET_PHASES


def _has_semantic_marker(body: object) -> bool:
    return isinstance(body, str) and any(m in body for m in _SEMANTIC_MARKERS)


def _hold(state: SemanticReviewStateV1, reason: str) -> SemanticTransitionDecision:
    return SemanticTransitionDecision(
        state.phase, state.round, state.fix_retries, reason, state
    )


def _escalated(
    state: SemanticReviewStateV1, reason: str, now: str, **changes: Any
) -> SemanticTransitionDecision:
    """A runner-made escalation (Preflight, invalidation, promotion) in the gate's shape."""
    if state.phase != _P.ESCALATED:
        changes |= {"escalated_from": state.phase, "phase_started_at": now}
    nxt = replace(
        state,
        phase=_P.ESCALATED,
        escalation_reason=reason,
        state_revision=state.state_revision + 1,
        updated_at=now,
        **changes,
    )
    return SemanticTransitionDecision(
        _P.ESCALATED, nxt.round, nxt.fix_retries, reason, nxt, requires_escalation=True
    )


class SemanticReviewCycleRunner:
    """Advance each pending integration attempt by at most one phase per cycle."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        ports: SemanticReviewPorts | None = None,
        repository_root: Path,
        now: datetime,
        worker_id: str,
        **kwargs: Any,
    ) -> None:
        """``now`` fixes what this cycle observes and decides; claims are
        acquired, renewed and released at the ``clock`` time of each write,
        so a long CI run cannot leave a lease that expired before it began."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        resolved_ports = _resolve_semantic_review_ports(ports, kwargs)
        self._config = config
        self._forge = resolved_ports.forge
        self._evidence = SemanticReviewEvidence(resolved_ports.forge)
        self._root = repository_root
        self._now = now.astimezone(UTC)
        self._worker = worker_id
        self._clock = resolved_ports.clock or (lambda: datetime.now(UTC))
        self._verify_ci = resolved_ports.verify_ci_fn
        self._ops = resolved_ports.operations or SemanticReviewOperations()
        self._claims = resolved_ports.claims or SemanticClaimClient()
        self._remote_head = resolved_ports.remote_head_fn or (
            lambda name: remote_branch_sha(repository_root, name)
        )
        self._mode = SemanticReviewMode(config.semantic_review_mode)
        self._lease = SemanticReviewLease(
            forge=self._forge,
            repository_root=self._root,
            claims=self._claims,
            worker_id=self._worker,
            clock=self._clock,
        )
        self._effects = SemanticReviewEffects(
            forge=self._forge,
            repository_root=self._root,
            config=self._config,
            operations=self._ops,
            evidence=self._evidence,
            lease=self._lease,
            reviewer=self._reviewer,
            stamp=lambda: self._stamp,
        )

    @property
    def _stamp(self) -> str:
        return self._now.isoformat().replace("+00:00", "Z")

    def run(self) -> list[SemanticReviewCycleReport]:
        attempts = [
            attempt
            for attempt in find_open_attempts(
                self._forge, self._config.parent_issue_number
            )
            if _is_target(attempt)
        ]
        if not attempts:
            return [SemanticReviewCycleReport("no_pending_attempt")]
        return [self._advance_attempt(attempt) for attempt in attempts]

    def inspect(
        self, pr: OpenAttempt
    ) -> tuple[SemanticReviewStateV1, ReviewDiagnostics]:
        """Read-only state and diagnostics of one attempt (`review status`)."""
        ctx = self._evidence.restore(pr)
        return ctx.state, self._diagnose(ctx, ctx.state, self._acquire_review(ctx))[0]

    def _diagnose(
        self, ctx: _Attempt, state: SemanticReviewStateV1, review: _Review
    ) -> tuple[ReviewDiagnostics, AckAssessment]:
        return build_review_diagnostics(
            self._config,
            state,
            pr_number=ctx.pr.pr_number,
            requests=review.requests,
            acknowledged=review.acknowledged,
            now=self._now,
        )

    # -- restoration and observation ---------------------------------------

    def _advance_attempt(self, pr: OpenAttempt) -> SemanticReviewCycleReport:
        """One attempt's failure is reported and never stops the others."""
        ctx: _Attempt | None = None
        try:
            ctx = self._evidence.restore(pr)
            if ctx.state.phase not in _TARGET_PHASES:
                return self._report(ctx, "not_pending", ctx.state)
            return self._carry_out(ctx, self._decide(ctx))
        except ForgeAuthError:
            raise
        except Exception as error:  # noqa: BLE001 - reported per attempt
            status = error.status if isinstance(error, _Stop) else "error"
            details = (
                error.details if isinstance(error, _Stop) else {"error": str(error)}
            )
            if ctx is None:
                return SemanticReviewCycleReport(
                    status, pr_number=pr.pr_number, details=details
                )
            return self._report(ctx, status, ctx.state, **details)

    def _current_identity(
        self, state: SemanticReviewStateV1
    ) -> IntegrationAttemptIdentity:
        """Identity of the current base and frozen children; dropped ones fall out."""
        try:
            base = self._remote_head(f"parent/issue-{state.parent_issue_number}")
            children = []
            for child in state.child_sources:
                labels = self._forge.get_issue_labels(child.issue_number)
                if StatusLabel.DONE.value not in labels:
                    continue
                branch = build_task_branch_name(child.issue_number, child.subtask_id)
                sha = self._remote_head(branch)
                children.append(replace(child, source_sha=sha) if sha else child)
        except Exception as error:  # an unreadable input is never "unchanged"
            raise _Stop("inputs_unknown", error=str(error)) from error
        if base is None:
            raise _Stop("inputs_unknown", error="parent branch is missing")
        return IntegrationAttemptIdentity.create(
            state.parent_issue_number, base, children
        )

    def _observe(self, ctx: _Attempt) -> SemanticTransitionObservation:
        state = ctx.state
        if state.phase in _REVIEW_PHASES and state.round > 0:
            ctx.review = self._acquire_review(ctx)
        operation = self._poll(ctx) if self._needs_poll(state) else None
        judgments = None
        if state.operation_id and state.phase in (
            _P.FIX_IN_PROGRESS,
            _P.CI_VERIFICATION,
        ):
            judgments = self._evidence.judgments(ctx)
        return SemanticTransitionObservation(
            identity=self._current_identity(state),
            observed_head_sha=ctx.pr.head_sha,
            now=self._now,
            mode=self._mode,
            limits=self._config.semantic_review_limits,
            override=ctx.override,
            retry_request=self._evidence.retry_request(ctx)
            if state.phase == _P.ESCALATED
            else None,
            review_complete=ctx.review.complete,
            reviewed_sha=state.head_sha if ctx.review.complete else None,
            judgments=judgments,
            operation=operation,
        )

    @staticmethod
    def _needs_poll(state: SemanticReviewStateV1) -> bool:
        if state.operation_id is None:
            return False
        return state.phase == _P.FIX_IN_PROGRESS or (
            state.phase == _P.ESCALATED
            and state.operation_status in _UNSETTLED_OPERATIONS
        )

    def _acquire_review(self, ctx: _Attempt) -> _Review:
        """The current round's review of the requested head, by its reviewer only."""
        state, comments = ctx.state, ctx.comments
        triggers = self._evidence.trusted_triggers(ctx)
        requested = round_triggers(triggers, state, fresh=True)
        if not requested:
            return _Review()
        trigger = requested[-1]
        requests = tuple(
            (str(t.id), parse_iso(precise_created_at(comments, t)) or self._now)
            for t in requested
        )
        later = [t for t in triggers if t.created_at > trigger.created_at]
        started = precise_created_at(comments, trigger)
        ended = precise_created_at(comments, later[0]) if later else ""
        excluded: set[int | str] = trigger_comment_ids(comments) | {
            c["id"] for c in comments if _has_semantic_marker(c.get("body"))
        }
        forge = cast(Any, self._forge)  # review listings are GitHubForge-only
        snapshot = {
            "issue_comments": comments,
            "reviews": forge.list_pull_request_reviews(ctx.pr.pr_number),
            "inline_comments": forge.list_pull_request_review_comments(
                ctx.pr.pr_number
            ),
        }
        window: dict[str, Any] = {
            "exclude_issue_comment_ids": excluded,
            "round_started_at": trigger.created_at,
            "round_ended_at": later[0].created_at if later else "",
            "requested_head_sha": state.head_sha,
            "activity_started_at": started,
            "activity_ended_at": ended,
        }
        result = collect_review_state(snapshot, trigger.reviewer, **window)
        acknowledged = result[
            "acquisition_status"
        ] != "unavailable" or has_reviewer_activity(
            snapshot, trigger.reviewer, **window
        )
        if result["acquisition_status"] != "acquired":
            return _Review(acknowledged=acknowledged, requests=requests)
        target, _ = derive_review_target(
            result["review_items"], state.head_sha, state.head_sha
        )
        result = {**result, "round": state.round}
        return _Review(target == state.head_sha, True, result, requests)

    def _poll(self, ctx: _Attempt) -> SemanticOperationObservation:
        state = ctx.state
        assert state.operation_id is not None
        result = self._ops.poll(
            self._forge,
            self._root,
            pr_number=ctx.pr.pr_number,
            attempt_id=state.attempt_id,
            operation_id=state.operation_id,
        )
        output = result.output_sha
        if result.status == SemanticOperationStatus.COMPLETED and output is None:
            output = result.input_sha  # a judgment-only result changed nothing
        if result.status in (
            SemanticOperationStatus.COMPLETED,
            SemanticOperationStatus.FAILED,
        ):
            self._effects.cleanup_fix_worktree(
                state.operation_id, state.provider_handle
            )
        return SemanticOperationObservation(
            state.operation_id, result.status, output, result.input_sha
        )

    # -- decision -----------------------------------------------------------

    def _decide(self, ctx: _Attempt) -> SemanticTransitionDecision:
        state = ctx.state
        obs = ctx.obs = self._observe(ctx)
        decision = evaluate_semantic_review_transition(state, obs)
        if decision.next_phase == _P.INVALIDATED:
            return self._guard_invalidation(state, obs, decision)
        if self._config.apply and self._needs_preflight(state, decision):
            preflight = self._ops.preflight(self._config, repository_root=self._root)
            if not preflight.ok:
                return self._preflight_failed(state, preflight)
        if decision.reason == "review_pending_timeout" and not ctx.review.acknowledged:
            nxt = replace(decision.next_state, escalation_reason=_ACK_REASON)
            return replace(decision, reason=_ACK_REASON, next_state=nxt)
        return self._ack_decision(ctx, decision)

    def _ack_decision(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticTransitionDecision:
        """Stop an unanswered request at the ack deadline instead of the (much
        later) review timeout; the opt-in retry is carried out by `_carry_out`."""
        if ctx.state.phase != _P.REVIEW_PENDING or decision.reason != "review_pending":
            return decision
        _, ctx.ack = self._diagnose(ctx, ctx.state, ctx.review)
        if ctx.ack.action is AckAction.ESCALATE:
            return _escalated(ctx.state, _ACK_REASON, self._stamp)
        return decision

    def _guard_invalidation(
        self,
        state: SemanticReviewStateV1,
        obs: SemanticTransitionObservation,
        decision: SemanticTransitionDecision,
    ) -> SemanticTransitionDecision:
        """Invalidate only once the old job is known to have stopped writing."""
        op = obs.operation
        unsettled = (
            state.operation_id is not None
            and state.operation_status in _UNSETTLED_OPERATIONS
            and (op is None or op.status in _UNSETTLED_OPERATIONS)
        )
        if not unsettled:
            return decision
        reason = "invalidation_unconfirmed"
        if state.phase == _P.ESCALATED and state.escalation_reason == reason:
            return _hold(state, reason)
        return _escalated(state, reason, self._stamp)

    def _needs_preflight(
        self, state: SemanticReviewStateV1, decision: SemanticTransitionDecision
    ) -> bool:
        """New (NOT_STARTED) or resumed (ESCALATED) work; overrides come first."""
        if decision.next_phase in (_P.SKIPPED_BY_HUMAN, _P.ESCALATED):
            return False
        if self._mode == SemanticReviewMode.OFF:
            return False
        return state.phase in (_P.NOT_STARTED, _P.ESCALATED)

    def _preflight_failed(
        self, state: SemanticReviewStateV1, result: SemanticReviewPreflightResult
    ) -> SemanticTransitionDecision:
        reason = result.error_code or "preflight_failed"
        if (
            state.phase == _P.ESCALATED
            and state.preflight_notice_key == result.notice_key
        ):
            return _hold(state, reason)
        return _escalated(
            state, reason, self._stamp, preflight_notice_key=result.notice_key
        )

    # -- side effects -------------------------------------------------------

    def _needs_work(self, ctx: _Attempt, decision: SemanticTransitionDecision) -> bool:
        if decision.next_state != ctx.state or decision.requires_fix_promotion:
            return True
        if decision.reason == "ci_pending" and ctx.state.phase == _P.CI_VERIFICATION:
            return True
        return (
            ctx.state.phase == _P.ESCALATED
            and not self._effects.escalation_recorded(ctx.state)
        )

    def _carry_out(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticReviewCycleReport:
        if ctx.ack is not None and ctx.ack.action is AckAction.RETRY:
            return self._repeat_request(ctx)
        if not self._needs_work(ctx, decision):
            return self._report(
                ctx, "held", decision.next_state, reason=decision.reason
            )
        if not self._config.apply:
            return self._report(
                ctx, "planned", decision.next_state, reason=decision.reason
            )
        self._lease.acquire(ctx)
        try:
            self._lease.require_unchanged(ctx)
            decision = self._claimed_observations(ctx, decision)
            final = self._apply(ctx, decision)
        finally:
            self._lease.release(ctx)
        return self._report(ctx, "advanced", final, reason=decision.reason)

    def _repeat_request(self, ctx: _Attempt) -> SemanticReviewCycleReport:
        """Post the one extra request of an unanswered round (state unchanged)."""
        reason = "review_request_retried"
        if not self._config.apply:
            return self._report(ctx, "planned", ctx.state, reason=reason)
        self._lease.acquire(ctx)
        try:
            self._lease.require_unchanged(ctx)
            self._lease.revalidate(ctx)
            ctx.comments = self._forge.list_all_issue_comments(ctx.pr.pr_number)
            ctx.review = self._acquire_review(ctx)  # a late answer cancels the retry
            if self._diagnose(ctx, ctx.state, ctx.review)[1].action != AckAction.RETRY:
                return self._report(ctx, "held", ctx.state, reason="retry_not_due")
            self._forge.create_issue_comment(
                ctx.pr.pr_number, self._effects.trigger_body(ctx, ctx.state)
            )
        finally:
            self._lease.release(ctx)
        return self._report(ctx, "advanced", ctx.state, reason=reason)

    def _claimed_observations(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticTransitionDecision:
        """Promotion and CI run only under the claim; each may yield a new decision."""
        state = ctx.state
        if decision.requires_fix_promotion:
            return self._promote(ctx, decision)
        if decision.reason != "ci_pending" or state.phase != _P.CI_VERIFICATION:
            return decision
        self._lease.revalidate(
            ctx, self._config.semantic_ci_timeout_seconds + CLAIM_TTL_SECONDS
        )
        ci = self._verify_ci(ctx.pr.temp_branch, state.head_sha)
        if ci is None:
            raise _Stop("ci_discarded", head_sha=state.head_sha)
        assert ctx.obs is not None
        # Decided at the end of the run: the next phase starts (and its deadline
        # counts) from now, and an attempt deadline passed during CI applies.
        self._now = self._clock().astimezone(UTC)
        obs = replace(ctx.obs, ci=ci, now=self._now)
        return evaluate_semantic_review_transition(state, obs)

    def _promote(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticTransitionDecision:
        state = ctx.state
        assert ctx.claim is not None and state.operation_id is not None
        result = self._lease.promote_fix_output(
            ctx, fix_branch=semantic_fix_output_branch(state.operation_id)
        )
        if result.status is not PushStatus.PUSHED or result.claim is None:
            if result.detail == DETAIL_FIX_NOT_DESCENDANT:
                return _escalated(state, "fix_output_not_descendant", self._stamp)
            raise _Stop("promotion_failed", detail=result.detail)
        assert ctx.obs is not None and ctx.obs.operation is not None
        promoted = str(ctx.obs.operation.output_sha)
        moved = evaluate_semantic_review_transition(
            state, replace(ctx.obs, observed_head_sha=promoted)
        )
        if moved.next_phase != _P.CI_VERIFICATION:
            raise _Stop("promotion_unconfirmed", reason=moved.reason)
        return moved

    def _apply(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticReviewStateV1:
        nxt = decision.next_state
        if decision.requires_review_trigger:
            self._effects.post_trigger(ctx, nxt)
        if nxt.phase == _P.INVALIDATED:
            self._lease.revalidate(ctx)
            self._claims.delete_fix_branches(self._root, nxt.attempt_id)
            self._effects.cleanup_fix_worktree(nxt.operation_id, nxt.provider_handle)
        if nxt != ctx.state:
            nxt = self._effects.save(ctx, nxt)
        if decision.requires_fix_dispatch:
            nxt = self._effects.dispatch_fix(ctx, nxt)
        if nxt.phase == _P.SKIPPED_BY_HUMAN and ctx.state.phase != nxt.phase:
            self._effects.note_skip(ctx, nxt, decision.reason)
        if nxt.phase == _P.ESCALATED and not self._effects.escalation_recorded(nxt):
            self._lease.revalidate(ctx)
            reason = nxt.escalation_reason or "escalated"
            guidance = self._ack_guidance(ctx, nxt) if reason == _ACK_REASON else ""
            self._effects.escalate(ctx, nxt, guidance)
        return nxt

    def _reviewer(self) -> str:
        """The reviewer Preflight resolved (same setting and dispatch target)."""
        target = getattr(self._config.dispatch_target, "target_name", None)
        resolution = describe_reviewer(
            self._config.semantic_review_provider,
            target if isinstance(target, str) else "local",
            self._config.reviewer_by_target,
            self._config.installed_review_vendors,
        )
        if resolution.reviewer is None:
            raise _Stop("reviewer_unresolved")
        return resolution.reviewer

    def _ack_guidance(self, ctx: _Attempt, state: SemanticReviewStateV1) -> str:
        """Facts and next steps of an unanswered request (#1436)."""
        review = ctx.review
        if not review.requests:  # recorded earlier: look at the PR again
            review = self._acquire_review(replace(ctx, state=state))
        return "\n\n" + self._diagnose(ctx, state, review)[0].guidance()

    def _report(
        self,
        ctx: _Attempt,
        status: str,
        state: SemanticReviewStateV1,
        **details: Any,
    ) -> SemanticReviewCycleReport:
        return SemanticReviewCycleReport(
            status=status,
            attempt_id=ctx.state.attempt_id,
            pr_number=ctx.pr.pr_number,
            previous_phase=ctx.state.phase.value,
            next_phase=state.phase.value,
            round=state.round,
            details=details,
        )


def advance_pending_semantic_reviews(
    config: DispatcherConfig,
    *,
    now: datetime | None = None,
    repository_root: Path | None = None,
    worker_id: str | None = None,
    verify_ci_fn: VerifyCiFn | None = None,
    operations: SemanticReviewOperations | None = None,
    claims: SemanticClaimClient | None = None,
    remote_head_fn: RemoteHeadFn | None = None,
    cleanup_refs: RemoteRefPorts | None = None,
) -> dict[str, Any]:
    """Advance every pending attempt of ``config``'s parent by at most one phase."""
    root = (repository_root or Path(".")).resolve()
    resolved_worker_id = worker_id or default_worker_id()
    runner = SemanticReviewCycleRunner(
        config,
        ports=SemanticReviewPorts(
            forge=config.resolved_forge,
            verify_ci_fn=verify_ci_fn
            or semantic_ci_verifier(config, root, remote_head_fn),
            operations=operations,
            claims=claims,
            remote_head_fn=remote_head_fn,
        ),
        repository_root=root,
        now=now or datetime.now(UTC),
        worker_id=resolved_worker_id,
    )
    reports = runner.run()
    statuses = sorted({report.status for report in reports})
    cleanup = run_cycle_cleanup(
        config,
        repository_root=root,
        worker_id=resolved_worker_id,
        claims=claims,
        poll=operations.poll if operations else None,
        refs=cleanup_refs,
    )
    return {
        "status": statuses[0] if len(statuses) == 1 else "mixed",
        "mode": config.semantic_review_mode,
        "attempts": [report.to_dict() for report in reports],
        "cleanup": cleanup,
    }


__all__ = [
    "SemanticClaimClient",
    "SemanticReviewCycleReport",
    "SemanticReviewCycleRunner",
    "SemanticReviewOperations",
    "SemanticReviewPorts",
    "SUCCESS_STATUSES",
    "WARNING_STATUSES",
    "advance_pending_semantic_reviews",
    "remote_branch_sha",
    "verify_semantic_ci",
]
