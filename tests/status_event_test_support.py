"""Production adapters, the Event correspondence table and execution drivers (#1264).

`EVENT_BY_SOURCE` maps every production label path - each `CALL_SITES` key, each
`OUT_OF_SCOPE_PATHS` entry, the three label-invariant completion paths and the
label-invariant budget path - to the Event routes it can produce. A route names the executed cases that cover
it, so a source or branch without an executed case fails the build.

The drivers below run the real production functions against an in-memory forge.
Only collaborators outside the label decision (worktree removal, process
liveness, external-execution holds, evidence lookups that need git or the
network) are stubbed; forge mutations are never replaced by a mock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestune.complete.contracts import CompletionLabelStatus, DownstreamPolicyRecord
from orchestune.complete.status_labels import transition_completion_status_label
from orchestune.consistency.repairs.status import (
    COMMAND_ADD_LABEL,
    COMMAND_REMOVE_LABEL,
    plan_status_repairs,
)
from orchestune.dependencies.resolution import build_legacy_dag_inputs
from orchestune.dispatch import gc as dispatch_gc
from orchestune.dispatch import (
    launch,
    phase_rebase,
    prior_parent_merge,
    rebase,
    reconciliation,
    status_repair,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc import completion, policies, policy_review, zombies
from orchestune.dispatch.gc.policy_effects import apply_effects
from orchestune.dispatch.retry_policy import (
    RetryDisposition,
    RetryState,
    early_death_policy,
    plan_retry,
    review_timeout_policy,
)
from orchestune.dispatch.rules import _RuleExecutionContext
from orchestune.integrator import pr as integrator_pr
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState, TaskReclaimRecord
from orchestune.ledger.status_events import (
    BackoffState,
    BudgetCounts,
    BudgetLimits,
    Event,
    Kind,
    ReclaimState,
    RetryStates,
)
from orchestune.lock_contracts import ExternalLockScanResult
from orchestune.outcome_record import RESULT_NOT_NEEDED, OutcomeLookupState
from orchestune.replan import operations as replan_operations
from orchestune.targets.cloud_routine import ClaudeCodeCloudRoutineDispatchTarget
from tests.conftest import FakeForge, make_issue
from tests.consistency_status_test_support import (
    _desired,
    _desired_task,
    _evaluate,
    _observed,
    _task_scope,
)
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_dispatcher_config,
    make_test_task,
)
from tests.status_transition_callsite_drivers import (
    _NOOP,
    ISSUE,
    Env,
    _active,
    _fake,
    _reconciliation_ctx,
    _stub_completion,
    prior_parent_normalize_closed,
)

NOW = 1_700_000_000.0
#: `attempt >= 3` is a literal in the production base-branch-red paths
#: (`dispatch/gc/policies.py::_prepare`, `dispatch/gc/outcome_decision.py`).
BASE_BRANCH_RED_ATTEMPTS = 3


def production_backoff_planner(config: DispatcherConfig) -> Any:
    """The model's backoff decision, delegated to `retry_policy.plan_retry`."""

    def plan(kind: Kind, state: BackoffState, now: float) -> BackoffState | None:
        policy = (
            early_death_policy(
                config.max_early_death_retries, config.early_death_backoff_seconds
            )
            if kind is Kind.EARLY_DEATH
            else review_timeout_policy(
                config.max_review_timeout_retries,
                config.review_timeout_backoff_seconds,
            )
        )
        current = RetryState(state.count, state.retry_at, state.pending)
        planned = plan_retry(policy, current, now=now)
        if planned.disposition is RetryDisposition.EXHAUSTED:
            return None
        return BackoffState(
            planned.state.count, planned.state.retry_at, planned.state.pending
        )

    return plan


def limits_from_config(config: DispatcherConfig) -> BudgetLimits:
    return BudgetLimits(
        max_task_reclaims=config.max_task_reclaims,
        max_recompute_retries=config.max_recompute_retries,
        base_branch_red_attempts=BASE_BRANCH_RED_ATTEMPTS,
        plan_backoff=production_backoff_planner(config),
    )


def production_limits(state_root: Path | None = None, **overrides: Any) -> BudgetLimits:
    """Limits built from a `DispatcherConfig` with the given field overrides."""
    return limits_from_config(make_test_dispatcher_config(state_root, **overrides))


@dataclass(frozen=True)
class Route:
    """One Event a source can produce, and the executed cases that cover it."""

    event: Event
    kind: Kind = Kind.PLAIN
    cases: tuple[str, ...] = ()


