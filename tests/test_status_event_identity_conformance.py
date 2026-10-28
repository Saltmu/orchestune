"""Runtime comparisons for the execution-identity contract in #1286."""

from dataclasses import replace
from pathlib import Path

import pytest

from orchestune.dispatch.cycle_context_state import RecordStatus
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState
from orchestune.ledger.status_events import (
    Applied,
    Event,
    EventInput,
    ExecutionIdentity,
    IndeterminateExecution,
    Kind,
    NoOp,
    Rejected,
    TaskModel,
    apply_event,
)
from tests.dispatch_test_support import (
    make_test_active_worktree,
    make_test_cycle_context,
    make_test_task,
    replace_flat,
)
from tests.status_event_identity_test_support import (
    EXPECTED_SOURCES,
    all_route_keys,
    execution_identity,
    expected_launch_fact,
    protection_rows,
    render_protection_table,
)
from tests.status_event_test_support import EVENT_BY_SOURCE, production_limits
from tests.verification_contract_test_support import expect_violation, require

ISSUE = 1286
LIMITS = production_limits()


def test_relaunch_of_same_fact_after_confirmed_recovery_matches_production() -> None:
    """Production accepts a retired fact after recovery; the model must too."""
    active = make_test_active_worktree(ISSUE)
    identity = execution_identity(active)
    context = make_test_cycle_context(
        tasks_by_issue={
            ISSUE: make_test_task(ISSUE, status_labels=(StatusLabel.QUEUED.value,))
        }
    )
    first = context.record_launch(active)
    assert first.status is RecordStatus.APPLIED
    recovered = context.record_transition(
        ISSUE,
        expected_labels=(StatusLabel.IN_PROGRESS.value,),
        verified_labels=(StatusLabel.QUEUED.value,),
        execution_active=False,
    )
    assert recovered.status is RecordStatus.APPLIED
    production = context.record_launch(active)

    state = TaskModel.from_labels((StatusLabel.QUEUED.value,))
    initial = apply_event(state, EventInput(Event.LAUNCH, execution=identity), LIMITS)
    assert isinstance(initial, Applied)
    retired = apply_event(
        initial.state,
        EventInput(Event.REQUEUE, Kind.RECOVERY, execution=identity),
        LIMITS,
    )
    assert isinstance(retired, Applied)
    modeled = apply_event(
        retired.state, EventInput(Event.LAUNCH, execution=identity), LIMITS
    )

    assert production.status is RecordStatus.APPLIED
    assert isinstance(modeled, Applied)
    production_task = context.task(ISSUE)
    assert production_task is not None
    assert modeled.state.labels == frozenset(production_task.status_labels)
    assert modeled.state.execution_active
    assert modeled.state.execution_identity == identity
    assert context.launch_fact(ISSUE) == expected_launch_fact(active)


def test_record_launch_identity_matrix() -> None:
    """Same/other active facts keep production and the model in agreement."""
    active = make_test_active_worktree(ISSUE)
    assert active.launch.pid is not None
    other = replace_flat(active, pid=active.launch.pid + 1)
    identity = execution_identity(active)
    duplicate_instance = make_test_active_worktree(ISSUE)
    assert duplicate_instance is not active
    assert execution_identity(duplicate_instance) == identity
    context = make_test_cycle_context(
        tasks_by_issue={
            ISSUE: make_test_task(ISSUE, status_labels=(StatusLabel.QUEUED.value,))
        }
    )
    assert context.record_launch(active).status is RecordStatus.APPLIED
    same = context.record_launch(duplicate_instance)
    different = context.record_launch(other)

    state = TaskModel.from_labels((StatusLabel.QUEUED.value,))
    first = apply_event(state, EventInput(Event.LAUNCH, execution=identity), LIMITS)
    assert isinstance(first, Applied)
    same_model = apply_event(
        first.state, EventInput(Event.LAUNCH, execution=identity), LIMITS
    )
    assert not isinstance(same_model, Rejected)
    different_model = apply_event(
        first.state,
        EventInput(Event.LAUNCH, execution=execution_identity(other)),
        LIMITS,
    )

    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        same.status is RecordStatus.NOOP and isinstance(same_model, NoOp),
        "same active fact must be a production NOOP and a model NoOp",
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        different.status is RecordStatus.CONFLICT
        and different.reason == "launch-mismatch"
        and different_model == Rejected("launch-mismatch"),
        "different active fact must conflict without replacing the current launch",
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        context.launch_fact(ISSUE) == expected_launch_fact(active),
        "a rejected launch must leave the original fact unchanged",
    )
    assert isinstance(different_model, Rejected)
    assert first.state.execution_identity == identity
    production_task = context.task(ISSUE)
    assert production_task is not None
    assert first.state.labels == frozenset(production_task.status_labels)


