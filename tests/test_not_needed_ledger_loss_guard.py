"""Remote pending evidence must outlive the local completion journal (#1299)."""

from dataclasses import replace

import pytest

from orchestune.complete.service import complete_task
from orchestune.consistency.desired import TaskLifecycle
from orchestune.dependencies.assessment import DependencyState
from orchestune.dependencies.resolution import TaskDependencies
from orchestune.dispatch.status_repair_dependencies import task_lifecycle
from orchestune.ledger.run_state import RunState
from tests.complete_lifecycle_test_support import unclaimed_environment
from tests.dispatch_test_support import make_test_cycle_context, make_test_task
from tests.verification_contract_test_support import expect_violation, require

PENDING = "not-needed-review:pending"
CONTRACT = "P3A-NOT-NEEDED-LEDGER-LOSS-GUARD"


def _require_lost_ledger_hold(labels):
    ctx = make_test_cycle_context(
        tasks_by_issue={1111: make_test_task(1111, status_labels=tuple(labels))},
        run_state=RunState(),
        config=None,
        prior_parent_merge_completed_issue_numbers=frozenset({1111}),
    )
    require(
        "P3A-NOT-NEEDED-LEDGER-LOSS-GUARD",
        ctx.is_completion_blocked(1111),
        "remote pending blocks empty ledger",
    )
    require(
        "P3A-NOT-NEEDED-LEDGER-LOSS-GUARD",
        not ctx.is_effectively_done(1111),
        "override must not bypass review",
    )


def test_unclaimed_completion_keeps_dependency_waiting_after_ledger_loss(
    tmp_path, monkeypatch
):
    request, forge = unclaimed_environment(tmp_path, monkeypatch)
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert PENDING in forge.labels
    _require_lost_ledger_hold(forge.labels)
    assert forge.operations.index("add:" + PENDING) < forge.operations.index("post")
    labels = tuple(forge.labels)
    ctx = make_test_cycle_context(
        tasks_by_issue={
            1111: make_test_task(1111, subtask_id="upstream", status_labels=labels),
            1112: make_test_task(1112, depends_on=("upstream",)),
        },
        run_state=RunState(),
        dependency_resolution={1112: TaskDependencies(resolved=(1111,))},
        prior_parent_merge_completed_issue_numbers=frozenset({1111}),
    )
    assert ctx.is_completion_blocked(1111)
    assert not ctx.is_completion_confirmed(1111)
    assert not ctx.is_effectively_done(1111)
    assert ctx.assess_dependencies(1112).resolved[0].state is DependencyState.WAITING


@pytest.mark.parametrize("terminal", ["status:not-needed", "status:done"])
def test_remote_pending_precedes_completed_override(terminal):
    assert task_lifecycle((terminal, PENDING), completed=True) is TaskLifecycle.OPEN


def test_pending_add_failure_prevents_outcome_and_terminal_publication(
    tmp_path, monkeypatch
):
    request, forge = unclaimed_environment(tmp_path, monkeypatch)
    original = forge.add_label

    def add(issue, label):
        if label == PENDING:
            raise OSError("unavailable")
        original(issue, label)

    monkeypatch.setattr(forge, "add_label", add)
    failed = complete_task(request, forge=forge)
    assert not failed.success
    assert not forge.comments
    assert "status:not-needed" not in forge.labels
    monkeypatch.setattr(forge, "add_label", original)
    resumed = complete_task(
        replace(request, completion_id=failed.completion_id), forge=forge
    )
    assert resumed.success, resumed.failure
    assert len(forge.comments) == 1


@pytest.mark.parametrize("fault", ["publication", "reader"])
def test_control_missing_hold_is_detected(tmp_path, monkeypatch, fault):
    if fault == "publication":
        monkeypatch.setattr(
            "orchestune.complete.publication.ensure_review_pending", lambda *a: None
        )
    else:
        from orchestune.dispatch.cycle_context_state import _CycleState

        real = _CycleState._labels
        monkeypatch.setattr(
            _CycleState,
            "_labels",
            lambda self, issue: tuple(
                label for label in real(self, issue) if label != PENDING
            ),
        )
    request, forge = unclaimed_environment(tmp_path, monkeypatch)
    assert complete_task(request, forge=forge).success
    with expect_violation("P3A-NOT-NEEDED-LEDGER-LOSS-GUARD"):
        _require_lost_ledger_hold(forge.labels)


@pytest.mark.parametrize("cloud", [False, True])
def test_claimed_policy_selection_is_fixed_in_outcome(tmp_path, monkeypatch, cloud):
    from orchestune.infra.process_utils import run_state_lock
    from orchestune.ledger.run_state import load_run_state_readonly, save_run_state
    from tests.complete_lifecycle_test_support import lifecycle_environment

    request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
    if cloud:
        state = load_run_state_readonly(request.state_path)
        active = state.active_worktrees["1110"]
        state.active_worktrees["1110"] = replace(
            active, launch=replace(active.launch, external_id="cloud-run")
        )
        with run_state_lock(request.state_path.with_suffix(".lock")):
            save_run_state(state, request.state_path)
    result = complete_task(request, forge=forge)
    assert result.success, result.failure
    assert result.outcome_record.not_needed_review.requirement == (
        "required" if cloud else "exempt-local"
    )
    assert (PENDING in forge.labels) == cloud
    ctx = make_test_cycle_context(
        config=None,
        run_state=RunState(),
        tasks_by_issue={1110: make_test_task(1110, status_labels=tuple(forge.labels))},
    )
    assert ctx.is_effectively_done(1110) == (not cloud)


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
@pytest.mark.parametrize("terminal", ["status:not-needed", "status:done"])
def test_integrator_pending_precedes_closed_and_included(state, terminal):
    from orchestune.integrator.tasks import _dependency_integration_states
    from tests.conftest import make_issue

    issue = make_issue(
        12, state=state, labels=(terminal, "integration:included", PENDING)
    )
    result = _dependency_integration_states([issue], {12: "dependency"})
    assert "dependency" not in result.integrated
    assert result.not_integrated["dependency"] == PENDING


def test_closed_observation_without_task_still_blocks_override():
    from tests.conftest import make_issue

    ctx = make_test_cycle_context(
        config=None,
        run_state=RunState(),
        issue_records_by_number={
            12: make_issue(12, state="CLOSED", labels=("status:done", PENDING))
        },
        prior_parent_merge_completed_issue_numbers=frozenset({12}),
    )
    assert ctx.is_completion_blocked(12)
    assert not ctx.is_completion_confirmed(12)
