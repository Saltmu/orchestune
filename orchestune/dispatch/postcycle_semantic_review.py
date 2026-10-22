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

import hashlib
import os
import re
import socket
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.reviewer import resolve_reviewer_bot
from orchestune.dispatch.semantic_review_adapters import (
    SemanticOperationResult,
    SemanticReviewPreflightResult,
    dispatch_semantic_fix_agent,
    escalate_semantic_review_to_human,
    poll_semantic_operation,
    trigger_semantic_review_round,
    validate_semantic_review_preflight,
)
from orchestune.forge import Forge, ForgeAuthError
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import default_ci_command
from orchestune.integrator.attempt import (
    OpenAttempt,
    find_open_attempts,
)
from orchestune.integrator.ci_execution import run_ci_stages
from orchestune.integrator.semantic_claim import (
    DETAIL_FIX_NOT_DESCENDANT,
    ClaimResult,
    ClaimStatus,
    PushResult,
    PushStatus,
    SemanticClaim,
    acquire_semantic_claim,
    delete_attempt_fix_branches,
    promote_fix_output,
    release_semantic_claim,
    renew_semantic_claim,
)
from orchestune.issue_notice import latest_notice_body, post_notice_if_changed
from orchestune.labels import StatusLabel
from orchestune.pr_link_notice_trust import NoticePresence, TrustContext
from orchestune.review.acquisition import collect_review_state
from orchestune.review.markers import derive_review_target
from orchestune.review.rounds import (
    activity_bounds,
    build_restorable_trigger_body,
    restore_triggers,
    trigger_comment_ids,
)
from orchestune.review.semantic_gate import (
    ACTIVE_PENDING_PHASES,
    IN_FLIGHT_OPERATION_STATUSES,
    IntegrationAttemptIdentity,
    SemanticCiConclusion,
    SemanticCiObservation,
    SemanticJudgmentDecision,
    SemanticOperationObservation,
    SemanticOperationStatus,
    SemanticReviewHumanOverrideV1,
    SemanticReviewMode,
    SemanticReviewPhase,
    SemanticReviewRetryV1,
    SemanticReviewStateV1,
    SemanticTransitionDecision,
    SemanticTransitionObservation,
    evaluate_semantic_judgments,
    evaluate_semantic_review_transition,
    format_semantic_review_state_marker,
    restore_latest_fix_result,
    restore_latest_human_override,
    restore_latest_retry_request,
    restore_latest_semantic_review_state,
    semantic_fix_output_branch,
)
from orchestune.review.snapshot import EvidenceContractError

_P = SemanticReviewPhase
#: The head check of `find_open_attempts`; a promoted fix output trips it until
#: the runner records the new head, so that one error is re-restored here.
_HEAD_MISMATCH = "semantic state head does not match the PR head"
_TARGET_PHASES = ACTIVE_PENDING_PHASES | {_P.ESCALATED}
_REVIEW_PHASES = frozenset({_P.REVIEW_PENDING, _P.FIX_IN_PROGRESS, _P.CI_VERIFICATION})
_UNSETTLED_OPERATIONS = IN_FLIGHT_OPERATION_STATUSES | {
    SemanticOperationStatus.INDETERMINATE
}
_CLAIM_TTL_SECONDS = 300
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_SEMANTIC_MARKERS = (
    "orchestune:semantic-review-",
    "orchestune:semantic-fix-result",
    "```orchestune-review-judgments",
)
_JUDGMENT_BLOCK = re.compile(
    r"^```orchestune-review-judgments[^\n]*\n.*?^```[^\S\n]*$", re.M | re.S
)
#: Statuses that leave an attempt for a human or a later cycle to settle.
WARNING_STATUSES = frozenset(
    {"state_unreadable", "inputs_unknown", "claim_unknown", "claim_lost"}
)

VerifyCiFn = Callable[[str, str], SemanticCiObservation | None]
RemoteHeadFn = Callable[[str], str | None]


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
class SemanticReviewOperations:
    """The #1361 operation adapters (replaceable for tests)."""

    preflight: Callable[..., SemanticReviewPreflightResult] = (
        validate_semantic_review_preflight
    )
    trigger: Callable[..., SemanticOperationResult] = trigger_semantic_review_round
    dispatch_fix: Callable[..., SemanticOperationResult] = dispatch_semantic_fix_agent
    poll: Callable[..., SemanticOperationResult] = poll_semantic_operation
    escalate: Callable[..., None] = escalate_semantic_review_to_human


