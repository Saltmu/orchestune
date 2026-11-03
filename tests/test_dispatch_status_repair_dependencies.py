"""Issue #872: fresh status-repair dependency evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
)
from orchestune.consistency.models import (
    ConsistencyReport,
    ConsistencyScope,
    RepairCommand,
    RepairStatus,
)
from orchestune.consistency.repairs.status import COMMAND_TRANSITION_LABEL
from orchestune.consistency.supervisor import (
    ConsistencyCycleReport,
    ConsistencyMode,
    ConsistencyScanResult,
    ScanKind,
)
from orchestune.dependencies.assessment import DependencyState
from orchestune.dependencies.resolution import REASON_MISSING
from orchestune.dispatch.config.model import DispatcherConfig
from orchestune.dispatch.cycle.run import _status_repair_commands
from orchestune.dispatch.status_dependency_policy import dependencies_completed
from orchestune.dispatch.status_repair import (
    _inspect_blocked_outcome,
    _precondition_holds,
    execute_status_repair_command,
)
from orchestune.dispatch.status_repair_dependencies import (
    evaluate_fresh_dependencies,
)
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import RunState
from orchestune.outcome_record import OutcomeRecord
from tests.conftest import make_issue, make_task


@dataclass(frozen=True)
class _CompletionEvidence:
    confirmed: frozenset[int] = frozenset()

    def is_completion_confirmed(self, issue_number: int) -> bool:
        return issue_number in self.confirmed


def _evaluate(subject, population, forge, *, confirmed=()):
    return evaluate_fresh_dependencies(
        subject,
        population,
        completion_evidence=_CompletionEvidence(frozenset(confirmed)),
        forge=forge,
    )


def test_missing_fresh_subject_fails_closed(in_memory_forge):
    stale = make_task(1, depends_on=())

    assert _evaluate(stale, {1: stale}, in_memory_forge) is None


def test_closed_fresh_subject_skips_before_dependency_label_io(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
            state="CLOSED",
        )
    )

    with patch.object(
        in_memory_forge,
        "get_issue_labels",
        side_effect=RuntimeError("dependency labels must not be fetched"),
    ) as get_labels:
        result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)

    assert result is None
    get_labels.assert_not_called()


def test_reparses_subject_and_uses_changed_dependency_declaration(in_memory_forge):
    stale = make_task(1, subtask_id="subject", depends_on=())
    dependency = make_task(2, subtask_id="dep", status_labels=(StatusLabel.DONE,))
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, subtask_id="dep", labels=(StatusLabel.DONE,))
    )

    result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)

    assert result is not None
    assert result.task.depends_on == ("dep",)
    assert result.dependencies.resolved == (2,)
    assert dependencies_completed(result.assessment)


def test_missing_dependency_in_fresh_population_stays_unresolved(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("missing",),
            labels=(StatusLabel.BLOCKED,),
        )
    )

    result = _evaluate(stale, {1: stale}, in_memory_forge)

    assert result is not None
    assert result.dependencies.resolved == ()
    assert result.dependencies.unresolved[0].reason == REASON_MISSING
    assert not dependencies_completed(result.assessment)


def test_native_dependency_is_resolved_from_the_fresh_subject(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            blocked_by=(2,),
            labels=(StatusLabel.BLOCKED,),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, subtask_id="dep", labels=(StatusLabel.NOT_NEEDED,))
    )

    result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)

    assert result is not None
    assert result.dependencies.resolved == (2,)
    assert dependencies_completed(result.assessment)


def test_body_dependency_does_not_resolve_to_same_name_in_another_epic(
    in_memory_forge,
):
    stale = make_task(1, subtask_id="subject", parent_number=100)
    other_epic = make_task(2, subtask_id="dep", parent_number=200)
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
            parent={"number": 100},
        )
    )

    result = _evaluate(stale, {1: stale, 2: other_epic}, in_memory_forge)

    assert result is not None
    assert result.dependencies.resolved == ()
    assert not dependencies_completed(result.assessment)


def test_live_lifecycle_truth_table(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, subtask_id="dep", labels=(StatusLabel.DONE,))
    )

    for labels, expected in (
        ((StatusLabel.DONE,), True),
        ((StatusLabel.NOT_NEEDED,), True),
        ((StatusLabel.DONE, StatusLabel.QUEUED), False),
        ((StatusLabel.QUEUED,), False),
    ):
        in_memory_forge.seed_issue(make_issue(2, subtask_id="dep", labels=labels))
        result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)
        assert result is not None
        assert dependencies_completed(result.assessment) is expected


def test_confirmed_completion_still_checks_live_labels(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )

    with patch.object(
        in_memory_forge,
        "get_issue_labels",
        wraps=in_memory_forge.get_issue_labels,
    ) as get_labels:
        result = _evaluate(
            stale,
            {1: stale, 2: dependency},
            in_memory_forge,
            confirmed={2},
        )

    assert result is not None
    assert result.assessment.resolved[0].state is DependencyState.COMPLETED
    get_labels.assert_called_once_with(2)


def test_late_remote_pending_blocks_confirmed_snapshot(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep", status_labels=(StatusLabel.DONE,))
    in_memory_forge.seed_issue(
        make_issue(
            1, subtask_id="subject", depends_on=("dep",), labels=(StatusLabel.BLOCKED,)
        )
    )
    in_memory_forge.seed_issue(
        make_issue(2, labels=(StatusLabel.DONE, "not-needed-review:pending"))
    )
    result = _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge, confirmed={2})
    assert result.assessment.resolved[0].state is DependencyState.WAITING


def test_confirmed_dependency_label_failure_is_not_absence(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1, subtask_id="subject", depends_on=("dep",), labels=(StatusLabel.BLOCKED,)
        )
    )
    with (
        patch.object(
            in_memory_forge, "get_issue_labels", side_effect=OSError("unavailable")
        ),
        pytest.raises(OSError),
    ):
        _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge, confirmed={2})


def test_forge_label_error_is_not_converted_to_unresolved(in_memory_forge):
    stale = make_task(1, subtask_id="subject")
    dependency = make_task(2, subtask_id="dep")
    in_memory_forge.seed_issue(
        make_issue(
            1,
            subtask_id="subject",
            depends_on=("dep",),
            labels=(StatusLabel.BLOCKED,),
        )
    )

    with patch.object(
        in_memory_forge,
        "get_issue_labels",
        side_effect=RuntimeError("Forge down"),
    ):
        with pytest.raises(RuntimeError, match="Forge down"):
            _evaluate(stale, {1: stale, 2: dependency}, in_memory_forge)


def test_no_declared_dependencies_is_distinguishable_from_unresolved(in_memory_forge):
    stale = make_task(1, subtask_id="subject", depends_on=("stale",))
    in_memory_forge.seed_issue(
        make_issue(
            1, subtask_id="subject", depends_on=(), labels=(StatusLabel.BLOCKED,)
        )
    )

    result = _evaluate(stale, {1: stale}, in_memory_forge)

    assert result is not None
    assert result.dependencies.is_empty
    assert dependencies_completed(result.assessment)


def test_dry_run_promotion_preview_suppressed_for_self_reserved_target():
    """Issue #1283: dry run promotion preview suppresses candidate when target has unreleased reservation."""
    cmd = RepairCommand(
        code=COMMAND_TRANSITION_LABEL,
        scope=ConsistencyScope.TASK,
        idempotency_key="test-key",
        subject_id="10",
        parameters=(("finding_code", BLOCKED_WITH_RESOLVED_DEPENDENCIES),),
    )
    scan = ConsistencyScanResult(
        boundary="status:blocked_with_resolved_dependencies",
        kind=ScanKind.FULL,
        report=ConsistencyReport(repository_id="test-repo"),
        repair_candidates=(cmd,),
    )
    report = ConsistencyCycleReport(mode=ConsistencyMode.REPAIR)

    # With pending/reserved stage: suppressed
    reserved_state = RunState(
        completion_reservations={"repo::10": {"issue_number": 10, "stage": "reserved"}}
    )
    assert (
        _status_repair_commands(
            report,
            scan,
            BLOCKED_WITH_RESOLVED_DEPENDENCIES,
            applied=False,
            run_state=reserved_state,
        )
        == ()
    )

    # With invalid stage: also suppressed
    invalid_state = RunState(
        completion_reservations={"repo::10": {"issue_number": 10, "stage": "invalid"}}
    )
    assert (
        _status_repair_commands(
            report,
            scan,
            BLOCKED_WITH_RESOLVED_DEPENDENCIES,
            applied=False,
            run_state=invalid_state,
        )
        == ()
    )

    # With reservation for an unrelated issue (e.g. 99): not suppressed
    unrelated_state = RunState(
        completion_reservations={"repo::99": {"issue_number": 99, "stage": "reserved"}}
    )
    assert _status_repair_commands(
        report,
        scan,
        BLOCKED_WITH_RESOLVED_DEPENDENCIES,
        applied=False,
        run_state=unrelated_state,
    ) == (cmd,)

    # Without reservation: not suppressed
    empty_state = RunState()
    assert _status_repair_commands(
        report,
        scan,
        BLOCKED_WITH_RESOLVED_DEPENDENCIES,
        applied=False,
        run_state=empty_state,
    ) == (cmd,)