#: Label paths that confirm a completion without a lifecycle label transition:
#: (file, qualified function, how the completion is evidenced).
LABEL_INVARIANT_COMPLETIONS: tuple[tuple[str, str, str], ...] = (
    (
        "dispatch/cycle_context_state.py",
        "_CycleState.record_completion",
        "same-cycle completion; Forge labels stay as observed",
    ),
    (
        "dispatch/prior_parent_merge.py",
        "reconcile_prior_parent_merges",
        "verified prior parent merge in a dry run (`completed_issue_numbers`)",
    ),
    (
        "dispatch/gc/__init__.py",
        "_rule_not_needed",
        "not-needed Outcome record without the not-needed label",
    ),
    (
        "dispatch/gc/policy_review.py",
        "reconcile_review",
        "passed independent review closes the issue; `_apply_policy` then marks "
        "the `not-needed-review` policy applied",
    ),
)

#: Budget paths that persist a count without any label change.
LABEL_INVARIANT_BUDGETS: tuple[tuple[str, str, str], ...] = (
    (
        "dispatch/rebase.py",
        "_apply_recomputed_event",
        "within-budget recompute; persists `recompute_count`, labels stay as observed",
    ),
)

_E, _K = Event, Kind


def _r(event: Event, kind: Kind, *cases: str) -> Route:
    return Route(event, kind, cases)