def test_each_launch_fact_field_contributes_to_execution_identity() -> None:
    """Every normalized LaunchFact field participates in canonical identity."""
    active = make_test_active_worktree(
        ISSUE,
        external_id="worker-a",
        launch_attempt_id="attempt-a",
        claim_id="claim-a",
    )
    assert active.launch.pid is not None
    assert active.launch.started_at is not None
    base_identity = execution_identity(active)
    alternatives = {
        "issue_number": ISSUE + 1,
        "branch": "claude/issue-1286-other-branch",
        "worktree_path": "worktrees/other",
        "pid": active.launch.pid + 1,
        "started_at": active.launch.started_at + 1,
        "external_id": "worker-b",
        "launch_attempt_id": "attempt-b",
        "owner_kind": "interactive",
        "claim_id": "claim-b",
        "reservation_kind": "repository",
    }
    changed_facts = {
        field: replace_flat(active, **{field: value})
        for field, value in alternatives.items()
    }
    assert all(
        execution_identity(fact) != base_identity for fact in changed_facts.values()
    )

    for field, changed in changed_facts.items():
        context = make_test_cycle_context(
            tasks_by_issue={
                number: make_test_task(
                    number, status_labels=(StatusLabel.QUEUED.value,)
                )
                for number in (ISSUE, ISSUE + 1)
            }
        )
        assert context.record_launch(active).status is RecordStatus.APPLIED
        result = context.record_launch(changed)
        if field == "issue_number":
            assert result.status is RecordStatus.APPLIED
            assert context.launch_fact(ISSUE + 1) == expected_launch_fact(changed)
        else:
            assert result.status is RecordStatus.CONFLICT, field
            assert result.reason == "launch-mismatch", field
            assert context.launch_fact(ISSUE) == expected_launch_fact(active), field


def test_invalid_fact_fields_are_normalized_independently() -> None:
    active = make_test_active_worktree(
        ISSUE,
        external_id="worker-a",
        launch_attempt_id="",
        claim_id=17,
        pid=True,
        started_at=float("inf"),
    )
    expected = expected_launch_fact(active)
    assert expected.pid is None
    assert expected.started_at is None
    assert expected.launch_attempt_id is None
    assert expected.claim_id is None

    context = make_test_cycle_context(
        tasks_by_issue={
            ISSUE: make_test_task(ISSUE, status_labels=(StatusLabel.QUEUED.value,))
        }
    )
    assert context.record_launch(active).status is RecordStatus.APPLIED
    assert context.launch_fact(ISSUE) == expected

    invalid_external = make_test_active_worktree(
        ISSUE,
        external_id=17,
        pid=4_200_222,
        started_at=True,
        launch_attempt_id="attempt-a",
        claim_id="claim-a",
    )
    expected_external = expected_launch_fact(invalid_external)
    assert expected_external.pid == 4_200_222
    assert expected_external.started_at is None
    assert expected_external.external_id is None
    external_context = make_test_cycle_context(
        tasks_by_issue={
            ISSUE: make_test_task(ISSUE, status_labels=(StatusLabel.QUEUED.value,))
        }
    )
    assert (
        external_context.record_launch(invalid_external).status is RecordStatus.APPLIED
    )
    assert external_context.launch_fact(ISSUE) == expected_external


