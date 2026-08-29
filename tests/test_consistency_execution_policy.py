from __future__ import annotations

from datetime import UTC, datetime

from orchestune.consistency import (
    ConsistencyEngine,
    ConsistencyReport,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    ScopedObservations,
)
from orchestune.consistency.invariants.execution import (
    ACTIVE_EXECUTION_OWNERSHIP_CONFLICT,
    BRANCH_MISSING,
    BRANCH_OWNERSHIP_CONFLICT,
    EXECUTION_OBSERVATION_UNKNOWN,
    FACT_PULL_REQUEST_BASE_REF,
    FACT_PULL_REQUEST_HEAD_REF,
    FORGE_OBSERVATION_UNKNOWN,
    ISSUE_OWNERSHIP_CONFLICT,
    LOCAL_PROCESS_DEAD,
    ORPHAN_EXECUTION,
    PULL_REQUEST_ASSOCIATION_UNKNOWN,
    PULL_REQUEST_BASE_MISMATCH,
    PULL_REQUEST_HEAD_MISMATCH,
    RUN_STATE_MISSING,
    RUN_STATE_STALE,
    WORKTREE_MISSING,
    WORKTREE_OWNERSHIP_CONFLICT,
    execution_invariants,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    Evidence,
    FindingSeverity,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    EXECUTION_KIND_NONE,
    FACT_BRANCH_EXISTS,
    FACT_BRANCH_NAME,
    FACT_EXECUTION_EXTERNAL_ID,
    FACT_EXECUTION_EXTERNAL_STATUS,
    FACT_EXECUTION_KIND,
    FACT_EXECUTION_PID,
    FACT_EXECUTION_PROCESS_ALIVE,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_STATE,
    FACT_PARENT_ISSUE_NUMBER,
    FACT_PULL_REQUEST_NUMBER,
    FACT_WORKTREE_EXISTS,
    FACT_WORKTREE_PATH,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
    plan_execution_repairs,
)

NOW = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
REPOSITORY_ID = "Saltmu/orchestune"


def _observation(
    name: str,
    value: object,
    *,
    certainty: ObservationCertainty = ObservationCertainty.KNOWN,
    source: str = "test",
    diagnostics: tuple[str, ...] = (),
) -> Observation:
    return Observation(
        name=name,
        certainty=certainty,
        source=source,
        observed_at=NOW,
        value=value,  # type: ignore[arg-type]
        diagnostics=diagnostics,
    )


def _task_scope(
    subject_id: str,
    *,
    kind: str = EXECUTION_KIND_LOCAL,
    pid: int | None = 100,
    external_id: str | None = None,
    process_alive: bool | None = True,
    external_status: str | None = None,
    branch: str | None = None,
    branch_exists: bool | None = True,
    worktree: str | None = None,
    worktree_exists: bool | None = True,
    issue_state: str | None = "OPEN",
    parent: int | None = 700,
    pr_number: int | None = 720,
    extra: tuple[Observation, ...] = (),
) -> ScopedObservations:
    branch = branch if branch is not None else f"codex/issue-{subject_id}"
    worktree = worktree if worktree is not None else f"worktree/{subject_id}"
    facts = (
        _observation(FACT_ISSUE_STATE, issue_state, source="forge"),
        _observation(FACT_PARENT_ISSUE_NUMBER, parent, source="forge"),
        _observation(FACT_EXECUTION_KIND, kind, source="run-state"),
        _observation(FACT_EXECUTION_PID, pid, source="run-state"),
        _observation(FACT_EXECUTION_EXTERNAL_ID, external_id, source="run-state"),
        _observation(FACT_EXECUTION_PROCESS_ALIVE, process_alive, source="process"),
        _observation(
            FACT_EXECUTION_EXTERNAL_STATUS,
            external_status,
            source="external-execution",
        ),
        _observation(FACT_BRANCH_NAME, branch, source="run-state"),
        _observation(FACT_BRANCH_EXISTS, branch_exists, source="git"),
        _observation(FACT_WORKTREE_PATH, worktree, source="run-state"),
        _observation(FACT_WORKTREE_EXISTS, worktree_exists, source="worktree"),
        _observation(FACT_PULL_REQUEST_NUMBER, pr_number, source="forge"),
    )
    facts_by_name = {fact.name: fact for fact in facts}
    facts_by_name.update({fact.name: fact for fact in extra})
    return ScopedObservations(
        scope=ConsistencyScope.TASK,
        subject_id=subject_id,
        facts=tuple(sorted(facts_by_name.values(), key=lambda fact: fact.name)),
    )


def _state(
    *tasks: ScopedObservations,
    forge: Observation | None = None,
) -> ObservedRepositoryState:
    repository = ScopedObservations(
        scope=ConsistencyScope.REPOSITORY,
        facts=(forge or _observation(FACT_FORGE_REACHABLE, True, source="forge"),),
    )
    return ObservedRepositoryState(
        repository_id=REPOSITORY_ID,
        observed_at=NOW,
        observations=(repository, *tasks),
    )