#: Every production label path -> the Events it produces. CALL_SITES routes name
#: the conditions of `tests/test_status_transition_callsites.py::CASES`; the
#: other cases are `EVENT_CASES` below.
EVENT_BY_SOURCE: dict[str, tuple[Route, ...]] = {
    "claim/service.py::_apply_status_label": (
        _r(
            _E.LAUNCH,
            _K.CLAIM,
            "queued",
            "blocked",
            "aux-force-serial",
            "aux-recompute-lock",
            "no-lifecycle",
            "replay",
            "queued-and-blocked",
        ),
    ),
    "dispatch/launch.py::_apply_yaml_error_blocking": (
        _r(_E.BLOCK, _K.PLAIN, "queued", "replay", "stale-both"),
    ),
    "dispatch/launch.py::_apply_invalid_footprint_blocking": (
        _r(_E.ESCALATE, _K.PLAIN, "queued", "replay"),
    ),
    "dispatch/launch.py::_handle_launch_failure": (
        _r(
            _E.BLOCK,
            _K.PLAIN,
            "claim-failed",
            "agent-failed",
            "blocked-again",
            "aux-recompute",
        ),
        _r(_E.ESCALATE, _K.PLAIN, "invalid-after-claim", "invalid"),
    ),
    "dispatch/launch.py::_record_successful_launch": (
        _r(
            _E.LAUNCH,
            _K.PLAIN,
            "queued",
            "blocked",
            "queued-and-blocked",
            "replay",
            "confirms-reservations",
        ),
    ),
    "dispatch/launch_attempts.py::reconcile_attempt": (
        _r(
            _E.LAUNCH,
            _K.RECOVERY,
            "queued",
            "blocked",
            "replay",
            "queued-and-blocked",
            "no-lifecycle",
            "aux-force-serial-kept",
        ),
    ),
    "dispatch/reconciliation.py::_resolve_one_blocked_recompute_issue": (
        _r(_E.QUEUE, _K.RECOMPUTE, "aux-recompute-removed"),
    ),
    "dispatch/reconciliation.py::_release_recompute_for_promotion": (
        _r(_E.RELEASE_HOLD, _K.RECOMPUTE, "pending-dependencies"),
    ),
    "dispatch/reconciliation.py::_apply_base_branch_red_requeue": (
        _r(_E.QUEUE, _K.BASE_BRANCH_RED, "blocked", "replay"),
    ),
    "dispatch/reconciliation.py::_apply_base_branch_red_unmark": (
        _r(_E.RELEASE_HOLD, _K.BASE_BRANCH_RED, "unmark"),
    ),
    "dispatch/reconciliation.py::_apply_base_branch_red_escalate": (
        _r(_E.BLOCK, _K.BASE_BRANCH_RED, "third-attempt"),
    ),
    "dispatch/rebase.py::notify_recompute": (
        _r(_E.BLOCK, _K.RECOMPUTE, "aux-added-directly", "aux-replay"),
    ),
    "dispatch/rebase.py::_apply_forced_serial_event": (
        _r(_E.RECOMPUTE, _K.PLAIN, "over-budget"),
    ),
    "dispatch/rebase.py::_apply_recomputed_event": (
        _r(_E.RECOMPUTE, _K.PLAIN, "within-budget", "last-within-budget"),
    ),
    "dispatch/rebase.py::_prepare_wip_backup_for_rebase": (
        _r(_E.ESCALATE, _K.MANUAL_MERGE, "in-progress", "replay"),
    ),
    "dispatch/rebase.py::_handle_rebase_failure": (
        _r(_E.ESCALATE, _K.MANUAL_MERGE, "in-progress", "replay"),
    ),
    "dispatch/phase_rebase.py::_apply_external_lock_sync": (
        _r(_E.HOLD, _K.EXTERNAL_LOCK, "lock"),
        _r(_E.QUEUE, _K.EXTERNAL_LOCK, "unlock-queued", "unlock-restores-queued"),
        _r(_E.RELEASE_HOLD, _K.EXTERNAL_LOCK, "unlock-blocked"),
    ),
    "dispatch/gc/zombies.py::_notify_requeued_reclaim": (
        _r(
            _E.RECLAIM,
            _K.PLAIN,
            "in-progress",
            "blocked",
            "both",
            "stale-snapshot",
            "first-reclaim",
            "pending-reclaim-resumed",
        ),
    ),
    "dispatch/prior_parent_merge.py::_apply_verified_repair": (
        _r(
            _E.COMPLETE,
            _K.PLAIN,
            "queued",
            "blocked",
            "in-progress",
            "queued-and-blocked",
            "replay",
        ),
    ),
    "dispatch/prior_parent_merge.py::_normalize_closed_issue_label": (
        _r(_E.COMPLETE, _K.PLAIN, "queued", "no-lifecycle", "terminal-done-cleanup"),
        _r(_E.NOT_NEEDED, _K.PLAIN, "terminal-not-needed-cleanup"),
    ),
    "dispatch/prior_parent_merge.py::reconcile_prior_parent_merges": (
        _r(_E.COMPLETE_WITHOUT_LABEL, _K.PRIOR_MERGE, "dry-run"),
    ),
    "dispatch/gc/completion.py::_apply_blocked_hold": (
        _r(_E.BLOCK, _K.PLAIN, "in-progress", "queued"),
        _r(_E.BLOCK, _K.RECOMPUTE, "aux-added-directly"),
    ),
    "dispatch/gc/completion.py::_publish_requeue": (
        _r(
            _E.REQUEUE,
            _K.EARLY_DEATH,
            "in-progress",
            "blocked",
            "replay",
            "early-death-retry",
        ),
        _r(_E.REQUEUE, _K.REVIEW_TIMEOUT, "review-timeout-retry"),
    ),
    "dispatch/gc/completion.py::_apply_done_worktree_cleanup": (
        _r(_E.COMPLETE, _K.PLAIN, "in-progress", "both"),
    ),
    "dispatch/gc/completion.py::_apply_escalated_base_branch_red": (
        _r(_E.BLOCK, _K.BASE_BRANCH_RED, "third-attempt"),
    ),
    "dispatch/gc/completion.py::_finalize_not_needed_worktree": (
        _r(_E.NOT_NEEDED, _K.PLAIN, "labelled"),
        _r(_E.NOT_NEEDED, _K.PLAIN, "outcome-only", "in-progress"),
        _r(_E.AWAIT_REVIEW, _K.PLAIN, "cloud-review-dispatched"),
    ),
    "dispatch/gc/cloud_completion.py::_handle_abandoned_cloud_reclaim": (
        _r(_E.RECLAIM, _K.PLAIN, "in-progress", "blocked"),
    ),
    "dispatch/gc/policy_effects.py::reconcile_labels": (
        _r(
            _E.REQUEUE,
            _K.REVIEW_TIMEOUT,
            "review-timeout-requeue",
            "review-timeout-exhausted",
        ),
        _r(_E.BLOCK, _K.BASE_BRANCH_RED, "base-red-hold", "base-red-escalate"),
        _r(_E.REVIEW_REJECT, _K.PLAIN, "review-rejected", "cloud-review-rejected"),
        _r(_E.ESCALATE, _K.PLAIN, "review-launch-timeout", "cloud-review-timeout"),
    ),
    "dispatch/gc/policy_review.py::reconcile_review": (
        _r(
            _E.COMPLETE_WITHOUT_LABEL,
            _K.REVIEW_PASSED,
            "cloud-review-passed",
            "review-passed",
        ),
    ),
    "dispatch/gc/__init__.py::_rule_not_needed": (
        _r(_E.NOT_NEEDED, _K.PLAIN, "outcome-only"),
        _r(_E.AWAIT_REVIEW, _K.PLAIN, "cloud-review-dispatched"),
    ),
    "dispatch/status_repair.py::_apply_command": (
        _r(
            _E.QUEUE,
            _K.PLAIN,
            "dependency-resolved",
            "add-missing-queued",
            "remove-conflicting-blocked",
        ),
        _r(_E.BLOCK, _K.PLAIN, "dependency-unresolved", "add-missing-blocked"),
        _r(_E.MERGE_REVERT, _K.PLAIN, "interrupted-rollback"),
        _r(_E.COMPLETE, _K.PLAIN, "remove-stale-queued-from-done"),
        _r(_E.REQUEUE, _K.RECOVERY, "remove-stale-in-progress"),
    ),
    "dispatch/recovery.py::execute_recovery_requeue_command": (
        _r(
            _E.REQUEUE,
            _K.RECOVERY,
            "in-progress",
            "also-blocked",
            "aux-force-serial-kept",
        ),
    ),
    "dispatch/cycle_context_state.py::_CycleState.record_completion": (
        _r(
            _E.COMPLETE_WITHOUT_LABEL,
            _K.CYCLE,
            "in-progress",
            "duplicate",
            "already-done",
        ),
    ),
    "complete/status_labels.py::_completion_mutate": (
        _r(
            _E.COMPLETE,
            _K.COMPLETION,
            "done-from-in-progress",
            "done-keeps-force-serial",
            "done-repair",
            "done-replay",
            "done-escalated-conflict",
            "done-auxiliary-conflict",
            "done-stale-generation",
            "done-remove-fails-then-retried",
        ),
        _r(
            _E.NOT_NEEDED,
            _K.COMPLETION,
            "not-needed-from-in-progress",
            "not-needed-unknown-status-conflict",
        ),
        _r(_E.BLOCK, _K.COMPLETION, "blocked-from-in-progress"),
    ),
    "replan/operations.py::_transition_to_not_needed": (
        _r(
            _E.NOT_NEEDED,
            _K.REPLAN,
            "queued",
            "blocked-with-recompute",
            "in-progress-force-serial",
            "escalated",
            "replay",
        ),
    ),
    "integrator/pr.py::handle_merge_failure": (
        _r(
            _E.MERGE_REVERT,
            _K.PLAIN,
            "done",
            "partial-rollback",
            "add-fails-then-retried",
            "remove-fails-then-retried",
        ),
    ),
    "ledger/escalation.py::apply_human_review_escalation": (
        _r(
            _E.ESCALATE,
            _K.PLAIN,
            "in-progress",
            "queued",
            "blocked",
            "not-needed-timeout",
            "in-progress-and-queued",
            "replay",
            "no-lifecycle",
        ),
        _r(_E.RECLAIM, _K.PLAIN, "reclaim-over-budget"),
        _r(_E.REQUEUE, _K.EARLY_DEATH, "early-death-exhausted"),
    ),
    "integrator/steps.py::AutoMergeChildIntegrationStep._restore_blocked_label": (
        _r(_E.ESCALATE, _K.PLAIN, "in-progress", "queued-and-blocked"),
    ),
}


