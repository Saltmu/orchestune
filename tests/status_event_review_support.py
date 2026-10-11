"""Production drivers for the not-needed review-pending interval (#1278).

A not-needed completion that production holds for an independent review
(`not-needed-review` policy) is not complete until that review passes, although
it already carries `status:not-needed`.  The expectations here come from the
issue's timeline table, not from the model: the expected `Kind` of each input
is fixed per case, and the production policy selection, the dependency guard and
`CycleContext.is_effectively_done` are observed separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from orchestune.complete.contracts import CompleteRequest, CompletionLabelStatus
from orchestune.complete.not_needed_policy import not_needed_policies
from orchestune.complete.service import complete_task
from orchestune.complete.status_labels import transition_completion_status_label
from orchestune.dispatch.gc.policies import process_completion_policies
from orchestune.infra.process_utils import run_state_lock
from orchestune.labels import StatusLabel
from orchestune.ledger.completion_reservations import (
    completion_record,
    dependency_completion_blocked,
)
from orchestune.ledger.run_state import (
    RunState,
    load_run_state_readonly,
    save_run_state,
)
from orchestune.ledger.status_events import (
    Applied,
    EventInput,
    TaskModel,
    apply_event,
    restart,
)
from orchestune.targets.contracts import DispatchHandle
from tests.complete_lifecycle_test_support import unclaimed_environment
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_dispatcher_config,
    make_test_task,
)
from tests.status_event_test_support import (
    CaseEnv,
    Observation,
    production_limits,
    route_for,
)
from tests.status_transition_callsite_drivers import ISSUE
from tests.verification_contract_test_support import require

COMPLETION_SOURCE = "complete/status_labels.py::_completion_mutate"
UNCLAIMED_CASE = "not-needed-review-unclaimed"
LABELS_SOURCE = "dispatch/gc/policy_effects.py::reconcile_labels"
REVIEW_SOURCE = "dispatch/gc/policy_review.py::reconcile_review"
LIMITS = production_limits()
_N = StatusLabel.NOT_NEEDED
_Q = StatusLabel.QUEUED
_TIMEOUT_SECONDS = 10


def _status_labels(labels: Any) -> frozenset[str]:
    return frozenset(label for label in labels if label.startswith("status:"))


def completion_not_needed(env: CaseEnv) -> tuple[Observation, ...]:
    """Write the not-needed label, then observe which downstream policy production made.

    `completion` is the dependency-visible completion: the label is present and
    no `not-needed-review` policy is left to hold it.  A fault that drops the
    production policy therefore changes this observation, not the expected Kind.
    """
    kind = env.params["active"]
    active = (
        None
        if kind is None
        else make_test_active_worktree(
            ISSUE, external_id="routine-1" if kind == "cloud" else None
        )
    )
    request = CompleteRequest.not_needed(
        ISSUE, state_path=env.tmp_path / "run_state.json", worktree_root=env.tmp_path
    )
    policies = not_needed_policies(request, active, "repo", "generation", "cid", {})
    result = transition_completion_status_label(
        env.forge, ISSUE, _N, generation_matches=lambda: True
    )
    assert result.status is CompletionLabelStatus.CONFIRMED
    labels = frozenset(env.forge.get_issue_labels(ISSUE))
    held = any(p.policy_kind == "not-needed-review" for p in policies)
    return (Observation(labels, "applied", completion=_N in labels and not held),)


@dataclass(frozen=True)
class Stage:
    """What production shows at one point of the sequence."""

    labels: frozenset[str]
    done: bool
    blocked: bool
    policy: str | None


class LabeledReviewWorld:
    """`complete_task` and the policy runner over a worktree-free ledger."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.request, self.forge = unclaimed_environment(
            tmp_path, monkeypatch, issue=ISSUE
        )
        self.config = make_test_dispatcher_config(
            tmp_path,
            forge=self.forge,
            apply=True,
            not_needed_review_timeout_seconds=_TIMEOUT_SECONDS,
        )
        self.path = self.request.state_path
        self.config.dispatch_target = Mock()
        self.config.dispatch_target.fire_text.return_value = DispatchHandle(
            external_id="review-session"
        )

    def complete(self) -> None:
        result = complete_task(self.request, forge=self.forge)
        assert result.success, result.failure

    def run_policies(self, now: float) -> None:
        state = load_run_state_readonly(self.path)
        process_completion_policies(state, self.config, now=now, repository_id="repo")

    def post_verdict(self, verdict: str) -> None:
        record = completion_record(load_run_state_readonly(self.path), ISSUE)
        assert record is not None
        (policy,) = record["downstream_policy_records"].values()
        operation = policy["metadata"]["operation_id"]
        self.forge.add_comment(
            ISSUE, f"<!-- orchestune:policy-review {operation} verdict={verdict} -->"
        )

    def lose_ledger(self) -> None:
        with run_state_lock(self.path.with_suffix(".lock")):
            save_run_state(RunState(), self.path)

    def observe(self) -> Stage:
        state = load_run_state_readonly(self.path)
        labels = tuple(self.forge.get_issue_labels(ISSUE))
        ctx = make_test_cycle_context(
            tasks_by_issue={ISSUE: make_test_task(ISSUE, status_labels=labels)},
            run_state=state,
            config=self.config,
        )
        record = completion_record(state, ISSUE)
        policies = {} if record is None else record["downstream_policy_records"]
        statuses = {p["status"] for p in policies.values()}
        # The dependency guard and the cycle's completion are observed separately.
        assert dependency_completion_blocked(state, ISSUE) == ctx.is_completion_blocked(
            ISSUE
        )
        return Stage(
            _status_labels(labels),
            ctx.is_effectively_done(ISSUE),
            ctx.is_completion_blocked(ISSUE),
            ", ".join(sorted(statuses)) or None,
        )


