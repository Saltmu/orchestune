"""Pure repository-wide invariants for executions and their Git resources."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from orchestune.consistency.contracts import Invariant
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    Evidence,
    FactValue,
    FindingSeverity,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    ScopedObservations,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    EXECUTION_KIND_UNKNOWN,
)
from orchestune.consistency.vocabulary import (
    DESIRED_RUN_STATE_ACTIVE,
    DESIRED_TASK_TIMEOUT_SECONDS,
    DESIRED_ZOMBIE_GC_ENABLED,
    FACT_BRANCH_EXISTS,
    FACT_BRANCH_NAME,
    FACT_EXECUTION_EXTERNAL_ID,
    FACT_EXECUTION_EXTERNAL_STATUS,
    FACT_EXECUTION_KIND,
    FACT_EXECUTION_PID,
    FACT_EXECUTION_PROCESS_ALIVE,
    FACT_EXECUTION_STARTED_AT,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_STATE,
    FACT_PARENT_ISSUE_NUMBER,
    FACT_PULL_REQUEST_BASE_REF,
    FACT_PULL_REQUEST_HEAD_REF,
    FACT_PULL_REQUEST_NUMBER,
    FACT_WORKTREE_EXISTS,
    FACT_WORKTREE_PATH,
)

# Stable finding codes.  These values are persisted in reports and repair
# allowlists, so changing one is a compatibility change.
ACTIVE_EXECUTION_OWNERSHIP_CONFLICT = "execution.active-execution-ownership-conflict"
BRANCH_MISSING = "execution.branch-missing"
BRANCH_OWNERSHIP_CONFLICT = "execution.branch-ownership-conflict"
EXECUTION_OBSERVATION_UNKNOWN = "execution.observation-unknown"
EXECUTION_TIMED_OUT = "execution.timed-out"
FORGE_OBSERVATION_UNKNOWN = "execution.forge-observation-unknown"
HANDLELESS_EXECUTION_ORPHAN = "execution.handleless-orphan"
ISSUE_OWNERSHIP_CONFLICT = "execution.issue-ownership-conflict"
LOCAL_PROCESS_DEAD = "execution.local-process-dead"
ORPHAN_EXECUTION = "execution.orphan"
PULL_REQUEST_ASSOCIATION_UNKNOWN = "execution.pull-request-association-unknown"
PULL_REQUEST_BASE_MISMATCH = "execution.pull-request-base-mismatch"
PULL_REQUEST_HEAD_MISMATCH = "execution.pull-request-head-mismatch"
RUN_STATE_MISSING = "execution.run-state-missing"
RUN_STATE_STALE = "execution.run-state-stale"
WORKTREE_MISSING = "execution.worktree-missing"
WORKTREE_OWNERSHIP_CONFLICT = "execution.worktree-ownership-conflict"

_KNOWN = ObservationCertainty.KNOWN
_ACTIVE_KINDS = frozenset({EXECUTION_KIND_LOCAL, EXECUTION_KIND_CLOUD})

REQUIRED_OBSERVED_FACT_NAMES_BY_SCOPE = {
    ConsistencyScope.REPOSITORY: frozenset({FACT_FORGE_REACHABLE}),
    ConsistencyScope.TASK: frozenset(
        {
            FACT_BRANCH_EXISTS,
            FACT_BRANCH_NAME,
            FACT_EXECUTION_EXTERNAL_ID,
            FACT_EXECUTION_EXTERNAL_STATUS,
            FACT_EXECUTION_KIND,
            FACT_EXECUTION_PID,
            FACT_EXECUTION_PROCESS_ALIVE,
            FACT_EXECUTION_STARTED_AT,
            FACT_ISSUE_STATE,
            FACT_PARENT_ISSUE_NUMBER,
            FACT_PULL_REQUEST_BASE_REF,
            FACT_PULL_REQUEST_HEAD_REF,
            FACT_PULL_REQUEST_NUMBER,
            FACT_WORKTREE_EXISTS,
            FACT_WORKTREE_PATH,
        }
    ),
}


def _fact(scope: ScopedObservations, name: str) -> Observation | None:
    matches = tuple(fact for fact in scope.facts if fact.name == name)
    return matches[0] if len(matches) == 1 else None


def _desired_fact(
    desired: DesiredRepositoryState, subject_id: str, name: str
) -> DesiredFact | None:
    matches = tuple(
        fact
        for fact in desired.facts
        if fact.scope is ConsistencyScope.TASK
        and fact.subject_id == subject_id
        and fact.name == name
    )
    return matches[0] if len(matches) == 1 else None


def _desired_repository_value(
    desired: DesiredRepositoryState, name: str, default: FactValue
) -> FactValue:
    matches = tuple(
        fact
        for fact in desired.facts
        if fact.scope is ConsistencyScope.REPOSITORY
        and fact.subject_id is None
        and fact.name == name
    )
    return matches[0].value if len(matches) == 1 else default


def _tasks(observed: ObservedRepositoryState) -> tuple[ScopedObservations, ...]:
    return tuple(
        sorted(
            (
                scope
                for scope in observed.observations
                if scope.scope is ConsistencyScope.TASK and scope.subject_id is not None
            ),
            key=lambda scope: scope.subject_id or "",
        )
    )


def _repository_scope(
    observed: ObservedRepositoryState,
) -> ScopedObservations | None:
    matches = tuple(
        scope
        for scope in observed.observations
        if scope.scope is ConsistencyScope.REPOSITORY
    )
    return matches[0] if len(matches) == 1 else None


def _observation_details(fact: Observation) -> tuple[str, ...]:
    return (
        f"fact={fact.name}",
        f"source={fact.source}",
        f"certainty={fact.certainty.value}",
        *fact.diagnostics,
    )


def _unknown_finding(
    code: str,
    fact: Observation,
    *,
    scope: ConsistencyScope,
    subject_id: str | None = None,
) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=code,
        scope=scope,
        subject_id=subject_id,
        severity=FindingSeverity.WARNING,
        expected=Evidence(
            "observation is current and unambiguous",
            (f"fact={fact.name}",),
        ),
        observed=Evidence(
            f"observation certainty is {fact.certainty.value}",
            _observation_details(fact),
            fact.value,
        ),
        repairability=Repairability.NONE,
    )


def _task_finding(
    code: str,
    task: ScopedObservations,
    fact: Observation,
    *,
    expected: FactValue,
    expected_summary: str,
    observed_summary: str,
    repairability: Repairability,
    severity: FindingSeverity = FindingSeverity.ERROR,
) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=code,
        scope=ConsistencyScope.TASK,
        subject_id=task.subject_id,
        severity=severity,
        expected=Evidence(expected_summary, value=expected),
        observed=Evidence(
            observed_summary,
            _observation_details(fact),
            fact.value,
        ),
        repairability=repairability,
    )


def _known_value(task: ScopedObservations, name: str) -> FactValue:
    fact = _fact(task, name)
    return fact.value if fact is not None and fact.certainty is _KNOWN else None


def _is_active(task: ScopedObservations) -> bool:
    return _known_value(task, FACT_EXECUTION_KIND) in _ACTIVE_KINDS


def _ownership_finding(
    code: str, resource: FactValue, owners: tuple[str, ...]
) -> ConsistencyFinding:
    return ConsistencyFinding(
        code=code,
        scope=ConsistencyScope.REPOSITORY,
        severity=FindingSeverity.ERROR,
        expected=Evidence("resource has exactly one task owner", value=resource),
        observed=Evidence(
            "resource is owned by multiple tasks",
            tuple(f"owner={owner}" for owner in owners),
            (resource, *owners),
        ),
        repairability=Repairability.MANUAL,
    )


def _collisions(
    tasks: tuple[ScopedObservations, ...],
    code: str,
    identity: Callable[[ScopedObservations], FactValue],
) -> tuple[ConsistencyFinding, ...]:
    owners_by_resource: dict[FactValue, list[str]] = {}
    for task in tasks:
        resource = identity(task)
        if resource is None or task.subject_id is None:
            continue
        owners_by_resource.setdefault(resource, []).append(task.subject_id)
    return tuple(
        _ownership_finding(code, resource, tuple(sorted(owners)))
        for resource, owners in sorted(
            owners_by_resource.items(), key=lambda item: repr(item[0])
        )
        if len(set(owners)) > 1
    )


def _execution_identity(task: ScopedObservations) -> FactValue:
    kind = _known_value(task, FACT_EXECUTION_KIND)
    if kind == EXECUTION_KIND_LOCAL:
        pid = _known_value(task, FACT_EXECUTION_PID)
        return None if pid is None else ("local", pid)
    if kind == EXECUTION_KIND_CLOUD:
        external_id = _known_value(task, FACT_EXECUTION_EXTERNAL_ID)
        return None if external_id is None else ("cloud", external_id)
    return None


def _ownership_findings(
    tasks: tuple[ScopedObservations, ...],
) -> tuple[ConsistencyFinding, ...]:
    return (
        *_issue_ownership_findings(tasks),
        *_collisions(tasks, BRANCH_OWNERSHIP_CONFLICT, _branch_identity),
        *_collisions(tasks, WORKTREE_OWNERSHIP_CONFLICT, _worktree_identity),
        *_collisions(tasks, ACTIVE_EXECUTION_OWNERSHIP_CONFLICT, _execution_identity),
    )


def _issue_ownership_findings(
    tasks: tuple[ScopedObservations, ...],
) -> tuple[ConsistencyFinding, ...]:
    counts: dict[str, int] = {}
    for task in tasks:
        if task.subject_id is not None:
            counts[task.subject_id] = counts.get(task.subject_id, 0) + 1
    return tuple(
        _ownership_finding(
            ISSUE_OWNERSHIP_CONFLICT,
            issue,
            tuple(f"snapshot-entry-{index}" for index in range(1, count + 1)),
        )
        for issue, count in sorted(counts.items())
        if count > 1
    )


def _branch_identity(task: ScopedObservations) -> FactValue:
    return _known_value(task, FACT_BRANCH_NAME)


def _worktree_identity(task: ScopedObservations) -> FactValue:
    return _known_value(task, FACT_WORKTREE_PATH)


def _forge_findings(
    observed: ObservedRepositoryState,
) -> tuple[ConsistencyFinding, ...]:
    repository = _repository_scope(observed)
    fact = None if repository is None else _fact(repository, FACT_FORGE_REACHABLE)
    if fact is None or fact.certainty is _KNOWN:
        return ()
    return (
        _unknown_finding(
            FORGE_OBSERVATION_UNKNOWN,
            fact,
            scope=ConsistencyScope.REPOSITORY,
        ),
    )


def _repository_findings(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> tuple[ConsistencyFinding, ...]:
    del desired
    tasks = _tasks(observed)
    return (*_forge_findings(observed), *_ownership_findings(tasks))


def _uncertain_execution_findings(
    task: ScopedObservations,
) -> tuple[ConsistencyFinding, ...]:
    kind = _fact(task, FACT_EXECUTION_KIND)
    if kind is None:
        return ()
    if kind.certainty is not _KNOWN:
        return (
            _unknown_finding(
                EXECUTION_OBSERVATION_UNKNOWN,
                kind,
                scope=ConsistencyScope.TASK,
                subject_id=task.subject_id,
            ),
        )
    if kind.value not in _ACTIVE_KINDS:
        return ()
    names = [
        FACT_ISSUE_STATE,
        FACT_BRANCH_NAME,
        FACT_BRANCH_EXISTS,
        FACT_WORKTREE_PATH,
        FACT_WORKTREE_EXISTS,
    ]
    names.append(
        FACT_EXECUTION_EXTERNAL_STATUS
        if kind.value == EXECUTION_KIND_CLOUD
        else FACT_EXECUTION_PROCESS_ALIVE
    )
    uncertain = tuple(
        fact
        for name in names
        if (fact := _fact(task, name)) is not None and fact.certainty is not _KNOWN
    )
    return tuple(
        _unknown_finding(
            EXECUTION_OBSERVATION_UNKNOWN,
            fact,
            scope=ConsistencyScope.TASK,
            subject_id=task.subject_id,
        )
        for fact in uncertain
    )


def _local_process_findings(
    task: ScopedObservations,
    *,
    zombie_gc_enabled: bool,
) -> tuple[ConsistencyFinding, ...]:
    if (
        not zombie_gc_enabled
        or _known_value(task, FACT_EXECUTION_KIND) != EXECUTION_KIND_LOCAL
    ):
        return ()
    alive = _fact(task, FACT_EXECUTION_PROCESS_ALIVE)
    if alive is None or alive.certainty is not _KNOWN or alive.value is not False:
        return ()
    return (
        _task_finding(
            LOCAL_PROCESS_DEAD,
            task,
            alive,
            expected=True,
            expected_summary="recorded local execution process is alive",
            observed_summary="recorded local execution process is dead",
            repairability=Repairability.AUTOMATIC,
        ),
    )


def _missing_resource_finding(
    task: ScopedObservations,
    fact_name: str,
    code: str,
    resource: str,
    *,
    zombie_gc_enabled: bool,
) -> tuple[ConsistencyFinding, ...]:
    if not zombie_gc_enabled or not _is_active(task):
        return ()
    fact = _fact(task, fact_name)
    if fact is None or fact.certainty is not _KNOWN or fact.value is not False:
        return ()
    return (
        _task_finding(
            code,
            task,
            fact,
            expected=True,
            expected_summary=f"recorded execution {resource} exists",
            observed_summary=f"recorded execution {resource} is missing",
            repairability=Repairability.AUTOMATIC,
        ),
    )


def _orphan_findings(
    task: ScopedObservations, *, zombie_gc_enabled: bool
) -> tuple[ConsistencyFinding, ...]:
    if not zombie_gc_enabled or not _is_active(task):
        return ()
    issue = _fact(task, FACT_ISSUE_STATE)
    if issue is None or issue.certainty is not _KNOWN or issue.value is not None:
        return ()
    return (
        _task_finding(
            ORPHAN_EXECUTION,
            task,
            issue,
            expected="OPEN or CLOSED",
            expected_summary="execution belongs to an existing Issue",
            observed_summary="execution has no corresponding Issue",
            repairability=Repairability.AUTOMATIC,
        ),
    )


def _timeout_findings(
    task: ScopedObservations,
    observed: ObservedRepositoryState,
    desired: DesiredRepositoryState,
) -> tuple[ConsistencyFinding, ...]:
    kind = _fact(task, FACT_EXECUTION_KIND)
    if kind is None or kind.value not in {*_ACTIVE_KINDS, EXECUTION_KIND_UNKNOWN}:
        return ()
    timeout = _desired_repository_value(desired, DESIRED_TASK_TIMEOUT_SECONDS, 0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or timeout <= 0
    ):
        return ()
    started_at = _fact(task, FACT_EXECUTION_STARTED_AT)
    if (
        started_at is None
        or started_at.certainty is not _KNOWN
        or isinstance(started_at.value, bool)
        or not isinstance(started_at.value, int | float)
    ):
        return ()
    elapsed = observed.observed_at.timestamp() - started_at.value
    if elapsed <= timeout:
        return ()
    return (
        _task_finding(
            EXECUTION_TIMED_OUT,
            task,
            started_at,
            expected=timeout,
            expected_summary="execution elapsed time stays within configured timeout",
            observed_summary="recorded execution exceeded configured timeout",
            repairability=Repairability.AUTOMATIC,
        ),
    )


def _self_healed_orphan_findings(
    task: ScopedObservations, *, zombie_gc_enabled: bool
) -> tuple[ConsistencyFinding, ...]:
    """Recognize a handleless recovery record from known persisted facts."""
    if not zombie_gc_enabled:
        return ()
    kind = _fact(task, FACT_EXECUTION_KIND)
    if kind is None or kind.value != EXECUTION_KIND_UNKNOWN:
        return ()
    expected_values = {
        FACT_EXECUTION_PID: None,
        FACT_EXECUTION_EXTERNAL_ID: None,
        FACT_EXECUTION_STARTED_AT: None,
        FACT_WORKTREE_EXISTS: False,
    }
    facts = {name: _fact(task, name) for name in expected_values}
    if any(
        fact is None
        or fact.certainty is not _KNOWN
        or fact.value != expected_values[name]
        for name, fact in facts.items()
    ):
        return ()
    worktree = facts[FACT_WORKTREE_EXISTS]
    assert worktree is not None
    return (
        _task_finding(
            HANDLELESS_EXECUTION_ORPHAN,
            task,
            worktree,
            expected=True,
            expected_summary="self-healed execution worktree exists",
            observed_summary="handleless self-healed execution is orphaned",
            repairability=Repairability.AUTOMATIC,
        ),
    )


def _run_state_findings(
    task: ScopedObservations, desired: DesiredRepositoryState
) -> tuple[ConsistencyFinding, ...]:
    if task.subject_id is None:
        return ()
    expected = _desired_fact(desired, task.subject_id, DESIRED_RUN_STATE_ACTIVE)
    if expected is None or not isinstance(expected.value, bool):
        return ()
    kind = _fact(task, FACT_EXECUTION_KIND)
    if kind is None or kind.certainty is not _KNOWN:
        return ()
    active = kind.value in _ACTIVE_KINDS
    if expected.value == active:
        return ()
    code = RUN_STATE_MISSING if expected.value else RUN_STATE_STALE
    summary = "run state is missing" if expected.value else "run state is stale"
    return (
        _task_finding(
            code,
            task,
            kind,
            expected=expected.value,
            expected_summary="run-state activity matches desired task state",
            observed_summary=summary,
            repairability=Repairability.AUTOMATIC,
        ),
    )


def _pr_association_findings(
    task: ScopedObservations,
) -> tuple[ConsistencyFinding, ...]:
    number = _fact(task, FACT_PULL_REQUEST_NUMBER)
    uncertain = [] if number is None or number.certainty is _KNOWN else [number]
    if number is not None and number.certainty is _KNOWN and number.value is not None:
        uncertain.extend(
            fact
            for name in (FACT_PULL_REQUEST_HEAD_REF, FACT_PULL_REQUEST_BASE_REF)
            if (fact := _fact(task, name)) is not None and fact.certainty is not _KNOWN
        )
    return tuple(
        _unknown_finding(
            PULL_REQUEST_ASSOCIATION_UNKNOWN,
            fact,
            scope=ConsistencyScope.TASK,
            subject_id=task.subject_id,
        )
        for fact in uncertain
    )


def _pr_mismatch(
    task: ScopedObservations,
    *,
    fact_name: str,
    expected: FactValue,
    code: str,
    label: str,
) -> tuple[ConsistencyFinding, ...]:
    fact = _fact(task, fact_name)
    if fact is None or fact.certainty is not _KNOWN or fact.value == expected:
        return ()
    return (
        _task_finding(
            code,
            task,
            fact,
            expected=expected,
            expected_summary=f"pull request {label} matches task ownership",
            observed_summary=f"pull request {label} does not match task ownership",
            repairability=Repairability.MANUAL,
        ),
    )


def _pr_mismatch_findings(
    task: ScopedObservations,
) -> tuple[ConsistencyFinding, ...]:
    number = _fact(task, FACT_PULL_REQUEST_NUMBER)
    if number is None or number.certainty is not _KNOWN or number.value is None:
        return ()
    branch = _fact(task, FACT_BRANCH_NAME)
    parent = _fact(task, FACT_PARENT_ISSUE_NUMBER)
    findings: list[ConsistencyFinding] = []
    if (
        branch is not None
        and branch.certainty is _KNOWN
        and isinstance(branch.value, str)
    ):
        findings.extend(
            _pr_mismatch(
                task,
                fact_name=FACT_PULL_REQUEST_HEAD_REF,
                expected=branch.value,
                code=PULL_REQUEST_HEAD_MISMATCH,
                label="head",
            )
        )
    if parent is not None and parent.certainty is _KNOWN:
        if isinstance(parent.value, int) and not isinstance(parent.value, bool):
            expected_base = f"parent/issue-{parent.value}"
        elif parent.value is None:
            expected_base = "main"
        else:
            return tuple(findings)
        findings.extend(
            _pr_mismatch(
                task,
                fact_name=FACT_PULL_REQUEST_BASE_REF,
                expected=expected_base,
                code=PULL_REQUEST_BASE_MISMATCH,
                label="base",
            )
        )
    return tuple(findings)


def _one_task_findings(
    task: ScopedObservations,
    observed: ObservedRepositoryState,
    desired: DesiredRepositoryState,
) -> tuple[ConsistencyFinding, ...]:
    zombie_gc_enabled = _desired_repository_value(
        desired, DESIRED_ZOMBIE_GC_ENABLED, True
    )
    zombie_gc_enabled = (
        zombie_gc_enabled if isinstance(zombie_gc_enabled, bool) else False
    )
    return (
        *_uncertain_execution_findings(task),
        *_local_process_findings(task, zombie_gc_enabled=zombie_gc_enabled),
        *_timeout_findings(task, observed, desired),
        *_self_healed_orphan_findings(task, zombie_gc_enabled=zombie_gc_enabled),
        *_missing_resource_finding(
            task,
            FACT_BRANCH_EXISTS,
            BRANCH_MISSING,
            "branch",
            zombie_gc_enabled=zombie_gc_enabled,
        ),
        *_missing_resource_finding(
            task,
            FACT_WORKTREE_EXISTS,
            WORKTREE_MISSING,
            "worktree",
            zombie_gc_enabled=zombie_gc_enabled,
        ),
        *_orphan_findings(task, zombie_gc_enabled=zombie_gc_enabled),
        *_run_state_findings(task, desired),
        *_pr_association_findings(task),
        *_pr_mismatch_findings(task),
    )


def _task_findings(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> tuple[ConsistencyFinding, ...]:
    return tuple(
        finding
        for task in _tasks(observed)
        for finding in _one_task_findings(task, observed, desired)
    )


type _Evaluator = Callable[
    [ObservedRepositoryState, DesiredRepositoryState],
    tuple[ConsistencyFinding, ...],
]


@dataclass(frozen=True, slots=True)
class _ExecutionInvariant(Invariant):
    code: str
    scope: ConsistencyScope
    evaluator: _Evaluator

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]:
        return self.evaluator(observed, desired)


def execution_invariants() -> tuple[Invariant, ...]:
    """Return deterministic pure invariants for repository and task scope."""
    return (
        _ExecutionInvariant(
            "execution.repository-ownership",
            ConsistencyScope.REPOSITORY,
            _repository_findings,
        ),
        _ExecutionInvariant(
            "execution.task-state",
            ConsistencyScope.TASK,
            _task_findings,
        ),
    )


__all__ = [
    "ACTIVE_EXECUTION_OWNERSHIP_CONFLICT",
    "BRANCH_MISSING",
    "BRANCH_OWNERSHIP_CONFLICT",
    "EXECUTION_OBSERVATION_UNKNOWN",
    "EXECUTION_TIMED_OUT",
    "FACT_PULL_REQUEST_BASE_REF",
    "FACT_PULL_REQUEST_HEAD_REF",
    "FORGE_OBSERVATION_UNKNOWN",
    "HANDLELESS_EXECUTION_ORPHAN",
    "ISSUE_OWNERSHIP_CONFLICT",
    "LOCAL_PROCESS_DEAD",
    "ORPHAN_EXECUTION",
    "REQUIRED_OBSERVED_FACT_NAMES_BY_SCOPE",
    "PULL_REQUEST_ASSOCIATION_UNKNOWN",
    "PULL_REQUEST_BASE_MISMATCH",
    "PULL_REQUEST_HEAD_MISMATCH",
    "RUN_STATE_MISSING",
    "RUN_STATE_STALE",
    "WORKTREE_MISSING",
    "WORKTREE_OWNERSHIP_CONFLICT",
    "execution_invariants",
]
