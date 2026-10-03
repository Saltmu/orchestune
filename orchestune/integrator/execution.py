"""Per-parent execution state: deadline scope, attempt budget, holds, escalation (#820).

``ExecutionState`` is created once per parent Issue integration cycle by
``SingleIssueIntegrator.execute``. It owns

* the monotonic ``ExecutionScope`` (deadline + cleanup budget + command limit),
* the parent execution lock held from reservation to the recorded result,
* the lazily written ``reserved`` / ``finished`` / ``terminal`` GitHub events,
* the hold that keeps a worktree and its diagnostics when a stop, rollback or write
  cannot be confirmed, and
* escalation to human review through the shared ``apply_human_review_escalation``.

A confirmed timeout is a *retryable* outcome; an unconfirmed stop, a failed rollback,
a HEAD mismatch, an exhausted cleanup budget or an unknown write is *indeterminate*
and is held for a human. Neither is folded into an ordinary CI failure, so neither
re-queues the worker through ``handle_merge_failure``.
"""

from __future__ import annotations

import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestune.forge import forge_supports_bounded_execution
from orchestune.infra.execution_deadline import (
    ExecutionCommandTimeout,
    ExecutionDeadlineExceeded,
    ExecutionInterrupt,
    ExecutionScope,
)
from orchestune.infra.process_utils import FileLockContentionError, file_lock
from orchestune.integrator.timeout_policy import (
    COUNTED_TIMEOUT_CAUSES,
    SIDE_EFFECT_NONE,
    SIDE_EFFECT_UNKNOWN,
    STATUS_EXECUTION_CLEANUP_FAILED,
    STATUS_EXECUTION_INDETERMINATE,
    STATUS_EXECUTION_RETRY_EXHAUSTED,
    STATUS_EXECUTION_TIMED_OUT,
    ExecutionFailureCause,
    IntegrationExecutionPolicy,
)
from orchestune.integrator.timeout_retry import (
    ESCALATION_MARKER,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    BudgetState,
    BudgetVerdict,
    EventWriteUnconfirmed,
    ExecutionBudgetStore,
    ExecutionEvent,
    IssueCommentForge,
    Target,
    planned_retry,
)
from orchestune.integrator.worktree import IntegrationWorktree
from orchestune.labels import StatusLabel
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.worktree_ops.temp_branches import load_holds

OUTPUT_TAIL_CHARS = 4000


def _monotonic() -> float:
    """Monotonic seconds; a seam so tests can move the cycle clock."""
    return time.monotonic()


LOCK_NAME = "integration-parent-issue-{parent}-execution.lock"


def parent_execution_lock_path(original_root: Path, parent_issue_number: int) -> Path:
    """The per-parent execution lock; deliberately has no run id in its name.

    Held from budget reservation to the recorded result so two runs for one parent
    cannot race the retry budget. It is separate from the short ref-update locks and
    never blocks a different parent's run.
    """
    return (
        original_root
        / "worktrees"
        / ".locks"
        / LOCK_NAME.format(parent=parent_issue_number)
    )


@dataclass
class ExecutionFailure:
    """One structured execution failure for ``IntegrationReport.execution_failures``."""

    cause: ExecutionFailureCause
    stage: str
    parent_issue_number: int
    issue_number: int | None = None
    subtask_id: str | None = None
    source_sha: str | None = None
    configured_limit_seconds: float | None = None
    effective_limit_seconds: float | None = None
    elapsed_seconds: float | None = None
    attempt_id: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    next_retry_at: str | None = None
    stop_confirmed: bool | None = None
    rollback_confirmed: bool | None = None
    side_effect_state: str = SIDE_EFFECT_NONE
    output_tail: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {key: value for key, value in self.__dict__.items()}
        data["cause"] = self.cause.value
        return data

    @property
    def confirmed_safe(self) -> bool:
        """Stop and rollback are confirmed and no write is in doubt."""
        return (
            self.stop_confirmed is not False
            and self.rollback_confirmed is True
            and self.side_effect_state == SIDE_EFFECT_NONE
        )


class IntegrationExecutionAbort(ExecutionInterrupt):
    """Abort this parent's integration without recording per-task CI failure.

    ``started`` is ``False`` when nothing was run (backoff, exhausted budget, hold,
    unreadable history). ``hold`` keeps the worktree and diagnostics for a human.
    """

    def __init__(
        self,
        failure: ExecutionFailure,
        *,
        hold: bool = False,
        started: bool = True,
        status: str | None = None,
    ) -> None:
        super().__init__(f"{failure.cause.value} at {failure.stage}: {failure.detail}")
        self.failure = failure
        self.hold = hold
        self.started = started
        self.status = status