def route_for(source: str, condition: str) -> Route:
    """The single route of `source` that names `condition`."""
    matches = [r for r in EVENT_BY_SOURCE[source] if condition in r.cases]
    assert len(matches) == 1, (source, condition, matches)
    return matches[0]


# ---- observations and the fault-injecting forge ----------------------------


@dataclass(frozen=True)
class Observation:
    """What one production attempt left behind. `None` fields are not compared."""

    labels: frozenset[str]
    result: str | None = None
    completion: bool | None = None
    execution_active: bool | None = None
    retries: RetryStates | None = None
    counts: BudgetCounts | None = None


class FaultyForge(FakeForge):
    """FakeForge whose next add/remove can fail before or after its effect."""

    def __init__(self) -> None:
        super().__init__()
        self.armed: tuple[str, str] | None = None

    def arm(self, op: str, mode: str) -> None:
        self.armed = (op, mode)

    def _mutate(self, op: str, apply: Callable[[], None]) -> None:
        mode = None
        if self.armed is not None and self.armed[0] == op:
            mode, self.armed = self.armed[1], None
        if mode == "before":
            raise OSError(f"injected failure before {op}")
        apply()
        if mode == "after":
            raise OSError(f"injected lost response after {op}")

    def add_label(
        self, issue_number: int | str, label: str, actor: str = "bot"
    ) -> None:
        self._mutate(
            "add",
            lambda: super(FaultyForge, self).add_label(issue_number, label, actor),
        )

    def remove_label(self, issue_number: int | str, label: str) -> None:
        self._mutate(
            "remove", lambda: super(FaultyForge, self).remove_label(issue_number, label)
        )

    def list_all_issue_comments(self, issue_number: int | str) -> list[dict[str, Any]]:
        return self.list_comments(issue_number) + super().list_all_issue_comments(
            issue_number
        )