def _desired(**active_by_subject: bool) -> DesiredRepositoryState:
    return DesiredRepositoryState(
        repository_id=REPOSITORY_ID,
        facts=tuple(
            DesiredFact(
                name="task.run_state_active",
                value=active,
                scope=ConsistencyScope.TASK,
                subject_id=subject,
                reason="test",
            )
            for subject, active in sorted(active_by_subject.items())
        ),
    )


def _evaluate(
    observed: ObservedRepositoryState,
    desired: DesiredRepositoryState,
) -> ConsistencyReport:
    return ConsistencyEngine(execution_invariants()).evaluate(observed, desired)


def _codes(report: ConsistencyReport) -> list[str]:
    return [finding.code for finding in report.findings]


def test_repository_ownership_conflicts_are_exhaustive_and_deterministic() -> None:
    first = _task_scope("703", branch="shared", worktree="worktree/shared", pid=42)
    second = _task_scope("704", branch="shared", worktree="worktree/shared", pid=42)

    report = _evaluate(_state(first, second), _desired(**{"703": True, "704": True}))
    reversed_report = _evaluate(
        _state(second, first), _desired(**{"703": True, "704": True})
    )

    assert report == reversed_report
    assert _codes(report) == [
        ACTIVE_EXECUTION_OWNERSHIP_CONFLICT,
        BRANCH_OWNERSHIP_CONFLICT,
        WORKTREE_OWNERSHIP_CONFLICT,
    ]
    assert all(
        finding.scope is ConsistencyScope.REPOSITORY
        and finding.repairability is Repairability.MANUAL
        for finding in report.findings
    )


def test_dead_local_pid_is_reclaimable_but_cloud_pid_none_is_not_dead() -> None:
    local = _task_scope("703", process_alive=False, worktree_exists=True)
    cloud = _task_scope(
        "704",
        kind=EXECUTION_KIND_CLOUD,
        pid=None,
        external_id="codex-cloud:abc",
        process_alive=None,
        external_status="running",
    )

    report = _evaluate(_state(local, cloud), _desired(**{"703": True, "704": True}))

    dead = [
        finding for finding in report.findings if finding.code == LOCAL_PROCESS_DEAD
    ]
    assert [finding.subject_id for finding in dead] == ["703"]
    assert dead[0].repairability is Repairability.AUTOMATIC
    assert EXECUTION_OBSERVATION_UNKNOWN not in _codes(report)
    assert [command.code for command in plan_execution_repairs(report)] == [
        COMMAND_RECLAIM,
        COMMAND_REQUEUE,
    ]


def test_cloud_execution_identifier_cannot_be_owned_by_multiple_tasks() -> None:
    first = _task_scope(
        "703",
        kind=EXECUTION_KIND_CLOUD,
        pid=None,
        external_id="codex-cloud:shared",
        process_alive=None,
        external_status="running",
    )
    second = _task_scope(
        "704",
        kind=EXECUTION_KIND_CLOUD,
        pid=None,
        external_id="codex-cloud:shared",
        process_alive=None,
        external_status="running",
    )

    report = _evaluate(_state(first, second), _desired(**{"703": True, "704": True}))

    assert ACTIVE_EXECUTION_OWNERSHIP_CONFLICT in _codes(report)


def test_duplicate_task_scope_is_an_issue_ownership_conflict() -> None:
    task = _task_scope("704")

    report = _evaluate(_state(task, task), _desired(**{"704": True}))

    assert ISSUE_OWNERSHIP_CONFLICT in _codes(report)


def test_partial_forge_and_provider_failures_are_unknown_and_never_repaired() -> None:
    forge = _observation(
        FACT_FORGE_REACHABLE,
        None,
        certainty=ObservationCertainty.UNKNOWN,
        source="forge",
        diagnostics=("forge probe failed: timeout",),
    )
    uncertain_status = _observation(
        FACT_EXECUTION_EXTERNAL_STATUS,
        None,
        certainty=ObservationCertainty.UNKNOWN,
        source="external-execution",
        diagnostics=("external execution probe failed: timeout",),
    )
    cloud = _task_scope(
        "704",
        kind=EXECUTION_KIND_CLOUD,
        pid=None,
        external_id="codex-cloud:abc",
        process_alive=None,
        extra=(uncertain_status,),
    )
    dead_local = _task_scope("703", process_alive=False)

    report = _evaluate(
        _state(dead_local, cloud, forge=forge),
        _desired(**{"703": True, "704": True}),
    )

    assert _codes(report) == [
        FORGE_OBSERVATION_UNKNOWN,
        LOCAL_PROCESS_DEAD,
        EXECUTION_OBSERVATION_UNKNOWN,
    ]
    assert plan_execution_repairs(report) == ()