@dataclass
class FinalOutcome:
    """What the cycle's last accounting step decided."""

    status: str | None = None
    failures: list[ExecutionFailure] = field(default_factory=list)
    error: str | None = None


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


@dataclass
class ExecutionState:
    parent_issue_number: int
    policy: IntegrationExecutionPolicy
    scope: ExecutionScope
    store: ExecutionBudgetStore
    original_root: Path
    temp_branch: str
    reserved: ExecutionEvent | None = None
    budget: BudgetState | None = None
    targets: list[Target] = field(default_factory=list)
    failures: list[ExecutionFailure] = field(default_factory=list)
    abort: IntegrationExecutionAbort | None = None
    hold_reason: str | None = None
    final_status: str | None = None
    # The pipeline defers removing the temporary worktree until the attempt's result
    # is saved, so an unconfirmed result can still hold it.
    worktree_removal_pending: bool = False

    # ------------------------------------------------------------------ setup

    @classmethod
    def create(
        cls,
        *,
        parent_issue_number: int,
        policy: IntegrationExecutionPolicy,
        forge: Any,
        original_root: Path,
        temp_branch: str,
    ) -> ExecutionState:
        scope = ExecutionScope(
            cycle_seconds=policy.integration_cycle_timeout_seconds,
            cleanup_seconds=policy.integration_cleanup_timeout_seconds,
            command_seconds=policy.integration_command_timeout_seconds,
            clock=_monotonic,
        )
        return cls(
            parent_issue_number=parent_issue_number,
            policy=policy,
            scope=scope,
            store=ExecutionBudgetStore(forge, parent_issue_number, policy, scope=scope),
            original_root=original_root,
            temp_branch=temp_branch,
        )

    @staticmethod
    def forge_is_bounded(forge: object) -> bool:
        return forge_supports_bounded_execution(forge)

    def acquire_parent_lock(self, stack: ExitStack) -> str | None:
        """Take the parent execution lock within the cycle budget; ``None`` on success."""
        wait = min(
            self.scope.remaining(),
            float(self.policy.integration_command_timeout_seconds),
        )
        path = parent_execution_lock_path(self.original_root, self.parent_issue_number)
        try:
            stack.enter_context(file_lock(path, timeout=max(0.0, wait)))
        except FileLockContentionError as error:
            return str(error)
        return None

    # ---------------------------------------------------------------- failures

    def make_failure(
        self,
        cause: ExecutionFailureCause,
        stage: str,
        *,
        target: Target | None = None,
        **fields: Any,
    ) -> ExecutionFailure:
        target = target or (self.targets[-1] if self.targets else None)
        timeouts = self.budget.timeouts if self.budget is not None else 0
        return ExecutionFailure(
            cause=cause,
            stage=stage,
            parent_issue_number=self.parent_issue_number,
            issue_number=None if target is None else target.issue_number,
            subtask_id=None if target is None else target.subtask_id,
            source_sha=None if target is None else target.source_sha,
            attempt_id=None if self.reserved is None else self.reserved.attempt_id,
            attempt=timeouts + 1,
            max_attempts=self.policy.max_attempts,
            elapsed_seconds=self.scope.elapsed(),
            **fields,
        )

    def note_target(self, target: Target) -> None:
        if target not in self.targets:
            self.targets.append(target)

    # --------------------------------------------------------------- attempts

    def begin_attempt(self, target: Target, stage: str) -> None:
        """Reserve the cycle's attempt before the first dependency/CI stage starts.

        Idempotent per cycle. Raises ``IntegrationExecutionAbort`` with
        ``started=False`` when the history forbids running, cannot be read, or the
        reservation cannot be confirmed; nothing is started in those cases.
        """
        self.note_target(target)
        if self.reserved is not None:
            return
        state = self.store.load()
        self.budget = state
        if state.verdict is not BudgetVerdict.PROCEED:
            raise self._blocked(state, stage)
        local = self._local_hold_block(state, stage)
        if local is not None:
            raise local
        try:
            self.reserved = self.store.reserve(state, [target], stage)
        except (EventWriteUnconfirmed, ExecutionDeadlineExceeded) as error:
            raise IntegrationExecutionAbort(
                self.make_failure(
                    ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE,
                    stage,
                    target=target,
                    side_effect_state=SIDE_EFFECT_UNKNOWN,
                    detail=f"reservation not confirmed: {error}",
                ),
                started=False,
                status=STATUS_EXECUTION_INDETERMINATE,
            ) from error

    def _local_hold_block(
        self, state: BudgetState, stage: str
    ) -> IntegrationExecutionAbort | None:
        """A local hold for this parent blocks new CI until a reset opens a newer generation.

        The GitHub history normally carries the same fact; the local record also covers
        a result that could not be saved. Records that cannot be read are never read
        as "no hold".
        """
        holds = load_holds(self.original_root)
        if holds is None:
            detail = "hold records could not be reconciled; refusing to start"
        else:
            mine = [
                hold
                for hold in holds
                if hold.get("parent_issue_number") == self.parent_issue_number
                and int(hold.get("generation", 1)) >= state.generation
            ]
            if not mine:
                return None
            detail = (
                f"a held integration worktree exists for parent "
                f"#{self.parent_issue_number} ({mine[0].get('reason')}); verify it "
                "and post a reset before integrating again"
            )
        return IntegrationExecutionAbort(
            self.make_failure(
                ExecutionFailureCause.CLEANUP_FAILED
                if holds is not None
                else ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE,
                stage,
                side_effect_state=SIDE_EFFECT_UNKNOWN,
                detail=detail,
            ),
            started=False,
            status=STATUS_EXECUTION_CLEANUP_FAILED
            if holds is not None
            else STATUS_EXECUTION_INDETERMINATE,
        )

    def _blocked(self, state: BudgetState, stage: str) -> IntegrationExecutionAbort:
        verdict = state.verdict
        if verdict is BudgetVerdict.BACKOFF:
            try:
                cause = ExecutionFailureCause(state.last_cause or "")
            except ValueError:
                cause = ExecutionFailureCause.CI_TIMEOUT
            return IntegrationExecutionAbort(
                self.make_failure(
                    cause,
                    stage,
                    next_retry_at=state.next_retry_at,
                    stop_confirmed=True,
                    rollback_confirmed=True,
                    detail=state.reason,
                ),
                started=False,
                status=STATUS_EXECUTION_TIMED_OUT,
            )
        if verdict is BudgetVerdict.EXHAUSTED:
            return IntegrationExecutionAbort(
                self.make_failure(
                    ExecutionFailureCause.RETRY_BUDGET_EXHAUSTED,
                    stage,
                    detail=state.reason,
                ),
                started=False,
                status=STATUS_EXECUTION_RETRY_EXHAUSTED,
            )
        cleanup = state.last_cause == ExecutionFailureCause.CLEANUP_FAILED.value
        needs_human = verdict is BudgetVerdict.HOLD
        return IntegrationExecutionAbort(
            self.make_failure(
                ExecutionFailureCause.CLEANUP_FAILED
                if cleanup and needs_human
                else ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE,
                stage,
                side_effect_state=SIDE_EFFECT_UNKNOWN,
                detail=state.reason,
            ),
            started=False,
            status=STATUS_EXECUTION_CLEANUP_FAILED
            if cleanup and needs_human
            else STATUS_EXECUTION_INDETERMINATE,
        )

    def timeout_abort(
        self,
        error: BaseException,
        stage: str,
        *,
        write_step: bool,
        before_start: bool,
    ) -> IntegrationExecutionAbort:
        """Classify a deadline/command timeout raised outside the CI runner.

        A timeout inside a step that writes to git/GitHub leaves the write's outcome
        unknown: nothing is guessed, retried or rolled back remotely. A timeout before
        any write only ends the cycle.
        """
        if write_step and not before_start:
            return IntegrationExecutionAbort(
                self.make_failure(
                    ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE,
                    stage,
                    side_effect_state=SIDE_EFFECT_UNKNOWN,
                    detail=(
                        f"{type(error).__name__} during {stage}; whether the write "
                        "took effect is unknown. Reconcile the remote refs and "
                        "existing integration evidence before anything is pushed or "
                        f"completed again: {error}"
                    ),
                ),
                hold=True,
                status=STATUS_EXECUTION_INDETERMINATE,
            )
        return IntegrationExecutionAbort(
            self.make_failure(
                ExecutionFailureCause.CYCLE_DEADLINE_EXCEEDED,
                stage,
                configured_limit_seconds=float(
                    self.policy.integration_cycle_timeout_seconds
                ),
                effective_limit_seconds=self.scope.cycle_seconds,
                stop_confirmed=True,
                rollback_confirmed=True,
                detail=f"{type(error).__name__} before any write: {error}",
            ),
            status=STATUS_EXECUTION_TIMED_OUT,
        )

    # ------------------------------------------------------------------- hold

    def hold(self, reason: str) -> None:
        """Keep the worktree, temp branch and diagnostics; never auto-reclaim them."""
        self.hold_reason = reason
        try:
            IntegrationWorktree(self.original_root, self.temp_branch).write_hold(
                parent_issue_number=self.parent_issue_number,
                attempt_id=None if self.reserved is None else self.reserved.attempt_id,
                reason=reason,
                generation=1 if self.budget is None else self.budget.generation,
            )
        except OSError as error:
            print(
                f"Warning: failed to write the integration hold record: {error}",
                file=sys.stderr,
            )

    @property
    def holding(self) -> bool:
        return self.hold_reason is not None

    # -------------------------------------------------------------- escalation

    def escalate(self, reason: str, failures: list[ExecutionFailure]) -> None:
        """Send the parent to human review once; failure to label never re-runs CI."""
        forge: Any = self.store.forge
        try:
            labels = tuple(forge.get_issue_labels(self.parent_issue_number))
            if StatusLabel.BLOCKED_HUMAN_REVIEW in labels:
                return
            lines = [
                ESCALATION_MARKER,
                f"Integration execution for parent #{self.parent_issue_number} needs a "
                f"human: {reason}",
                "",
            ]
            for failure in failures:
                lines.append(
                    f"- `{failure.cause.value}` at `{failure.stage}` "
                    f"(attempt `{failure.attempt_id}`, stop_confirmed="
                    f"{failure.stop_confirmed}, rollback_confirmed="
                    f"{failure.rollback_confirmed}, side_effect="
                    f"{failure.side_effect_state})"
                )
            lines += [
                "",
                "Verify the processes, the worktree and the remote refs, then post a "
                "reasoned `reset` event (see the operations guide) to release it.",
            ]
            apply_human_review_escalation(
                self.parent_issue_number, labels, "\n".join(lines), forge=forge
            )
        except (Exception, ExecutionCommandTimeout, ExecutionDeadlineExceeded) as error:
            print(
                f"Warning: failed to escalate parent #{self.parent_issue_number} to "
                f"human review: {error}",
                file=sys.stderr,
            )

    # --------------------------------------------------------------- finalize

    def finalize(self, *, normal_success: bool) -> FinalOutcome:
        """Record the attempt's result; the last accounting step of the cycle.

        Saving runs on the cleanup budget once the deadline has passed. A result that
        cannot be confirmed on GitHub is indeterminate: the count could otherwise be
        lost and an unbounded re-run become possible.
        """
        outcome = FinalOutcome()
        abort = self.abort
        if abort is not None and not abort.started:
            return self._finalize_not_started(abort, outcome)
        if self.reserved is None:
            if abort is not None:
                outcome.failures.append(abort.failure)
                outcome.status = abort.status
            return outcome
        in_cleanup = abort is not None or self.scope.expired()
        try:
            if in_cleanup:
                with self.scope.cleanup_phase():
                    self._record_result(abort, normal_success, outcome)
            else:
                self._record_result(abort, normal_success, outcome)
        except (EventWriteUnconfirmed, ExecutionDeadlineExceeded) as error:
            failure = self.make_failure(
                ExecutionFailureCause.SIDE_EFFECT_INDETERMINATE,
                "record-result",
                side_effect_state=SIDE_EFFECT_UNKNOWN,
                detail=f"the attempt result was not confirmed on GitHub: {error}",
            )
            outcome.failures.append(failure)
            outcome.status = STATUS_EXECUTION_INDETERMINATE
            self.hold(failure.detail)
            self.escalate(failure.detail, outcome.failures)
        return outcome

    def _finalize_not_started(
        self, abort: IntegrationExecutionAbort, outcome: FinalOutcome
    ) -> FinalOutcome:
        outcome.failures.append(abort.failure)
        outcome.status = abort.status
        if self.budget is not None and self.budget.verdict in {
            BudgetVerdict.EXHAUSTED,
            BudgetVerdict.HOLD,
        }:
            with self.scope.cleanup_phase():
                self._retry_terminal_and_escalation(abort)
        return outcome

    def _retry_terminal_and_escalation(self, abort: IntegrationExecutionAbort) -> None:
        """Retry only the label / notification (and a missing terminal event)."""
        state = self.budget
        if (
            state is not None
            and state.verdict is BudgetVerdict.EXHAUSTED
            and not state.terminal_recorded
        ):
            try:
                self.store.terminal_for_history(state)
            except (EventWriteUnconfirmed, ExecutionDeadlineExceeded) as error:
                print(f"Warning: terminal event not saved: {error}", file=sys.stderr)
        self.escalate(abort.failure.detail, [abort.failure])

    def _record_result(
        self,
        abort: IntegrationExecutionAbort | None,
        normal_success: bool,
        outcome: FinalOutcome,
    ) -> None:
        reserved = self.reserved
        assert reserved is not None
        if abort is None:
            self.store.finish(
                reserved,
                OUTCOME_SUCCESS if normal_success else OUTCOME_FAILED,
                targets=self.targets,
                stage=None,
                stop_confirmed=True,
                rollback_confirmed=True,
                side_effect_state=SIDE_EFFECT_NONE,
            )
            return
        outcome.failures.append(abort.failure)
        self._record_failure_result(reserved, abort.failure, outcome)

    def _record_failure_result(
        self,
        reserved: ExecutionEvent,
        failure: ExecutionFailure,
        outcome: FinalOutcome,
    ) -> None:
        """Save a failed attempt and decide: retry later, terminal, or a human."""
        counted = failure.cause in COUNTED_TIMEOUT_CAUSES and failure.confirmed_safe
        retry_allowed, next_at = (False, None)
        if counted:
            timeouts_before = 0 if self.budget is None else self.budget.timeouts
            retry_allowed, next_at = planned_retry(
                self.policy, timeouts_before, now=self.store.clock()
            )
        failure.next_retry_at = next_at
        self.store.finish(
            reserved,
            failure.cause.value,
            targets=self.targets,
            stage=failure.stage,
            stop_confirmed=failure.stop_confirmed,
            rollback_confirmed=failure.rollback_confirmed,
            side_effect_state=failure.side_effect_state,
            next_retry_at=next_at,
        )
        if counted and retry_allowed:
            outcome.status = STATUS_EXECUTION_TIMED_OUT
        elif counted:
            self.store.terminal(
                reserved,
                outcome=ExecutionFailureCause.RETRY_BUDGET_EXHAUSTED.value,
                targets=self.targets,
            )
            outcome.status = STATUS_EXECUTION_RETRY_EXHAUSTED
            self.escalate("the integration timeout retry limit was reached", [failure])
        else:
            outcome.status = (
                STATUS_EXECUTION_CLEANUP_FAILED
                if failure.cause is ExecutionFailureCause.CLEANUP_FAILED
                else STATUS_EXECUTION_INDETERMINATE
            )
            self.hold(failure.detail or failure.cause.value)
            self.escalate(failure.detail or failure.cause.value, [failure])