@dataclass
class CaseEnv:
    forge: FaultyForge
    config: DispatcherConfig
    monkeypatch: pytest.MonkeyPatch
    tmp_path: Path
    held: tuple[str, ...]
    params: Mapping[str, Any]


CaseDriver = Callable[[CaseEnv], tuple[Observation, ...]]


def _labels(env: CaseEnv) -> frozenset[str]:
    return frozenset(env.forge.get_issue_labels(ISSUE))


def _attempts(env: CaseEnv, run: Callable[[], str | None]) -> tuple[Observation, ...]:
    """Run `run` once per declared fault (one clean run by default)."""
    observed = []
    for fault in env.params.get("faults", (None,)):
        if fault is not None:
            env.forge.arm(*fault)
        try:
            result = run()
        except OSError:
            result = None
        observed.append(Observation(_labels(env), result))
    return tuple(observed)


def _ctx_env(env: CaseEnv) -> Env:
    return Env(
        env.forge, env.config, env.monkeypatch, env.tmp_path, env.held, env.params
    )


def _task_of(env: CaseEnv, **overrides: Any) -> Any:
    snapshot = env.params.get("snapshot", env.held)
    return make_test_task(ISSUE, status_labels=snapshot, **overrides)


def _record_result(status: Any) -> str:
    return {"applied": "applied", "noop": "noop", "conflict": "rejected"}[status.value]


# ---- drivers: direct Forge operations (OUT_OF_SCOPE_PATHS) ------------------


def completion_adapter(env: CaseEnv) -> tuple[Observation, ...]:
    statuses = {
        CompletionLabelStatus.CONFIRMED: "applied",
        CompletionLabelStatus.CONFLICT: "rejected",
    }

    def run() -> str | None:
        result = transition_completion_status_label(
            env.forge,
            ISSUE,
            env.params["target"],
            generation_matches=lambda: env.params.get("generation", True),
        )
        return statuses.get(result.status)

    return _attempts(env, run)


def _policy(kind: str, **metadata: Any) -> DownstreamPolicyRecord:
    return DownstreamPolicyRecord(
        repository_id="r",
        issue_number=ISSUE,
        generation_id="g",
        completion_id="c",
        policy_kind=kind,
        metadata={"operation_id": "op", **metadata},
    )


def review_timeout_policy_effect(env: CaseEnv) -> tuple[Observation, ...]:
    """`_reserve_retry` decides the target; `apply_effects` applies it."""
    count = env.params.get("count", 0)
    record = TaskReclaimRecord(review_timeout_retry_count=count)
    state = RunState(task_reclaim_counts={ISSUE: record})
    metadata = policies._reserve_retry(state, env.config, ISSUE, NOW)
    apply_effects(env.forge, ISSUE, _policy("review-timeout", **metadata))
    reserved = state.task_reclaim_counts[ISSUE]
    retry = BackoffState(
        reserved.review_timeout_retry_count,
        reserved.review_timeout_retry_at,
        # `_apply_policy` confirms the reservation once the effects are applied.
        False,
    )
    return (Observation(_labels(env), retries=RetryStates(review_timeout=retry)),)


def base_branch_red_policy_effect(env: CaseEnv) -> tuple[Observation, ...]:
    """The attempt -> target rule restates `policies._prepare` (attempt >= 3)."""
    attempt = env.params["attempt"]
    target = StatusLabel.BLOCKED_HUMAN_REVIEW if attempt >= 3 else StatusLabel.BLOCKED
    apply_effects(
        env.forge,
        ISSUE,
        _policy("base-branch-red", attempt=attempt, target_label=target),
    )
    return (Observation(_labels(env)),)


def not_needed_review_verdict(env: CaseEnv) -> tuple[Observation, ...]:
    saved: dict[str, Any] = {}
    metadata: dict[str, Any] = {"launch_state": "launched"}
    if env.params.get("verdict"):
        env.forge.add_comment(
            ISSUE,
            f"<!-- orchestune:policy-review op verdict={env.params['verdict']} -->",
        )
    else:
        metadata |= {"launch_requested_at": 0.0, "review_timeout_seconds": 10}
    # `_apply_policy` marks the policy applied (the dependency gate) iff it is True.
    applied = policy_review.reconcile_review(
        env.forge,
        ISSUE,
        _policy("not-needed-review", **metadata),
        None,
        saved.update,
        100.0,
    )
    return (Observation(_labels(env), completion=applied),)