@dataclass(frozen=True)
class SemanticClaimClient:
    """The #1370 claim API (replaceable for tests)."""

    acquire: Callable[..., ClaimResult] = acquire_semantic_claim
    renew: Callable[..., PushResult] = renew_semantic_claim
    release: Callable[..., PushResult] = release_semantic_claim
    promote: Callable[..., PushResult] = promote_fix_output
    delete_fix_branches: Callable[[str | Path, str], None] = delete_attempt_fix_branches


@dataclass(frozen=True)
class _Review:
    complete: bool = False
    acknowledged: bool = False
    result: dict[str, Any] | None = None


@dataclass
class _Attempt:
    """One attempt being advanced; ``claim`` is set once the claim is held."""

    pr: OpenAttempt
    state: SemanticReviewStateV1
    override: SemanticReviewHumanOverrideV1 | None
    comments: list[dict[str, Any]]
    trust: TrustContext
    review: _Review = _Review()
    obs: SemanticTransitionObservation | None = None
    claim: SemanticClaim | None = None


class _Stop(Exception):
    """Stop advancing one attempt with a report status (nothing more is written)."""

    def __init__(self, status: str, **details: Any) -> None:
        super().__init__(status)
        self.status = status
        self.details = details


def remote_branch_sha(repository_root: str | Path, branch: str) -> str | None:
    """The remote tip of ``branch``; ``None`` only when the branch is absent."""
    result = run_git(
        ["ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        cwd=repository_root,
        check=False,
    )
    if result.returncode == 2:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"failed to read origin/{branch}: {result.stderr}")
    return result.stdout.split()[0].lower()


def verify_semantic_ci(
    temp_branch: str,
    expected_head_sha: str,
    *,
    config: DispatcherConfig,
    repository_root: Path,
    remote_head_fn: RemoteHeadFn | None = None,
    runner: Any = None,
) -> SemanticCiObservation | None:
    """Run ``ci_command`` on a detached checkout of exactly ``expected_head_sha``.

    Integration PRs target ``parent/issue-{N}``, where GitHub CI does not run.
    The result is discarded (``None``) when the remote head is not the expected
    one before or after the run.
    """
    remote = remote_head_fn or (lambda name: remote_branch_sha(repository_root, name))
    if remote(temp_branch) != expected_head_sha:
        return None
    policy = replace(
        config.integration_execution_policy,
        integration_ci_timeout_seconds=config.semantic_ci_timeout_seconds,
    )
    command = config.ci_command or default_ci_command()
    run_git(
        ["fetch", "--no-tags", "origin", f"+refs/heads/{temp_branch}"],
        cwd=repository_root,
    )
    with tempfile.TemporaryDirectory(prefix="orchestune-semantic-ci-") as directory:
        checkout = Path(directory) / "head"
        run_git(
            ["worktree", "add", "--detach", str(checkout), expected_head_sha],
            cwd=repository_root,
        )
        try:
            result = run_ci_stages(
                command, checkout, repository_root, policy=policy, runner=runner
            )
        finally:
            run_git(
                ["worktree", "remove", "--force", str(checkout)],
                cwd=repository_root,
                check=False,
            )
    if remote(temp_branch) != expected_head_sha:
        return None
    conclusion = (
        SemanticCiConclusion.SUCCESS if result.ok else SemanticCiConclusion.FAILURE
    )
    return SemanticCiObservation(expected_head_sha, conclusion)


def _state_authors(trust: TrustContext) -> frozenset[str]:
    """Orchestune itself and configured bot collaborators (the #1360 state rule)."""
    trusted: set[str] = set()
    identity = trust.get_self_identity()
    if identity:
        trusted.add(identity.casefold())
    trusted.update(
        author.casefold()
        for author in trust.get_trusted_collaborators() or ()
        if author.casefold().endswith("[bot]")
    )
    return frozenset(trusted)


def _authored(
    comments: list[dict[str, Any]],
    trust: TrustContext,
    accept: Callable[[str], bool],
) -> list[tuple[str, str]]:
    """``(body, author)`` of the comments whose author ``accept`` admits."""
    selected = []
    for comment in comments:
        author, body = trust.extract_author(comment), comment.get("body")
        if author and isinstance(body, str) and accept(author.casefold()):
            selected.append((body, author))
    return selected


def _is_target(attempt: OpenAttempt) -> bool:
    if attempt.state_error is not None:
        return True
    return attempt.state is not None and attempt.state.phase in _TARGET_PHASES