def test_precondition_holds_no_current_blocked_outcome():
    assert _precondition_holds(
        "no-current-blocked-outcome",
        labels=("status:blocked",),
        dependencies_declared=True,
        dependencies_resolved=True,
        has_current_blocked_outcome=False,
    )
    assert not _precondition_holds(
        "no-current-blocked-outcome",
        labels=("status:blocked",),
        dependencies_declared=True,
        dependencies_resolved=True,
        has_current_blocked_outcome=True,
    )
    assert not _precondition_holds(
        "no-current-blocked-outcome",
        labels=("status:blocked",),
        dependencies_declared=True,
        dependencies_resolved=True,
    )


def test_inspect_blocked_outcome_scenarios(tmp_path):
    task = make_task(2, subtask_id="task-b")
    claim_id = "claim-current"
    blocked_issue = make_issue(
        2,
        subtask_id="task-b",
        labels=("status:blocked",),
        body=(
            "## Footprint\n```yaml\nsubtask_id: task-b\nfootprint:\n  - src/foo.py\n"
            f"claim_id: {claim_id}\n```\n"
        ),
    )

    # 1. Matching blocked outcome -> holds = True, diagnostic present
    forge = MagicMock()
    forge.list_all_issue_comments.return_value = [
        {
            "body": OutcomeRecord(
                result="blocked",
                issue=2,
                reason="footprint-expansion-required",
                claim_id=claim_id,
            ).render(),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    forge.get_issue.return_value = blocked_issue
    config = DispatcherConfig(
        parent_issue_number=100,
        forge=forge,
        events_log_path=tmp_path / "events.jsonl",
    )

    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert (
        diag
        == f"current blocked Outcome: reason=footprint-expansion-required, claim_id={claim_id}"
    )

    # 2. No outcome comment -> holds = False, no diagnostic
    forge.list_all_issue_comments.return_value = []
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is False
    assert diag is None

    # 3. Outcome is "done" -> holds = False
    forge.list_all_issue_comments.return_value = [
        {
            "body": OutcomeRecord(result="done", issue=2).render(),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is False
    assert diag is None

    # 4. Outcome is blocked from an old claim generation -> holds = False
    forge.list_all_issue_comments.return_value = [
        {
            "body": OutcomeRecord(
                result="blocked",
                issue=2,
                reason="footprint-expansion-required",
                claim_id="claim-old",
            ).render(),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is False
    assert diag is None

    # 5. Comment fetching fails -> fail closed: holds = True, error diagnostic
    forge.list_all_issue_comments.side_effect = RuntimeError("network outage")
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert "network outage" in str(diag)

    # 6. Issue fetching fails -> fail closed
    forge.list_all_issue_comments.side_effect = None
    forge.list_all_issue_comments.return_value = [
        {
            "body": OutcomeRecord(
                result="blocked",
                issue=2,
                reason="footprint-expansion-required",
                claim_id=claim_id,
            ).render(),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    forge.get_issue.side_effect = RuntimeError("issue fetch error")
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert "issue fetch error" in str(diag)

    # 7. Footprint has no claim_id -> fail closed: holds = True, diagnostic
    forge.get_issue.side_effect = None
    forge.get_issue.return_value = make_issue(
        2,
        subtask_id="task-b",
        labels=("status:blocked",),
        body="## Footprint\n```yaml\nsubtask_id: task-b\nfootprint:\n  - src/foo.py\n```\n",
    )
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert "no claim_id" in str(diag)

    # 8. Footprint missing entirely -> fail closed: holds = True
    forge.get_issue.return_value = make_issue(
        2,
        subtask_id="task-b",
        labels=("status:blocked",),
        body="No footprint here at all",
    )
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert "footprint block missing" in str(diag)

    # 9. Footprint invalid YAML -> fail closed: holds = True
    forge.get_issue.return_value = make_issue(
        2,
        subtask_id="task-b",
        labels=("status:blocked",),
        body="## Footprint\n```yaml\n: invalid: [yaml\n```\n",
    )
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert "YAML parsing failed" in str(diag)

    # 10. Outcome has no claim_id -> fail closed: holds = True
    forge.list_all_issue_comments.return_value = [
        {
            "body": OutcomeRecord(
                result="blocked",
                issue=2,
                reason="footprint-expansion-required",
                claim_id=None,
            ).render(),
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    forge.get_issue.return_value = blocked_issue
    has_blocked, diag = _inspect_blocked_outcome(task, config)
    assert has_blocked is True
    assert "it has no claim_id" in str(diag)


def _make_transition_command(subject_id: str = "2") -> RepairCommand:
    return RepairCommand(
        code=COMMAND_TRANSITION_LABEL,
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
        idempotency_key=f"status:{subject_id}:transition:status:queued",
        parameters=(
            ("new_label", StatusLabel.QUEUED),
            ("old_labels", (StatusLabel.BLOCKED,)),
            ("finding_code", BLOCKED_WITH_RESOLVED_DEPENDENCIES),
        ),
        preconditions=(
            "finding-certainty:known",
            "issue-open",
            "holds-primary-status:status:blocked",
            "dependencies-declared",
            "dependencies-resolved",
            "no-promotion-hold",
            "no-current-blocked-outcome",
        ),
    )


def test_does_not_promote_blocked_task_when_it_reported_blocked_outcome(
    tmp_path, in_memory_forge
):
    """Issue #1548: Task reporting blocked outcome stays in status:blocked."""
    claim_id = "claim-current"
    dep = make_task(1, subtask_id="dep", status_labels=("status:done",))
    blocked = make_task(
        2,
        subtask_id="task-b",
        status_labels=("status:blocked",),
        depends_on=("dep",),
    )
    in_memory_forge.seed_issue(make_issue(1, subtask_id="dep", labels=("status:done",)))
    in_memory_forge.seed_issue(
        make_issue(
            2,
            subtask_id="task-b",
            labels=("status:blocked",),
            depends_on=("dep",),
            body=(
                "## Footprint\n```yaml\nsubtask_id: task-b\nfootprint:\n  - src/foo.py\n"
                f"depends_on:\n  - dep\nclaim_id: {claim_id}\n```\n"
            ),
        )
    )
    in_memory_forge.create_issue_comment(
        2,
        OutcomeRecord(
            result="blocked",
            issue=2,
            reason="footprint-expansion-required",
            claim_id=claim_id,
        ).render(),
    )
    config = DispatcherConfig(
        parent_issue_number=100,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
        apply=True,
    )
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    result = execute_status_repair_command(
        _make_transition_command("2"),
        {1: dep, 2: blocked},
        completion_evidence=_CompletionEvidence(),
        config=config,
        now=now,
    )
    assert result.status is RepairStatus.SKIPPED
    assert any("current blocked Outcome" in diag for diag in result.diagnostics)
    assert in_memory_forge.get_issue_labels(2) == ("status:blocked",)


def test_promotes_blocked_task_when_latest_blocked_outcome_is_from_older_generation(
    tmp_path, in_memory_forge
):
    """Issue #1548: Task with blocked outcome from older generation promotes."""
    dep = make_task(1, subtask_id="dep", status_labels=("status:done",))
    blocked = make_task(
        2,
        subtask_id="task-b",
        status_labels=("status:blocked",),
        depends_on=("dep",),
    )
    in_memory_forge.seed_issue(make_issue(1, subtask_id="dep", labels=("status:done",)))
    in_memory_forge.seed_issue(
        make_issue(
            2,
            subtask_id="task-b",
            labels=("status:blocked",),
            depends_on=("dep",),
            body=(
                "## Footprint\n```yaml\nsubtask_id: task-b\nfootprint:\n  - src/foo.py\n"
                "depends_on:\n  - dep\nclaim_id: claim-new\n```\n"
            ),
        )
    )
    in_memory_forge.create_issue_comment(
        2,
        OutcomeRecord(
            result="blocked",
            issue=2,
            reason="footprint-expansion-required",
            claim_id="claim-old",
        ).render(),
    )
    config = DispatcherConfig(
        parent_issue_number=100,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
        apply=True,
    )
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    result = execute_status_repair_command(
        _make_transition_command("2"),
        {1: dep, 2: blocked},
        completion_evidence=_CompletionEvidence(),
        config=config,
        now=now,
    )
    assert result.status is RepairStatus.APPLIED
    assert in_memory_forge.get_issue_labels(2) == ("status:queued",)


def test_does_not_promote_blocked_task_when_comment_fetching_fails(
    tmp_path, in_memory_forge
):
    """Issue #1548: Failure to retrieve comments fails closed."""
    dep = make_task(1, subtask_id="dep", status_labels=("status:done",))
    blocked = make_task(
        2,
        subtask_id="task-b",
        status_labels=("status:blocked",),
        depends_on=("dep",),
    )
    in_memory_forge.seed_issue(make_issue(1, subtask_id="dep", labels=("status:done",)))
    in_memory_forge.seed_issue(
        make_issue(
            2,
            subtask_id="task-b",
            labels=("status:blocked",),
            depends_on=("dep",),
            body=(
                "## Footprint\n```yaml\nsubtask_id: task-b\nfootprint:\n  - src/foo.py\n"
                "depends_on:\n  - dep\nclaim_id: claim-current\n```\n"
            ),
        )
    )
    config = DispatcherConfig(
        parent_issue_number=100,
        run_state_path=tmp_path / "run_state.json",
        events_log_path=tmp_path / "events.jsonl",
        worktree_root=tmp_path / "worktrees",
        forge=in_memory_forge,
        apply=True,
    )
    with patch.object(
        in_memory_forge,
        "list_all_issue_comments",
        side_effect=RuntimeError("comment fetch failed"),
    ):
        now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        result = execute_status_repair_command(
            _make_transition_command("2"),
            {1: dep, 2: blocked},
            completion_evidence=_CompletionEvidence(),
            config=config,
            now=now,
        )
    assert result.status is RepairStatus.SKIPPED
    assert any("comment fetch failed" in diag for diag in result.diagnostics)
    assert in_memory_forge.get_issue_labels(2) == ("status:blocked",)