def _model_step(state: TaskModel, source: str, condition: str) -> TaskModel:
    route = route_for(source, condition)
    result = apply_event(state, EventInput(route.event, route.kind), LIMITS)
    assert isinstance(result, Applied), result
    return result.state


def _agree(
    where: str, stage: Stage, model: TaskModel, blocked: bool, policy: Any
) -> None:
    contract = "P3A-DYNAMIC-CONFORMANCE"
    require(contract, stage.labels == model.labels, f"{where}: labels")
    require(
        contract,
        stage.done == model.completion_done,
        f"{where}: production done={stage.done} vs model {model.completion_done}",
    )
    require(contract, stage.blocked == blocked, f"{where}: dependency guard")
    require(contract, stage.policy == policy, f"{where}: policy {stage.policy}")


#: verdict -> (verdict source, condition, labels left, dependency guard, policy)
_VERDICTS = {
    "passed": (REVIEW_SOURCE, "review-passed", {_N}, False, "applied"),
    "failed": (LABELS_SOURCE, "review-rejected", {_Q}, True, "pending"),
    "timeout": (LABELS_SOURCE, "review-launch-timeout", None, True, "pending"),
}


def check_labeled_review_sequence(
    verdict: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loss_at: str | None = None,
) -> None:
    """queued -> complete -> review launched -> verdict (-> ledger loss).

    Every step compares the production completion with the model's, and the
    expected dependency guard / policy state is fixed by the issue's timeline.
    """
    world = LabeledReviewWorld(tmp_path, monkeypatch)
    model = TaskModel.from_labels((_Q,))
    _agree("start", world.observe(), model, False, None)

    world.complete()
    model = _model_step(model, COMPLETION_SOURCE, UNCLAIMED_CASE)
    _agree("completed", world.observe(), model, True, "pending")

    world.run_policies(100)
    _agree("review launched", world.observe(), model, True, "pending")
    if loss_at == "pending":
        world.lose_ledger()
        model = restart(model, ledger_loss=True)
        _agree("ledger lost while pending", world.observe(), model, False, None)
        return

    source, condition, _left, blocked, policy = _VERDICTS[verdict]
    if verdict == "timeout":
        world.run_policies(100 + _TIMEOUT_SECONDS + 1)
    else:
        world.post_verdict(verdict)
        world.run_policies(101)
    model = _model_step(model, source, condition)
    _agree(f"review {verdict}", world.observe(), model, blocked, policy)
    if loss_at == "resolved":
        world.lose_ledger()
        model = restart(model, ledger_loss=True)
        _agree("ledger lost after the verdict", world.observe(), model, False, None)


__all__ = [
    "COMPLETION_SOURCE",
    "UNCLAIMED_CASE",
    "check_labeled_review_sequence",
    "completion_not_needed",
]