def test_provider_uncertainty_blocks_reclaim_from_other_task_findings() -> None:
    uncertain_status = _observation(
        FACT_EXECUTION_EXTERNAL_STATUS,
        None,
        certainty=ObservationCertainty.UNKNOWN,
        source="external-execution",
        diagnostics=("provider temporarily unavailable",),
    )
    cloud = _task_scope(
        "704",
        kind=EXECUTION_KIND_CLOUD,
        pid=None,
        external_id="codex-cloud:abc",
        process_alive=None,
        worktree_exists=False,
        extra=(uncertain_status,),
    )

    report = _evaluate(_state(cloud), _desired(**{"704": False}))

    assert EXECUTION_OBSERVATION_UNKNOWN in _codes(report)
    assert WORKTREE_MISSING in _codes(report)
    assert RUN_STATE_STALE in _codes(report)
    assert plan_execution_repairs(report) == ()


def test_missing_resources_stale_run_state_and_orphan_have_stable_codes() -> None:
    missing = _task_scope("703", branch_exists=False, worktree_exists=False)
    orphan = _task_scope("704", issue_state=None, pid=101)

    report = _evaluate(
        _state(missing, orphan),
        _desired(**{"703": True, "704": False}),
    )

    assert _codes(report) == [
        BRANCH_MISSING,
        WORKTREE_MISSING,
        ORPHAN_EXECUTION,
        RUN_STATE_STALE,
    ]
    assert all(
        finding.repairability is Repairability.AUTOMATIC for finding in report.findings
    )


def test_desired_active_task_without_execution_has_missing_run_state_finding() -> None:
    task = _task_scope(
        "704",
        kind=EXECUTION_KIND_NONE,
        pid=None,
        process_alive=None,
        worktree_exists=None,
    )

    report = _evaluate(_state(task), _desired(**{"704": True}))

    assert _codes(report) == [RUN_STATE_MISSING]
    assert [command.code for command in plan_execution_repairs(report)] == [
        COMMAND_REQUEUE,
        COMMAND_BOOKKEEPING,
    ]


def test_stale_terminal_run_state_is_reclaimed_without_requeue() -> None:
    task = _task_scope("704", process_alive=False)

    report = _evaluate(_state(task), _desired(**{"704": False}))

    assert LOCAL_PROCESS_DEAD in _codes(report)
    assert RUN_STATE_STALE in _codes(report)
    assert [command.code for command in plan_execution_repairs(report)] == [
        COMMAND_RECLAIM,
        COMMAND_BOOKKEEPING,
    ]


def test_pr_association_unknown_and_head_base_mismatches_are_diagnosable() -> None:
    pr_facts = (
        _observation(FACT_PULL_REQUEST_HEAD_REF, "other/head", source="forge"),
        _observation(FACT_PULL_REQUEST_BASE_REF, "main", source="forge"),
    )
    mismatch = _task_scope("704", branch="codex/issue-704", extra=pr_facts)
    unknown_pr = _task_scope(
        "705",
        pid=101,
        pr_number=None,
        extra=(
            _observation(
                FACT_PULL_REQUEST_NUMBER,
                None,
                certainty=ObservationCertainty.UNKNOWN,
                source="forge",
                diagnostics=("filtered snapshot",),
            ),
        ),
    )

    report = _evaluate(
        _state(mismatch, unknown_pr),
        _desired(**{"704": True, "705": True}),
    )

    assert _codes(report) == [
        PULL_REQUEST_BASE_MISMATCH,
        PULL_REQUEST_HEAD_MISMATCH,
        PULL_REQUEST_ASSOCIATION_UNKNOWN,
    ]
    assert all(
        finding.repairability is not Repairability.AUTOMATIC
        for finding in report.findings
    )


def test_repair_plan_is_deterministic_deduplicated_and_idempotency_aware() -> None:
    report = _evaluate(
        _state(
            _task_scope(
                "703", process_alive=False, branch_exists=False, worktree_exists=False
            ),
            _task_scope("704", issue_state=None),
        ),
        _desired(**{"703": True, "704": False}),
    )

    commands = plan_execution_repairs(report)
    reversed_report = ConsistencyReport(
        repository_id=report.repository_id,
        findings=tuple(reversed(report.findings)),
        evaluated_invariants=report.evaluated_invariants,
    )

    assert commands == plan_execution_repairs(reversed_report)
    assert [(command.subject_id, command.code) for command in commands] == [
        ("703", COMMAND_RECLAIM),
        ("703", COMMAND_REQUEUE),
        ("704", COMMAND_RECLAIM),
        ("704", COMMAND_BOOKKEEPING),
    ]
    assert len({command.idempotency_key for command in commands}) == len(commands)
    assert all(command.preconditions for command in commands)
    assert plan_execution_repairs(report) == commands


def test_manual_or_unknown_findings_cannot_be_smuggled_into_repair_plan() -> None:
    findings = tuple(
        ConsistencyFinding(
            code=LOCAL_PROCESS_DEAD,
            scope=ConsistencyScope.TASK,
            subject_id=subject,
            severity=FindingSeverity.ERROR,
            expected=Evidence("running"),
            observed=Evidence("not running"),
            repairability=repairability,
        )
        for subject, repairability in (
            ("manual", Repairability.MANUAL),
            ("unknown", Repairability.NONE),
        )
    )
    report = ConsistencyReport(repository_id=REPOSITORY_ID, findings=findings)

    assert plan_execution_repairs(report) == ()