def make_failure(
    state: ExecutionState | None,
    cause: ExecutionFailureCause,
    stage: str,
    *,
    target: Target | None = None,
    **fields: Any,
) -> ExecutionFailure:
    """Build a failure with the state's attempt context, or standalone without one."""
    if state is not None:
        return state.make_failure(cause, stage, target=target, **fields)
    return ExecutionFailure(
        cause=cause,
        stage=stage,
        parent_issue_number=0,
        issue_number=None if target is None else target.issue_number,
        subtask_id=None if target is None else target.subtask_id,
        source_sha=None if target is None else target.source_sha,
        **fields,
    )


def provisional_status(abort: IntegrationExecutionAbort) -> str:
    """The status to report before the attempt's result has been recorded."""
    if abort.status is not None:
        return abort.status
    failure = abort.failure
    if failure.cause in COUNTED_TIMEOUT_CAUSES and failure.confirmed_safe:
        return STATUS_EXECUTION_TIMED_OUT
    if failure.cause is ExecutionFailureCause.CLEANUP_FAILED:
        return STATUS_EXECUTION_CLEANUP_FAILED
    return STATUS_EXECUTION_INDETERMINATE


def require_bounded_forge(forge: object) -> str | None:
    """An error message when ``forge`` cannot be proven to bound its calls."""
    if forge_supports_bounded_execution(forge):
        return None
    return (
        "The injected Forge does not declare bounded execution "
        "(supports_bounded_execution); refusing to apply without a deadline guarantee"
    )


__all__ = [
    "OUTPUT_TAIL_CHARS",
    "ExecutionFailure",
    "ExecutionState",
    "FinalOutcome",
    "IntegrationExecutionAbort",
    "IssueCommentForge",
    "make_failure",
    "parent_execution_lock_path",
    "provisional_status",
    "require_bounded_forge",
]