def replan_retire(env: CaseEnv) -> tuple[Observation, ...]:
    replan_operations._transition_to_not_needed(env.forge, ISSUE)
    return (Observation(_labels(env)),)


def merge_failure(env: CaseEnv) -> tuple[Observation, ...]:
    def run() -> str | None:
        integrator_pr.handle_merge_failure(
            make_test_task(ISSUE), "ci failed", apply=True, forge=env.forge
        )
        return None

    return _attempts(env, run)


def status_repair_add_remove(env: CaseEnv) -> tuple[Observation, ...]:
    """Apply the add/remove commands the real status-repair planner generates."""
    report = _evaluate(
        _observed(_task_scope(ISSUE, labels=env.held)),
        _desired(_desired_task("status-policy", ISSUE, **env.params["desired"])),
    )
    commands = [
        command
        for command in plan_status_repairs(report)
        if command.code in {COMMAND_ADD_LABEL, COMMAND_REMOVE_LABEL}
    ]
    assert commands, "the planner proposed no add/remove command"
    journal = MagicMock()
    for command in commands:
        status_repair._apply_command(
            command, _task_of(env), _fake(intent_id="i"), journal, env.config
        )
    assert journal.mark_applied.call_count == len(commands)
    return (Observation(_labels(env)),)


def footprint_deviation(env: CaseEnv) -> tuple[Observation, ...]:
    """`_decide_footprint_deviation_outcome` picks the branch that is then applied."""
    active = make_test_active_worktree(
        ISSUE, pid=None, recompute_count=env.params["count"]
    )
    task, deviated = _task_of(env), ["src/deviated.py"]
    decision = rebase._decide_footprint_deviation_outcome(
        active, deviated, {ISSUE: task}, env.config, build_legacy_dag_inputs((task,))
    )
    rebase._apply_footprint_deviation_outcome(
        active, deviated, decision, {}, env.config
    )
    counts = BudgetCounts(recompute=active.launch.recompute_count)
    return (Observation(_labels(env), counts=counts),)


def external_lock_sync(env: CaseEnv) -> tuple[Observation, ...]:
    env.monkeypatch.setattr(phase_rebase, "_notify_external_locks", _NOOP)
    task = _task_of(env)
    locking = env.params["lock"]
    scan = ExternalLockScanResult(
        to_lock=[task] if locking else [], to_unlock=[] if locking else [task]
    )
    phase_rebase._apply_external_lock_sync(scan, env.config)
    return (Observation(_labels(env)),)


def blocked_recompute_with_pending_dependencies(
    env: CaseEnv,
) -> tuple[Observation, ...]:
    task, ctx, state = _reconciliation_ctx(_ctx_env(env))
    issue = make_issue(ISSUE, labels=env.held, depends_on=("unresolved-dependency",))
    env.forge.issues[ISSUE] = issue
    result = reconciliation._resolve_one_blocked_recompute_issue(
        issue, task, set(), ctx, state, env.config
    )
    assert result is None
    return (Observation(_labels(env)),)


def base_branch_red_unmark(env: CaseEnv) -> tuple[Observation, ...]:
    decision = reconciliation.BaseBranchRedRecoveryDecision(
        ISSUE, "task-a", "unmark_only"
    )
    reconciliation._apply_base_branch_red_unmark(decision, env.config, "a", "b")
    return (Observation(_labels(env)),)


def base_branch_red_escalate(env: CaseEnv) -> tuple[Observation, ...]:
    decision = reconciliation.BaseBranchRedRecoveryDecision(
        ISSUE, "task-a", "escalate", attempt=3
    )
    reconciliation._apply_base_branch_red_escalate(decision, env.config)
    return (Observation(_labels(env)),)


def gc_escalated_base_branch_red(env: CaseEnv) -> tuple[Observation, ...]:
    _stub_completion(_ctx_env(env))
    env.monkeypatch.setattr(
        completion, "worktree_has_uncommitted_changes", lambda *a: False
    )
    ctx = _fake(active=_active(), config=env.config, active_task=_task_of(env))
    completion._apply_escalated_base_branch_red(ctx, _fake(outcome=None))
    return (Observation(_labels(env), execution_active=False),)


def prior_parent_normalize(env: CaseEnv) -> tuple[Observation, ...]:
    prior_parent_normalize_closed(_ctx_env(env))
    return (Observation(_labels(env)),)