def test_unknown_invalid_terminal_and_indeterminate_launch_branches() -> None:
    """All early record_launch exits agree with their independent model cases."""
    active = make_test_active_worktree(ISSUE)
    unknown = make_test_cycle_context().record_launch(active)
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        (unknown.status, unknown.reason) == (RecordStatus.CONFLICT, "unknown-issue"),
        "unknown Issue must be rejected at the model-set lookup boundary",
    )

    queued_task = make_test_task(ISSUE, status_labels=(StatusLabel.QUEUED.value,))
    invalid = replace_flat(active, launch_phase="prepared")
    invalid_context = make_test_cycle_context(tasks_by_issue={ISSUE: queued_task})
    invalid_record = invalid_context.record_launch(invalid)
    invalid_model = apply_event(
        TaskModel.from_labels((StatusLabel.QUEUED.value,)),
        EventInput(Event.LAUNCH, execution=None),
        LIMITS,
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        (invalid_record.status, invalid_record.reason)
        == (RecordStatus.CONFLICT, "invalid-launch")
        and invalid_model == Rejected("invalid-launch"),
        "invalid launch phase must reject before changing model or production state",
    )
    invalid_task = invalid_context.task(ISSUE)
    assert invalid_task is not None
    assert invalid_task.status_labels == (StatusLabel.QUEUED.value,)
    assert invalid_context.launch_fact(ISSUE) is None

    terminal = make_test_cycle_context(
        tasks_by_issue={
            ISSUE: make_test_task(
                ISSUE,
                status_labels=(StatusLabel.BLOCKED_HUMAN_REVIEW.value,),
            )
        }
    )
    terminal_record = terminal.record_launch(active)
    terminal_model = apply_event(
        TaskModel.from_labels((StatusLabel.BLOCKED_HUMAN_REVIEW.value,)),
        EventInput(Event.LAUNCH, execution=execution_identity(active)),
        LIMITS,
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        (terminal_record.status, terminal_record.reason)
        == (RecordStatus.CONFLICT, "terminal-state")
        and terminal_model == Rejected("terminal-state"),
        "terminal human-review state must remain terminal for LAUNCH",
    )

    current = make_test_active_worktree(ISSUE, branch="claude/issue-1286-a")
    ambiguous = make_test_active_worktree(ISSUE, branch="claude/issue-1286-b")
    uncertain = make_test_cycle_context(
        tasks_by_issue={
            ISSUE: make_test_task(
                ISSUE,
                status_labels=(StatusLabel.IN_PROGRESS.value,),
            )
        },
        run_state=RunState(active_worktrees={"current": current, "other": ambiguous}),
    )
    uncertain_record = uncertain.record_launch(active)
    uncertain_model_state = replace(
        TaskModel.from_labels((StatusLabel.IN_PROGRESS.value,)),
        execution_identity=IndeterminateExecution("ambiguous-active-fact"),
    )
    uncertain_model = apply_event(
        uncertain_model_state,
        EventInput(Event.LAUNCH, execution=execution_identity(active)),
        LIMITS,
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        (uncertain_record.status, uncertain_record.reason)
        == (RecordStatus.CONFLICT, "launch-mismatch")
        and uncertain_model == Rejected("launch-mismatch"),
        "ambiguous active facts must stay held until a confirmed inactive transition",
    )


def test_source_protection_inventory_is_complete() -> None:
    """Every source/condition pair has an explicit identity-protection row."""
    rows = protection_rows()
    keys = {(row.source, row.condition, row.event, row.kind) for row in rows}
    route_keys = all_route_keys()
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        set(EXPECTED_SOURCES) == set(EVENT_BY_SOURCE),
        "source inventory differs from EVENT_BY_SOURCE",
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        len(keys) == len(rows) and keys == route_keys,
        "source/condition inventory has missing or duplicate routes",
    )
    require(
        "P3A-LAUNCH-IDENTITY-CONFORMANCE",
        all(
            row.guard
            and row.scope
            and row.old_input
            and row.effect
            and row.test_node
            and row.excluded_reason
            for row in rows
        ),
        "every route needs a guard, scope, expected old-input effect, test, and boundary reason",
    )


def test_status_label_docs_match_protection_inventory() -> None:
    """Both language editions publish the same complete source/condition table."""
    root = Path(__file__).parents[1]
    expected = render_protection_table()
    for language in ("ja", "en"):
        document = (root / "docs" / language / "status-labels.md").read_text(
            encoding="utf-8"
        )
        marker = "<!-- launch-identity-protection -->"
        section = document.partition(marker)[2]
        require(
            "P3A-LAUNCH-IDENTITY-CONFORMANCE",
            bool(section) and expected in section,
            f"{language} status-labels protection inventory differs from EVENT_BY_SOURCE",
        )


def test_control_record_launch_identity_guard_bypass_is_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken active-fact guard trips this contract, not a generic assert."""
    import orchestune.ledger.status_events as status_events

    current = replace(
        TaskModel.from_labels((StatusLabel.IN_PROGRESS.value,)),
        execution_identity=ExecutionIdentity("current"),
    )
    event = EventInput(Event.LAUNCH, execution=ExecutionIdentity("different-fact"))

    def check() -> None:
        result = apply_event(current, event, LIMITS)
        require(
            "P3A-LAUNCH-IDENTITY-CONFORMANCE",
            result == Rejected("launch-mismatch"),
            "different active fact was accepted",
        )

    with expect_violation("P3A-LAUNCH-IDENTITY-CONFORMANCE"):
        monkeypatch.setattr(status_events, "_refuse_launch", lambda *_: None)
        check()