def _escalation_notice_kind(attempt_id: str, reason: str) -> str:
    """The notice kind `escalate_semantic_review_to_human` posts for one reason."""
    key = hashlib.sha256(f"{attempt_id}:{reason}".encode()).hexdigest()[:16]
    return f"semantic-review:{attempt_id}:{key}"


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
        forge: Forge,
        repository_root: Path,
        now: datetime,
        worker_id: str,
        verify_ci_fn: VerifyCiFn,
        operations: SemanticReviewOperations | None = None,
        claims: SemanticClaimClient | None = None,
        remote_head_fn: RemoteHeadFn | None = None,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        self._config = config
        self._forge = forge
        self._root = repository_root
        self._now = now.astimezone(UTC)
        self._worker = worker_id
        self._verify_ci = verify_ci_fn
        self._ops = operations or SemanticReviewOperations()
        self._claims = claims or SemanticClaimClient()
        self._remote_head = remote_head_fn or (
            lambda name: remote_branch_sha(repository_root, name)
        )
        self._mode = SemanticReviewMode(config.semantic_review_mode)

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

    # -- restoration and observation ---------------------------------------

    def _advance_attempt(self, pr: OpenAttempt) -> SemanticReviewCycleReport:
        """One attempt's failure is reported and never stops the others."""
        ctx: _Attempt | None = None
        try:
            ctx = self._restore(pr)
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

    def _restore(self, pr: OpenAttempt) -> _Attempt:
        comments = self._forge.list_all_issue_comments(pr.pr_number)
        trust = TrustContext(self._forge)
        if pr.state is not None:
            return _Attempt(pr, pr.state, pr.override, comments, trust)
        if pr.state_error != _HEAD_MISMATCH:
            raise _Stop("state_unreadable", error=pr.state_error)
        try:
            trusted = _state_authors(trust)
            state = restore_latest_semantic_review_state(
                [body for body, _ in _authored(comments, trust, trusted.__contains__)]
            )
        except ValueError as error:
            raise _Stop("state_unreadable", error=str(error)) from error
        if state is None or state.pr_number != pr.pr_number:
            raise _Stop("state_unreadable", error=pr.state_error)
        override = None
        for body, actor in reversed(self._human_bodies(comments, trust)):
            found = restore_latest_human_override(
                [body], attempt_id=state.attempt_id, head_sha=state.head_sha
            )
            if found is not None:
                override = replace(found, actor=actor)
                break
        return _Attempt(pr, state, override, comments, trust)

    def _human_bodies(
        self, comments: list[dict[str, Any]], trust: TrustContext
    ) -> list[tuple[str, str]]:
        """Bodies of humans with write access (the #1360 override rule)."""
        permissions: dict[str, str] = {}

        def allowed(author: str) -> bool:
            if author.casefold().endswith("[bot]"):
                return False
            if author not in permissions:
                permissions[author] = self._forge.get_actor_permission(author)
            return permissions[author].casefold() in _WRITE_PERMISSIONS

        return _authored(comments, trust, allowed)

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
            judgments = self._judgments(ctx)
        return SemanticTransitionObservation(
            identity=self._current_identity(state),
            observed_head_sha=ctx.pr.head_sha,
            now=self._now,
            mode=self._mode,
            limits=self._config.semantic_review_limits,
            override=ctx.override,
            retry_request=self._retry_request(ctx)
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
        try:
            triggers = restore_triggers(comments)
        except (EvidenceContractError, ValueError):
            return _Review()
        current = [
            t
            for t in triggers
            if t.round == state.round and t.requested_head_sha == state.head_sha
        ]
        if len(current) != 1:
            return _Review()
        trigger = current[0]
        later = [t for t in triggers if t.round > trigger.round]
        started, ended = activity_bounds(comments, triggers, trigger)
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
        result = collect_review_state(
            snapshot,
            trigger.reviewer,
            exclude_issue_comment_ids=excluded,
            round_started_at=trigger.created_at,
            round_ended_at=later[0].created_at if later else "",
            requested_head_sha=state.head_sha,
            activity_started_at=started,
            activity_ended_at=ended,
        )
        acknowledged = result["acquisition_status"] != "unavailable"
        if result["acquisition_status"] != "acquired":
            return _Review(acknowledged=acknowledged)
        target, _ = derive_review_target(
            result["review_items"], state.head_sha, state.head_sha
        )
        result = {**result, "round": state.round}
        return _Review(target == state.head_sha, True, result)

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
        return SemanticOperationObservation(
            state.operation_id, result.status, output, result.input_sha
        )

    def _trusted(self, ctx: _Attempt, comment: dict[str, Any]) -> bool:
        presence, _ = ctx.trust.evaluate_candidate_markers([comment])
        return presence is NoticePresence.PRESENT

    def _judgment_comment(self, ctx: _Attempt) -> dict[str, Any] | None:
        """The trusted table the round's job named in its trusted result marker."""
        state = ctx.state
        bodies = [
            str(c["body"])
            for c in ctx.comments
            if isinstance(c.get("body"), str) and self._trusted(ctx, c)
        ]
        try:
            result = restore_latest_fix_result(
                bodies,
                attempt_id=state.attempt_id,
                operation_id=str(state.operation_id),
            )
        except ValueError:
            return None
        if result is None or result.judgment_comment_id is None:
            return None
        for comment in ctx.comments:
            if comment.get("id") == result.judgment_comment_id:
                return comment if self._trusted(ctx, comment) else None
        return None

    def _judgments(self, ctx: _Attempt) -> SemanticJudgmentDecision | None:
        comment = self._judgment_comment(ctx)
        if comment is None:
            return None
        return evaluate_semantic_judgments(
            str(comment.get("body") or ""),
            ctx.review.result if ctx.review.complete else None,
            expected_round=ctx.state.round,
        )

    def _retry_request(self, ctx: _Attempt) -> SemanticReviewRetryV1 | None:
        """The latest unconsumed retry/restart a collaborator posted for this head."""
        state = ctx.state
        bodies = [body for body, _ in self._human_bodies(ctx.comments, ctx.trust)]
        consumed = {state.last_retry_request_id} - {None}
        try:
            return restore_latest_retry_request(
                bodies,
                attempt_id=state.attempt_id,
                head_sha=state.head_sha,
                consumed_request_ids=cast(set[str], consumed),
            )
        except ValueError:
            return None

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
            reason = "review_not_acknowledged"
            nxt = replace(decision.next_state, escalation_reason=reason)
            return replace(decision, reason=reason, next_state=nxt)
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
        return ctx.state.phase == _P.ESCALATED and not self._escalation_recorded(
            ctx.state
        )

    def _carry_out(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticReviewCycleReport:
        if not self._needs_work(ctx, decision):
            return self._report(
                ctx, "held", decision.next_state, reason=decision.reason
            )
        if not self._config.apply:
            return self._report(
                ctx, "planned", decision.next_state, reason=decision.reason
            )
        self._acquire(ctx)
        try:
            decision = self._claimed_observations(ctx, decision)
            final = self._apply(ctx, decision)
        finally:
            self._release(ctx)
        return self._report(ctx, "advanced", final, reason=decision.reason)

    def _acquire(self, ctx: _Attempt) -> None:
        result = self._claims.acquire(
            self._root,
            parent_issue_number=ctx.state.parent_issue_number,
            attempt_id=ctx.state.attempt_id,
            holder=self._worker,
            now=self._now,
            ttl_seconds=_CLAIM_TTL_SECONDS,
        )
        if result.status is ClaimStatus.HELD_BY_OTHER:
            holder = result.claim.holder if result.claim else None
            raise _Stop("claimed_by_other", holder=holder)
        if result.status is not ClaimStatus.ACQUIRED or result.claim is None:
            raise _Stop("claim_unknown", detail=result.detail)
        ctx.claim = result.claim

    def _revalidate(self, ctx: _Attempt, ttl: float = _CLAIM_TTL_SECONDS) -> None:
        """Renew (and so re-prove) ownership right before an external write."""
        assert ctx.claim is not None
        result = self._claims.renew(
            self._root, ctx.claim, now=self._now, ttl_seconds=ttl
        )
        if result.status is not PushStatus.PUSHED or result.claim is None:
            ctx.claim = None
            raise _Stop("claim_lost", detail=result.detail)
        ctx.claim = result.claim

    def _release(self, ctx: _Attempt) -> None:
        if ctx.claim is None:
            return
        try:
            self._claims.release(self._root, ctx.claim, now=self._now)
        except Exception:  # an unreleased claim only expires later  # noqa: BLE001
            pass
        ctx.claim = None

    def _claimed_observations(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticTransitionDecision:
        """Promotion and CI run only under the claim; each may yield a new decision."""
        state = ctx.state
        if decision.requires_fix_promotion:
            return self._promote(ctx, decision)
        if decision.reason != "ci_pending" or state.phase != _P.CI_VERIFICATION:
            return decision
        self._revalidate(
            ctx, self._config.semantic_ci_timeout_seconds + _CLAIM_TTL_SECONDS
        )
        ci = self._verify_ci(ctx.pr.temp_branch, state.head_sha)
        if ci is None:
            raise _Stop("ci_discarded", head_sha=state.head_sha)
        assert ctx.obs is not None
        return evaluate_semantic_review_transition(state, replace(ctx.obs, ci=ci))

    def _promote(
        self, ctx: _Attempt, decision: SemanticTransitionDecision
    ) -> SemanticTransitionDecision:
        state = ctx.state
        assert ctx.claim is not None and state.operation_id is not None
        result = self._claims.promote(
            self._root,
            ctx.claim,
            temp_branch=ctx.pr.temp_branch,
            expected_head_sha=str(state.fix_input_sha),
            fix_branch=semantic_fix_output_branch(state.operation_id),
            now=self._now,
        )
        if result.status is not PushStatus.PUSHED or result.claim is None:
            if result.detail == DETAIL_FIX_NOT_DESCENDANT:
                return _escalated(state, "fix_output_not_descendant", self._stamp)
            raise _Stop("promotion_failed", detail=result.detail)
        ctx.claim = result.claim
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
            self._post_trigger(ctx, nxt)
        if nxt.phase == _P.INVALIDATED:
            self._revalidate(ctx)
            self._claims.delete_fix_branches(self._root, nxt.attempt_id)
        if nxt != ctx.state:
            nxt = self._save(ctx, nxt)
        if decision.requires_fix_dispatch:
            nxt = self._dispatch_fix(ctx, nxt)
        if nxt.phase == _P.SKIPPED_BY_HUMAN and ctx.state.phase != nxt.phase:
            self._note_skip(ctx, nxt, decision.reason)
        if nxt.phase == _P.ESCALATED and not self._escalation_recorded(nxt):
            self._revalidate(ctx)
            self._escalate(ctx, nxt)
        return nxt

    def _save(
        self, ctx: _Attempt, state: SemanticReviewStateV1
    ) -> SemanticReviewStateV1:
        self._revalidate(ctx)
        assert ctx.claim is not None
        stored = replace(
            state,
            claim_holder=ctx.claim.holder,
            claim_generation=ctx.claim.generation,
            claim_expires_at=ctx.claim.expires_at.isoformat(),
        )
        self._forge.add_comment(
            ctx.pr.pr_number, format_semantic_review_state_marker(stored)
        )
        return stored

    def _reviewer(self) -> str:
        """The reviewer Preflight resolved (same setting and dispatch target)."""
        target = getattr(self._config.dispatch_target, "target_name", None)
        provider = self._config.semantic_review_provider
        reviewer = None
        if provider in {"auto", "claude", "codex"}:
            reviewer = resolve_reviewer_bot(
                cast(Literal["auto", "claude", "codex"], provider),
                target if isinstance(target, str) else "local",
            )
        if reviewer is None:
            raise _Stop("reviewer_unresolved")
        return reviewer

    def _trigger_reply(self, ctx: _Attempt, nxt: SemanticReviewStateV1) -> str:
        """Round 1 asks for a review; later rounds carry the previous table and SHA."""
        intro = (
            f"Layer-2 semantic review of integration attempt `{nxt.attempt_id[:12]}` "
            f"for parent #{nxt.parent_issue_number}: review the combined diff "
            "for cross-task defects."
        )
        comment = self._judgment_comment(ctx) if ctx.state.operation_id else None
        block = (
            _JUDGMENT_BLOCK.search(str(comment.get("body") or "")) if comment else None
        )
        if nxt.round <= 1 or block is None:
            return f"{intro}\n\nReview head: `{nxt.head_sha}`."
        return (
            f"{intro}\n\nRound {ctx.state.round} judgments, addressed at "
            f"`{nxt.head_sha}`:\n\n{block.group(0)}\n"
        )

    def _post_trigger(self, ctx: _Attempt, nxt: SemanticReviewStateV1) -> None:
        reviewer = self._reviewer()
        reply = self._trigger_reply(ctx, nxt)
        try:
            build_restorable_trigger_body(reply, reviewer, nxt.round, nxt.head_sha)
        except EvidenceContractError:
            reply = f"Review head: `{nxt.head_sha}`."
        self._revalidate(ctx)
        result = self._ops.trigger(
            self._forge,
            pr_number=ctx.pr.pr_number,
            attempt_id=nxt.attempt_id,
            round_number=nxt.round,
            head_sha=nxt.head_sha,
            reviewer=reviewer,
            reply_body=reply,
        )
        if result.status is SemanticOperationStatus.INDETERMINATE:
            raise _Stop("trigger_indeterminate", error=result.error_code)

    def _dispatch_fix(
        self, ctx: _Attempt, reserved: SemanticReviewStateV1
    ) -> SemanticReviewStateV1:
        """Launch the reserved job once; an unknown launch is never relaunched."""
        assert reserved.operation_id is not None
        self._revalidate(ctx)
        result = self._ops.dispatch_fix(
            self._config.dispatch_target,
            pr_number=ctx.pr.pr_number,
            attempt_id=reserved.attempt_id,
            operation_id=reserved.operation_id,
            temp_branch=ctx.pr.temp_branch,
            expected_head_sha=reserved.head_sha,
            round_number=reserved.round,
        )
        launched = replace(
            reserved,
            operation_status=result.status,
            provider_handle=result.provider_handle,
            state_revision=reserved.state_revision + 1,
            updated_at=self._stamp,
        )
        return self._save(ctx, launched)

    def _escalation_recorded(self, state: SemanticReviewStateV1) -> bool:
        reason = state.escalation_reason or "escalated"
        kind = _escalation_notice_kind(state.attempt_id, reason)
        try:
            comments = self._forge.list_comments(state.parent_issue_number)
        except Exception:  # unreadable: never risk a duplicate notice  # noqa: BLE001
            return True
        return latest_notice_body(comments, kind) is not None

    def _escalate(self, ctx: _Attempt, state: SemanticReviewStateV1) -> None:
        reason = state.escalation_reason or "escalated"
        source = state.escalated_from.value if state.escalated_from else "unknown"
        self._ops.escalate(
            self._forge,
            parent_issue_number=state.parent_issue_number,
            pr_number=ctx.pr.pr_number,
            attempt_id=state.attempt_id,
            reason=reason,
            details=(
                f"stopped at `{source}` (round {state.round}, head `{state.head_sha}`); "
                "the head is kept. Resume with a semantic-review-retry marker or skip "
                "with a semantic-review-override marker bound to this head."
            ),
        )

    def _note_skip(
        self, ctx: _Attempt, state: SemanticReviewStateV1, reason: str
    ) -> None:
        override = ctx.override if reason == "human_override" else None
        by = f" by @{override.actor}: {override.reason}" if override else ""
        body = (
            "## Layer-2 semantic review skipped\n\n"
            f"- Attempt: `{state.attempt_id}`\n- Head: `{state.head_sha}`\n"
            f"- Reason: `{reason}`{by}"
        )
        post_notice_if_changed(
            self._forge,
            ctx.pr.pr_number,
            f"semantic-review-skip:{state.attempt_id}",
            body,
        )

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


def _default_worker_id() -> str:
    return f"semantic-runner@{socket.gethostname()}:{os.getpid()}"


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
) -> dict[str, Any]:
    """Advance every pending attempt of ``config``'s parent by at most one phase."""
    root = (repository_root or Path(".")).resolve()

    def verify(
        temp_branch: str, expected_head_sha: str
    ) -> SemanticCiObservation | None:
        return verify_semantic_ci(
            temp_branch,
            expected_head_sha,
            config=config,
            repository_root=root,
            remote_head_fn=remote_head_fn,
        )

    runner = SemanticReviewCycleRunner(
        config,
        forge=config.resolved_forge,
        repository_root=root,
        now=now or datetime.now(UTC),
        worker_id=worker_id or _default_worker_id(),
        verify_ci_fn=verify_ci_fn or verify,
        operations=operations,
        claims=claims,
        remote_head_fn=remote_head_fn,
    )
    reports = runner.run()
    statuses = sorted({report.status for report in reports})
    return {
        "status": statuses[0] if len(statuses) == 1 else "mixed",
        "mode": config.semantic_review_mode,
        "attempts": [report.to_dict() for report in reports],
    }


__all__ = [
    "SemanticClaimClient",
    "SemanticReviewCycleReport",
    "SemanticReviewCycleRunner",
    "SemanticReviewOperations",
    "WARNING_STATUSES",
    "advance_pending_semantic_reviews",
    "remote_branch_sha",
    "verify_semantic_ci",
]