def _completion_evidence(env: CaseEnv) -> bool:
    closed = env.forge.get_issue_state(ISSUE).upper() == "CLOSED"
    return closed or StatusLabel.NOT_NEEDED in _labels(env)


def _not_needed_review_dispatcher(env: CaseEnv) -> Callable[..., None] | None:
    """A cloud target defers the not-needed completion to an independent review."""
    if not env.params.get("cloud"):
        return None
    target = ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok")
    env.monkeypatch.setattr(env.config, "dispatch_target", target)
    return _NOOP


def finalize_not_needed(env: CaseEnv) -> tuple[Observation, ...]:
    _stub_completion(_ctx_env(env))
    env.monkeypatch.setattr(
        completion, "worktree_has_uncommitted_changes", lambda *a: False
    )
    review = _not_needed_review_dispatcher(env)
    event = completion._finalize_not_needed_worktree(
        _active(), _task_of(env), env.config, review
    )
    expected = "not_needed_review_dispatched" if review else "not_needed"
    assert event.action == expected, event
    return (Observation(_labels(env), completion=_completion_evidence(env)),)


# ---- drivers: label-invariant completions ----------------------------------


def cycle_record_completion(env: CaseEnv) -> tuple[Observation, ...]:
    actives = {str(ISSUE): _active()} if env.params.get("active", True) else {}
    ctx = make_test_cycle_context(
        tasks_by_issue={ISSUE: _task_of(env)},
        run_state=RunState(active_worktrees=actives),
        config=env.config,
    )
    observed = []
    for _ in range(env.params.get("calls", 1)):
        result = ctx.record_completion(ISSUE)
        observed.append(
            Observation(
                _labels(env),
                _record_result(result.status),
                completion=ctx.is_effectively_done(ISSUE),
                execution_active=ctx._is_in_progress(ISSUE),
            )
        )
    return tuple(observed)


def prior_merge_dry_run(env: CaseEnv) -> tuple[Observation, ...]:
    """The evidence scan (PR history and git reachability) is the stubbed input."""
    evidence = prior_parent_merge.PriorParentMergeEvidence(
        prior_parent_merge.PriorParentMergeStatus.ALREADY_MERGED,
        pr_number=9,
        base_ref="parent/issue-100",
    )
    issue = make_issue(ISSUE, labels=env.held)
    env.monkeypatch.setattr(
        prior_parent_merge,
        "inspect_prior_parent_merge",
        lambda *a, **k: (evidence, issue),
    )
    task = _task_of(env, parent_number=100)
    result = prior_parent_merge.reconcile_prior_parent_merges(
        env.forge, {ISSUE: task}, apply=False
    )
    return (
        Observation(_labels(env), completion=ISSUE in result.completed_issue_numbers),
    )


def rule_not_needed_outcome(env: CaseEnv) -> tuple[Observation, ...]:
    """Only the Outcome lookup (an Issue-comment fetch) is stubbed as FOUND."""
    _stub_completion(_ctx_env(env))
    env.monkeypatch.setattr(
        completion, "worktree_has_uncommitted_changes", lambda *a: False
    )
    found = _fake(
        state=OutcomeLookupState.FOUND, record=_fake(result=RESULT_NOT_NEEDED)
    )
    env.monkeypatch.setattr(dispatch_gc, "_fetch_outcome_for_active", lambda *a: found)
    env.monkeypatch.setattr(dispatch_gc, "fresh_external_hold", lambda *a, **k: None)
    key, active = str(ISSUE), _active()
    state = RunState(active_worktrees={key: active})
    ctx = _RuleExecutionContext(
        run_state=state,
        queries=_fake(record_completion=lambda _issue: None),
        config=env.config,
        not_needed_review_dispatcher=_not_needed_review_dispatcher(env),
    )
    outcome = dispatch_gc._rule_not_needed(ctx, key, active, _task_of(env))
    assert outcome is not None and outcome.terminal
    return (
        Observation(
            _labels(env),
            completion=_completion_evidence(env),
            execution_active=key in state.active_worktrees,
        ),
    )


# ---- drivers: production retry paths ---------------------------------------


def _reclaim_state(record: TaskReclaimRecord | None) -> RetryStates:
    if record is None:
        return RetryStates()
    return RetryStates(
        reclaim=ReclaimState(record.count, record.pending),
        early_death=BackoffState(
            record.early_death_retry_count,
            record.early_death_retry_at,
            record.early_death_retry_pending,
        ),
        review_timeout=BackoffState(
            record.review_timeout_retry_count,
            record.review_timeout_retry_at,
            record.review_timeout_retry_pending,
        ),
    )


