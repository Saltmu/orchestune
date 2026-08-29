"""tests/test_consistency_status_*.py 群が共有するビルダーと定数。

#705 の status policy テストを Invariant 側 (`test_consistency_status_policy.py`)
と Planner 側 (`test_consistency_status_repairs.py`) へ分割した際、両者から共通
利用される観測・desired state・Intent のビルダーをこのモジュールへ切り出した。
`test_` で始まらないため pytest には収集されない。

観測ビルダーは `ObservationCollector` が実際に出力する形（scope の並び、fact 名、
ラベルのソートと `status:` 接頭辞による絞り込み）を写し取っている。desired state
は #702 の `derive_desired_repository_state` を直接呼ぶため、fact 名がドリフト
した場合はテストが機械的に落ちる。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.invariants.status import status_invariants
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    IntentStatus,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    ScopedObservations,
    TransitionIntent,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_NONE,
    FACT_EXECUTION_KIND,
    FACT_FORGE_REACHABLE,
    FACT_ISSUE_LABELS,
    FACT_ISSUE_STATE,
    FACT_ISSUE_STATUS_LABELS,
)

REPOSITORY = "Saltmu/orchestune"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
KNOWN = ObservationCertainty.KNOWN
UNKNOWN = ObservationCertainty.UNKNOWN


def _observation(
    name: str,
    value: object,
    *,
    certainty: ObservationCertainty = KNOWN,
) -> Observation:
    return Observation(
        name=name,
        value=None if certainty is not KNOWN else value,  # type: ignore[arg-type]
        certainty=certainty,
        source="forge",
        observed_at=NOW,
        diagnostics=() if certainty is KNOWN else ("probe failed",),
    )


def _task_scope(
    issue_number: int,
    *,
    labels: Sequence[str] = ("status:queued",),
    issue_state: str | None = "OPEN",
    execution_kind: str = EXECUTION_KIND_NONE,
    uncertain: Sequence[str] = (),
    omit: Sequence[str] = (),
) -> ScopedObservations:
    """Mirror what `ObservationCollector` emits for one task Issue."""
    all_labels = tuple(sorted(set(labels)))
    values: dict[str, object] = {
        FACT_EXECUTION_KIND: execution_kind,
        FACT_ISSUE_LABELS: all_labels,
        FACT_ISSUE_STATE: issue_state,
        FACT_ISSUE_STATUS_LABELS: tuple(
            label for label in all_labels if label.startswith("status:")
        ),
    }
    facts = tuple(
        _observation(
            name,
            value,
            certainty=UNKNOWN if name in uncertain else KNOWN,
        )
        for name, value in sorted(values.items())
        if name not in omit
    )
    return ScopedObservations(
        scope=ConsistencyScope.TASK, subject_id=str(issue_number), facts=facts
    )


def _repository_scope(*facts: Observation) -> ScopedObservations:
    return ScopedObservations(scope=ConsistencyScope.REPOSITORY, facts=facts)


def _reachable(
    value: object = True, *, certainty: ObservationCertainty = KNOWN
) -> Observation:
    return Observation(
        name=FACT_FORGE_REACHABLE,
        value=value,  # type: ignore[arg-type]
        certainty=certainty,
        source="forge",
        observed_at=NOW,
        diagnostics=() if certainty is KNOWN else ("forge probe failed",),
    )


def _observed(
    *tasks: ScopedObservations,
    forge_certainty: ObservationCertainty = KNOWN,
) -> ObservedRepositoryState:
    repository = ScopedObservations(
        scope=ConsistencyScope.REPOSITORY,
        facts=(_observation(FACT_FORGE_REACHABLE, True, certainty=forge_certainty),),
    )
    return ObservedRepositoryState(
        repository_id=REPOSITORY,
        observed_at=NOW,
        observations=(repository, *tasks),
    )


def _desired_task(
    task_id: str,
    issue_number: int,
    *,
    depends_on: tuple[str, ...] = (),
    lifecycle: TaskLifecycle = TaskLifecycle.OPEN,
    forced_serial: bool = False,
) -> DesiredTaskInput:
    return DesiredTaskInput(
        task_id=task_id,
        subject_id=str(issue_number),
        depends_on=depends_on,
        lifecycle=lifecycle,
        forced_serial=forced_serial,
    )


def _desired(
    *tasks: DesiredTaskInput,
    active: tuple[str, ...] = (),
    completed: tuple[str, ...] = (),
    intents: tuple[TransitionIntent, ...] = (),
    max_concurrent: int = 3,
) -> DesiredRepositoryState:
    """Derive desired state with the real #702 derivation, not a stand-in."""
    return derive_desired_repository_state(
        REPOSITORY,
        tasks,
        active_task_ids=active,
        completed_task_ids=completed,
        policy=DispatchPolicy(max_concurrent=max_concurrent),
        intents=intents,
        now=NOW,
    )


def _status_intent(
    issue_number: int,
    *,
    status: IntentStatus = IntentStatus.APPLIED,
    expires_at: datetime | None = None,
    changed_fact: str = "task.status_label",
    scope: ConsistencyScope = ConsistencyScope.TASK,
    change_scope: ConsistencyScope = ConsistencyScope.TASK,
    subject_id: str | None = None,
    created_at: datetime | None = None,
) -> TransitionIntent:
    subject = str(issue_number) if subject_id is None else subject_id
    return TransitionIntent(
        intent_id=f"intent-{issue_number}",
        scope=scope,
        subject_id=subject,
        operation="transition-status",
        created_at=NOW - timedelta(minutes=1) if created_at is None else created_at,
        status=status,
        expires_at=expires_at,
        expected_changes=(
            DesiredFact(
                name=changed_fact,
                value="status:in-progress",
                scope=change_scope,
                subject_id=str(issue_number),
                reason="launch in flight",
            ),
        ),
    )


def _with_intents(
    desired: DesiredRepositoryState, *intents: TransitionIntent
) -> DesiredRepositoryState:
    """Attach intents the #702 derivation would have filtered out on its own."""
    return DesiredRepositoryState(
        repository_id=desired.repository_id,
        facts=desired.facts,
        transition_intents=intents,
    )


def _evaluate(
    observed: ObservedRepositoryState, desired: DesiredRepositoryState
) -> ConsistencyReport:
    return ConsistencyEngine(status_invariants()).evaluate(observed, desired)


def _codes(report: ConsistencyReport) -> tuple[str, ...]:
    return tuple(finding.code for finding in report.findings)


def _only(report: ConsistencyReport, code: str) -> ConsistencyFinding:
    matches = [finding for finding in report.findings if finding.code == code]
    assert len(matches) == 1, _codes(report)
    return matches[0]