def _ledger_record(retries: RetryStates) -> TaskReclaimRecord:
    return TaskReclaimRecord(
        count=retries.reclaim.count,
        pending=retries.reclaim.pending,
        early_death_retry_count=retries.early_death.count,
        early_death_retry_at=retries.early_death.retry_at,
        early_death_retry_pending=retries.early_death.pending,
        review_timeout_retry_count=retries.review_timeout.count,
        review_timeout_retry_at=retries.review_timeout.retry_at,
        review_timeout_retry_pending=retries.review_timeout.pending,
    )


def gc_reclaim(env: CaseEnv) -> tuple[Observation, ...]:
    """`_refresh_reclaim` resolves the count; `_reclaim_external_or_local` applies it."""
    env.monkeypatch.setattr(zombies, "fresh_external_hold", lambda *a, **k: None)
    env.monkeypatch.setattr(zombies, "save_run_state", _NOOP)
    key = str(ISSUE)
    active = make_test_active_worktree(
        ISSUE, pid=None, worktree_path=str(env.tmp_path / "missing")
    )
    retries = env.params.get("retries", RetryStates())
    counts = {ISSUE: _ledger_record(retries)} if retries != RetryStates() else {}
    state = RunState(active_worktrees={key: active}, task_reclaim_counts=counts)
    base = zombies.ZombieOrTimeoutReclaim(
        key=key,
        active=active,
        subtask_id="task-a",
        reason="zombie",
        finding_codes=(zombies.LOCAL_PROCESS_DEAD,),
        is_timeout=False,
        process_alive=False,
        status_labels=env.held,
    )
    precondition = _fake(
        active=active, timed_out=False, process_alive=False, observed_at=NOW
    )
    reclaim = zombies._refresh_reclaim(state, base, env.config, precondition)
    zombies._reclaim_external_or_local(state, reclaim, env.config, None)
    return (
        Observation(
            _labels(env),
            retries=_reclaim_state(state.task_reclaim_counts.get(ISSUE)),
            execution_active=key in state.active_worktrees,
        ),
    )


def gc_backoff_retry(env: CaseEnv) -> tuple[Observation, ...]:
    """`_handle_special_retry` with the GC's own settle callbacks.

    The exhausted early-death branch escalates but leaves releasing the active
    entry to its GC caller, so the execution state is observed only on requeue.
    """
    _stub_completion(_ctx_env(env))
    env.monkeypatch.setattr(dispatch_gc, "fresh_external_hold", lambda *a, **k: None)
    env.monkeypatch.setattr(dispatch_gc, "save_run_state", _NOOP)
    key = str(ISSUE)
    active = make_test_active_worktree(ISSUE, started_at=NOW - 10)
    retries = env.params.get("retries", RetryStates())
    state = RunState(
        active_worktrees={key: active},
        task_reclaim_counts={ISSUE: _ledger_record(retries)},
    )

    def settle(field_name: str) -> Callable[[], None]:
        return partial(
            dispatch_gc._settle_completion_requeue,
            state,
            env.config,
            key,
            ISSUE,
            field_name,
            (),
        )

    ctx = _fake(
        active=active,
        active_task=_task_of(env),
        config=env.config,
        run_state=state,
        now=NOW,
        open_prs=None,
        on_early_death_requeue=settle("early_death_retry_pending"),
        on_review_timeout_requeue=settle("review_timeout_retry_pending"),
    )
    early = env.params["kind"] == "early_death"
    action = "completed_no_commits" if early else "blocked_review_timeout"
    retried = completion._handle_special_retry(ctx, _fake(action=action))
    return (
        Observation(
            _labels(env),
            retries=_reclaim_state(state.task_reclaim_counts.get(ISSUE)),
            execution_active=(key in state.active_worktrees) if retried else None,
        ),
    )


def launch_confirms_reservations(env: CaseEnv) -> tuple[Observation, ...]:
    env.monkeypatch.setattr(
        launch, "_build_active_worktree_from_launch", lambda *a: _active()
    )
    env.monkeypatch.setattr(launch, "save_run_state", _NOOP)
    record = _ledger_record(env.params["retries"])
    state = RunState(active_worktrees={}, task_reclaim_counts={ISSUE: record})
    launch._record_successful_launch(
        _task_of(env), _fake(), _fake(), state, 1.0, env.config, None
    )
    return (
        Observation(
            _labels(env),
            retries=_reclaim_state(record),
            execution_active=str(ISSUE) in state.active_worktrees,
        ),
    )
