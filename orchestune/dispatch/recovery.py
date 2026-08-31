"""run_state.json消失時・不整合時の自己修復（self-healing）処理。"""

from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from orchestune.consistency.invariants.execution import (
    RUN_STATE_MISSING,
    execution_invariants,
)
from orchestune.consistency.models import (
    ConsistencyFinding,
    ConsistencyReport,
    ConsistencyScope,
    DesiredFact,
    DesiredRepositoryState,
    Evidence,
    FindingSeverity,
    Observation,
    ObservationCertainty,
    ObservedRepositoryState,
    Repairability,
    RepairCommand,
    RepairResult,
    RepairStatus,
    ScopedObservations,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_REQUEUE,
    plan_execution_repairs,
)
from orchestune.dispatch.execution_profiles import resolve_execution_profile
from orchestune.dispatch.execution_repair import (
    collect_execution_observed_state,
    command_finding_codes,
    derive_execution_desired_state,
)
from orchestune.dispatch.labels import (
    PRIMARY_STATUS_LABELS,
    TERMINAL_ESCALATION_LABELS,
    transition_status_label,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState, save_run_state
from orchestune.issue_parsing import (
    FOOTPRINT_BLOCK_PATTERN,
    launch_history_from_body,
    launch_history_in_window,
    parse_task_from_issue,
    recovery_counters_from_body,
)
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, PrRecord
from orchestune.pr_link_notice import pr_matches_issue

if TYPE_CHECKING:
    from orchestune.dispatch.config import DispatcherConfig

_FORCE_SERIAL_LABEL = StatusLabel.FORCE_SERIAL

FACT_RECOVERY_COUNTERS = "dispatch.recovery-counters"
FACT_LAUNCH_HISTORY = "dispatch.launch-history"
RECOVERY_COUNTERS_STALE = "execution.recovery-counters-stale"
LAUNCH_HISTORY_STALE = "execution.launch-history-stale"

type _Restoration = tuple[str, str, ActiveWorktree]
type _CounterTarget = tuple[str, int, bool]


@dataclass(frozen=True, slots=True)
class RecoveryBookkeepingSnapshot:
    """Authoritative inputs retained for one typed repair pass."""

    tasks_by_issue: Mapping[int, Task]
    open_prs: tuple[PrRecord, ...]
    restorations: tuple[_Restoration, ...]
    counter_targets: tuple[_CounterTarget, ...]
    launch_history: tuple[float, ...]


def _scope_order(scope: ConsistencyScope) -> int:
    return {
        ConsistencyScope.REPOSITORY: 0,
        ConsistencyScope.PARENT: 1,
        ConsistencyScope.TASK: 2,
    }[scope]


def _with_observations(
    observed: ObservedRepositoryState,
    additions: Sequence[tuple[ConsistencyScope, str | None, Observation]],
) -> ObservedRepositoryState:
    grouped = {
        (scope.scope, scope.subject_id): {fact.name: fact for fact in scope.facts}
        for scope in observed.observations
    }
    for scope, subject_id, fact in additions:
        grouped.setdefault((scope, subject_id), {})[fact.name] = fact
    scopes = tuple(
        ScopedObservations(
            scope=scope,
            subject_id=subject_id,
            facts=tuple(
                grouped[(scope, subject_id)][name]
                for name in sorted(grouped[(scope, subject_id)])
            ),
        )
        for scope, subject_id in sorted(
            grouped, key=lambda item: (_scope_order(item[0]), item[1] or "")
        )
    )
    return ObservedRepositoryState(
        repository_id=observed.repository_id,
        observed_at=observed.observed_at,
        observations=scopes,
    )


def _with_desired_facts(
    desired: DesiredRepositoryState, additions: Sequence[DesiredFact]
) -> DesiredRepositoryState:
    facts = (*desired.facts, *additions)
    return DesiredRepositoryState(
        repository_id=desired.repository_id,
        facts=tuple(
            sorted(
                facts,
                key=lambda fact: (
                    _scope_order(fact.scope),
                    fact.subject_id or "",
                    fact.name,
                ),
            )
        ),
        transition_intents=desired.transition_intents,
    )


def _observed_fact(
    observed: ObservedRepositoryState,
    desired: DesiredFact,
) -> Observation | None:
    return next(
        (
            fact
            for scope in observed.observations
            if scope.scope is desired.scope and scope.subject_id == desired.subject_id
            for fact in scope.facts
            if fact.name == desired.name
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class _RecoveryBookkeepingInvariant:
    code: str = "execution.recovery-bookkeeping"
    scope: ConsistencyScope = ConsistencyScope.REPOSITORY

    def evaluate(
        self,
        observed: ObservedRepositoryState,
        desired: DesiredRepositoryState,
    ) -> tuple[ConsistencyFinding, ...]:
        findings = []
        for expected in desired.facts:
            if expected.name not in {FACT_RECOVERY_COUNTERS, FACT_LAUNCH_HISTORY}:
                continue
            actual = _observed_fact(observed, expected)
            if actual is None or actual.certainty is not ObservationCertainty.KNOWN:
                continue
            if actual.value == expected.value:
                continue
            findings.append(
                ConsistencyFinding(
                    code=(
                        RECOVERY_COUNTERS_STALE
                        if expected.name == FACT_RECOVERY_COUNTERS
                        else LAUNCH_HISTORY_STALE
                    ),
                    scope=expected.scope,
                    subject_id=expected.subject_id,
                    severity=FindingSeverity.WARNING,
                    expected=Evidence(
                        summary="recovery bookkeeping matches durable state",
                        value=expected.value,
                    ),
                    observed=Evidence(
                        summary="recovery bookkeeping is stale",
                        value=actual.value,
                    ),
                    repairability=Repairability.AUTOMATIC,
                )
            )
        return tuple(findings)


def recovery_bookkeeping_invariants():
    return (*execution_invariants(), _RecoveryBookkeepingInvariant())


def _bookkeeping_command(finding: ConsistencyFinding) -> RepairCommand:
    subject = finding.subject_id or "repository"
    return RepairCommand(
        code=COMMAND_BOOKKEEPING,
        scope=finding.scope,
        subject_id=finding.subject_id,
        idempotency_key=f"recovery:{subject}:bookkeeping",
        parameters=(("finding_codes", (finding.code,)),),
        preconditions=("finding-certainty:known",),
    )


def plan_recovery_bookkeeping_repairs(
    report: ConsistencyReport,
) -> tuple[RepairCommand, ...]:
    """Plan only startup recovery commands; GC execution remains separately owned."""
    commands = [
        command
        for command in plan_execution_repairs(report)
        if command.code in {COMMAND_REQUEUE, COMMAND_BOOKKEEPING}
        and set(command_finding_codes(command)) == {RUN_STATE_MISSING}
    ]
    commands.extend(
        _bookkeeping_command(finding)
        for finding in report.findings
        if finding.code in {RECOVERY_COUNTERS_STALE, LAUNCH_HISTORY_STALE}
    )
    return tuple(commands)


def _extract_raw_subtask_id(issue: IssueRecord) -> str | None:
    """Issue本文のFootprint YAMLブロックから、素のsubtask_id（未検出ならNone）を取り出す。

    呼び出し側ごとにNone時のフォールバック方針が異なる（自己修復ブランチ名生成では
    合成IDへフォールバックするが、依存解決用マップでは未検出issueを含めない）ため、
    フォールバックを持たない共通の抽出処理として切り出している。
    """
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    subtask_id = data.get("subtask_id")
    return str(subtask_id) if subtask_id else None


def _parse_subtask_info_from_issue(
    issue: IssueRecord,
) -> tuple[str, tuple[str, ...]]:
    """Issueの本文から subtask_id と declared_footprint を抽出する。"""
    match = FOOTPRINT_BLOCK_PATTERN.search(issue.body)
    subtask_id = _extract_raw_subtask_id(issue)
    declared_footprint: tuple[str, ...] = ()
    if match:
        try:
            data = yaml.safe_load(match.group(1))
            if isinstance(data, dict):
                footprint = data.get("footprint", [])
                if isinstance(footprint, list):
                    declared_footprint = tuple(footprint)
        except Exception:
            pass

    if not subtask_id:
        subtask_id = f"issue-{issue.number}"

    return subtask_id, declared_footprint


def _dependency_issue_numbers(
    issue: IssueRecord,
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
) -> tuple[int, ...]:
    """自己修復に使う依存Issue番号をnative関係またはYAMLから解決する。"""
    if issue.blocked_by:
        return issue.blocked_by

    task = parse_task_from_issue(issue, issue_to_subtask_id)
    return tuple(
        subtask_id_to_issue_number[subtask_id]
        for subtask_id in task.depends_on
        if subtask_id in subtask_id_to_issue_number
    )


def _restored_base_branch(
    issue: IssueRecord,
    open_prs: list[PrRecord],
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
) -> str:
    """Issueの親・依存関係から自己修復時のbase branchを決定する。"""
    base_branch = "origin/main"
    if issue.parent and issue.parent.get("number") is not None:
        base_branch = f"parent/issue-{issue.parent['number']}"

    dependency_issue_numbers = _dependency_issue_numbers(
        issue,
        issue_to_subtask_id,
        subtask_id_to_issue_number,
    )
    for pr in open_prs:
        if any(
            pr_matches_issue(pr, dep_num, issue_to_subtask_id.get(dep_num))
            for dep_num in dependency_issue_numbers
        ):
            return pr.head_ref

    return base_branch


def _recovery_counters_for_issue(issue: IssueRecord) -> tuple[int, bool]:
    """#516レビュー指摘: Issue本文（Footprintフェンス）を第一のソースとしつつ、
    `status:force-serial`ラベルが付いているのに本文フィールドが無い/false
    のケースをラベル側の権威で補う。想定されるケースは2つ:
    (1) 本フィールド導入前からforced_serialだった移行時のIssue（本文に
        フィールドが無い）、(2) `_persist_recovery_counters`の本文書き込みは
        成功したがその後の`add_label`が失敗し、次のイベントまで本文が
        更新されないまま残ったケースの逆——ここでは扱わない（本文が
        `true`で確定していればそちらが優先される）。ラベルは`recompute_count`
        を持たないためforced_serial側のみのフォールバックとする。
    """
    recompute_count, forced_serial = recovery_counters_from_body(issue.body)
    if _FORCE_SERIAL_LABEL in issue.labels:
        forced_serial = True
    return (recompute_count, forced_serial)


def _tasks_from_issues(issues: Sequence[IssueRecord]) -> dict[int, Task]:
    issue_to_subtask_id = {
        issue.number: raw
        for issue in issues
        if (raw := _extract_raw_subtask_id(issue)) is not None
    }
    return {
        issue.number: parse_task_from_issue(issue, issue_to_subtask_id)
        for issue in issues
    }


def _counter_targets(
    run_state: RunState, issues: Sequence[IssueRecord]
) -> tuple[_CounterTarget, ...]:
    issues_by_number = {issue.number: issue for issue in issues}
    targets = []
    for key, active in sorted(run_state.active_worktrees.items()):
        issue = issues_by_number.get(active.issue_number)
        if issue is None:
            continue
        persisted_count, persisted_serial = _recovery_counters_for_issue(issue)
        targets.append(
            (
                key,
                max(active.recompute_count, persisted_count),
                active.forced_serial or persisted_serial,
            )
        )
    return tuple(targets)


def _merged_launch_history(
    run_state: RunState,
    parent_issue: IssueRecord | None,
    *,
    now: float,
    window_seconds: int,
) -> tuple[float, ...]:
    persisted: list[float] = (
        [] if parent_issue is None else launch_history_from_body(parent_issue.body)
    )
    in_window = launch_history_in_window(persisted, now, window_seconds)
    local_counts = Counter(run_state.launch_history)
    persisted_counts = Counter(in_window)
    merged_counts = Counter(
        {
            timestamp: max(local_counts[timestamp], persisted_counts[timestamp])
            for timestamp in set(local_counts) | set(persisted_counts)
        }
    )
    return tuple(sorted(merged_counts.elements()))


def _resolve_recovery_pr_and_branch(
    issue: IssueRecord,
    subtask_id: str,
    open_prs: list[PrRecord],
) -> tuple[str, str | None, str | None]:
    for pr in open_prs:
        if pr_matches_issue(pr, issue.number, subtask_id):
            return pr.head_ref, str(pr.number), f"PR#{pr.number}"
    return f"claude/issue-{issue.number}-{subtask_id}", None, None


def _build_restored_active_worktree(
    issue: IssueRecord,
    subtask_id: str,
    declared_footprint: tuple[str, ...],
    open_prs: list[PrRecord],
    issue_to_subtask_id: dict[int, str],
    subtask_id_to_issue_number: dict[str, int],
    config: DispatcherConfig,
) -> ActiveWorktree:
    recompute_count, forced_serial = _recovery_counters_for_issue(issue)
    branch_name, external_id, external_url = _resolve_recovery_pr_and_branch(
        issue, subtask_id, open_prs
    )
    slug = branch_name.replace("/", "-")
    worktree_path = Path(config.worktree_root) / slug
    restored_base = _restored_base_branch(
        issue, open_prs, issue_to_subtask_id, subtask_id_to_issue_number
    )

    task = parse_task_from_issue(issue, issue_to_subtask_id)
    execution_selection = resolve_execution_profile(
        task.execution_profile,
        config.dispatch_target,
        config.execution_profile_config,
    )

    return ActiveWorktree(
        issue_number=issue.number,
        branch=branch_name,
        worktree_path=str(worktree_path),
        pid=None,
        started_at=None,
        declared_footprint=declared_footprint,
        recompute_count=recompute_count,
        forced_serial=forced_serial,
        external_id=external_id,
        external_url=external_url,
        base_branch=restored_base,
        profile=execution_selection.profile,
        model=execution_selection.model,
        reasoning_effort=execution_selection.reasoning_effort,
        selection_reason=execution_selection.reason,
    )


def _restoration_candidates(
    issues: Sequence[IssueRecord],
    open_prs: Sequence[PrRecord],
    config: DispatcherConfig,
) -> tuple[_Restoration, ...]:
    issue_to_subtask_id = {
        issue.number: raw
        for issue in issues
        if (raw := _extract_raw_subtask_id(issue)) is not None
    }
    subtask_id_to_issue_number = {
        subtask_id: issue_number
        for issue_number, subtask_id in issue_to_subtask_id.items()
    }
    candidates = []
    for issue in issues:
        subtask_id, declared_footprint = _parse_subtask_info_from_issue(issue)
        active = _build_restored_active_worktree(
            issue,
            subtask_id,
            declared_footprint,
            list(open_prs),
            issue_to_subtask_id,
            subtask_id_to_issue_number,
            config,
        )
        candidates.append((str(issue.number), subtask_id, active))
    return tuple(candidates)


def _observation(
    name: str, value, observed_at: datetime, *, source: str
) -> Observation:
    return Observation(
        name=name,
        certainty=ObservationCertainty.KNOWN,
        source=source,
        observed_at=observed_at,
        value=value,
    )


def _bookkeeping_observations(
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    observed_at: datetime,
) -> list[tuple[ConsistencyScope, str | None, Observation]]:
    additions: list[tuple[ConsistencyScope, str | None, Observation]] = [
        (
            ConsistencyScope.REPOSITORY,
            None,
            _observation(
                FACT_LAUNCH_HISTORY,
                tuple(sorted(run_state.launch_history)),
                observed_at,
                source="run-state",
            ),
        )
    ]
    for key, _, _ in snapshot.counter_targets:
        active = run_state.active_worktrees.get(key)
        if active is not None:
            additions.append(
                (
                    ConsistencyScope.TASK,
                    str(active.issue_number),
                    _observation(
                        FACT_RECOVERY_COUNTERS,
                        (active.recompute_count, active.forced_serial),
                        observed_at,
                        source="run-state",
                    ),
                )
            )
    return additions


class RecoveryBookkeepingAdapter:
    """Fresh repository-wide startup observation for recovery bookkeeping."""

    def __init__(
        self,
        repository_id: str,
        run_state: RunState,
        config: DispatcherConfig,
        *,
        now: float,
    ) -> None:
        self._repository_id = repository_id
        self._run_state = run_state
        self._config = config
        self._now = now
        self._snapshot: RecoveryBookkeepingSnapshot | None = None

    @property
    def snapshot(self) -> RecoveryBookkeepingSnapshot:
        if self._snapshot is None:
            raise RuntimeError("recovery bookkeeping has not been observed")
        return self._snapshot

    @property
    def tasks_by_issue(self) -> Mapping[int, Task]:
        return self.snapshot.tasks_by_issue

    def _refresh_snapshot(self) -> RecoveryBookkeepingSnapshot:
        forge = self._config.resolved_forge
        issues = tuple(forge.list_issues_by_label(StatusLabel.IN_PROGRESS))
        open_prs = tuple(forge.list_open_prs())
        parent_issue = (
            forge.get_issue(self._config.parent_issue_number)
            if self._config.parent_issue_number is not None
            else None
        )
        self._snapshot = RecoveryBookkeepingSnapshot(
            tasks_by_issue=_tasks_from_issues(issues),
            open_prs=open_prs,
            restorations=_restoration_candidates(issues, open_prs, self._config),
            counter_targets=_counter_targets(self._run_state, issues),
            launch_history=_merged_launch_history(
                self._run_state,
                parent_issue,
                now=self._now,
                window_seconds=self._config.window_seconds,
            ),
        )
        return self._snapshot

    def observe(self) -> ObservedRepositoryState:
        snapshot = self._refresh_snapshot()
        observed_at = datetime.fromtimestamp(self._now, UTC)
        branches = {
            active.issue_number: active.branch for _, _, active in snapshot.restorations
        }
        base = collect_execution_observed_state(
            self._run_state,
            snapshot.tasks_by_issue,
            self._config,
            snapshot.open_prs,
            branches,
            self._repository_id,
            observed_at,
        )
        return _with_observations(
            base, _bookkeeping_observations(self._run_state, snapshot, observed_at)
        )

    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState:
        snapshot = self.snapshot
        desired = derive_execution_desired_state(
            snapshot.tasks_by_issue,
            self._config,
            self._repository_id,
            observed.observed_at,
        )
        additions = [
            DesiredFact(
                name=FACT_LAUNCH_HISTORY,
                value=snapshot.launch_history,
                scope=ConsistencyScope.REPOSITORY,
                reason="merge durable launch reservations monotonically",
            )
        ]
        for key, recompute_count, forced_serial in snapshot.counter_targets:
            active = self._run_state.active_worktrees.get(key)
            if active is None:
                continue
            additions.append(
                DesiredFact(
                    name=FACT_RECOVERY_COUNTERS,
                    value=(recompute_count, forced_serial),
                    scope=ConsistencyScope.TASK,
                    subject_id=str(active.issue_number),
                    reason="never roll recovery bookkeeping backward",
                )
            )
        return _with_desired_facts(desired, additions)


def _persist_recovery_snapshot(
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    config: DispatcherConfig,
) -> None:
    save_run_state(
        run_state,
        config.run_state_path,
        launch_window_seconds=config.window_seconds,
        open_prs=list(snapshot.open_prs),
    )


def _restorable(candidate: ActiveWorktree) -> bool:
    return candidate.external_id is not None


def _skipped(command: RepairCommand, detail: str) -> RepairResult:
    return RepairResult(
        command=command,
        status=RepairStatus.SKIPPED,
        diagnostics=(detail,),
    )


def execute_recovery_requeue_command(
    command: RepairCommand,
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    config: DispatcherConfig,
) -> RepairResult:
    """Requeue a task only when fresh recovery inputs expose no resumable resource."""
    if command.code != COMMAND_REQUEUE:
        return RepairResult(
            command=command,
            status=RepairStatus.FAILED,
            diagnostics=(f"unsupported recovery repair command: {command.code}",),
        )
    if not config.apply:
        return _skipped(command, "requeue is disabled in dry-run mode")
    selected = tuple(
        item for item in snapshot.restorations if item[0] == command.subject_id
    )
    if len(selected) != 1 or any(
        str(active.issue_number) == command.subject_id
        for active in run_state.active_worktrees.values()
    ):
        return _skipped(command, "requeue precondition no longer holds")
    if _restorable(selected[0][2]):
        return _skipped(command, "a resumable execution resource is available")
    labels = config.resolved_forge.get_issue_labels(selected[0][2].issue_number)
    if StatusLabel.IN_PROGRESS not in labels or any(
        label in labels for label in TERMINAL_ESCALATION_LABELS
    ):
        return _skipped(command, "task is no longer eligible for recovery requeue")
    transition_status_label(
        config.resolved_forge,
        selected[0][2].issue_number,
        StatusLabel.QUEUED,
        (label for label in PRIMARY_STATUS_LABELS if label in labels),
    )
    return RepairResult(command=command, status=RepairStatus.APPLIED)


def _apply_launch_history_bookkeeping(
    command: RepairCommand,
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    config: DispatcherConfig,
) -> RepairResult:
    previous = list(run_state.launch_history)
    merged = _merged_launch_history_values(previous, snapshot.launch_history)
    if merged == sorted(previous):
        return _skipped(command, "launch history already matches durable state")
    run_state.launch_history = merged
    if config.apply:
        try:
            _persist_recovery_snapshot(run_state, snapshot, config)
        except Exception:
            run_state.launch_history = previous
            raise
    return RepairResult(command=command, status=RepairStatus.APPLIED)


def _merged_launch_history_values(
    local: Sequence[float], persisted: Sequence[float]
) -> list[float]:
    local_counts = Counter(local)
    persisted_counts = Counter(persisted)
    merged = Counter(
        {
            value: max(local_counts[value], persisted_counts[value])
            for value in set(local_counts) | set(persisted_counts)
        }
    )
    return sorted(merged.elements())


def _apply_counter_bookkeeping(
    command: RepairCommand,
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    config: DispatcherConfig,
) -> RepairResult:
    selected = []
    for key, recompute_count, forced_serial in snapshot.counter_targets:
        active = run_state.active_worktrees.get(key)
        if active is not None and str(active.issue_number) == command.subject_id:
            selected.append((active, recompute_count, forced_serial))
    if len(selected) != 1 or not config.apply:
        return _skipped(command, "counter bookkeeping precondition no longer holds")
    active, recompute_count, forced_serial = selected[0]
    target = (
        max(active.recompute_count, recompute_count),
        active.forced_serial or forced_serial,
    )
    previous = (active.recompute_count, active.forced_serial)
    if target == previous:
        return _skipped(command, "recovery counters already match durable state")
    active.recompute_count, active.forced_serial = target
    try:
        _persist_recovery_snapshot(run_state, snapshot, config)
    except Exception:
        active.recompute_count, active.forced_serial = previous
        raise
    return RepairResult(command=command, status=RepairStatus.APPLIED)


def _apply_missing_entry_bookkeeping(
    command: RepairCommand,
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    config: DispatcherConfig,
) -> RepairResult:
    selected = tuple(
        item for item in snapshot.restorations if item[0] == command.subject_id
    )
    if len(selected) != 1 or not config.apply or not _restorable(selected[0][2]):
        return _skipped(command, "missing-entry precondition no longer holds")
    key, subtask_id, active = selected[0]
    labels = config.resolved_forge.get_issue_labels(active.issue_number)
    if (
        StatusLabel.IN_PROGRESS not in labels
        or key in run_state.active_worktrees
        or any(
            str(item.issue_number) == command.subject_id
            for item in run_state.active_worktrees.values()
        )
    ):
        return _skipped(command, "missing-entry precondition no longer holds")
    run_state.active_worktrees[key] = active
    try:
        _persist_recovery_snapshot(run_state, snapshot, config)
    except Exception:
        del run_state.active_worktrees[key]
        raise
    print(
        f"Self-healing: Restored active worktree state for subtask '{subtask_id}' "
        f"(Issue #{active.issue_number})",
        file=sys.stderr,
    )
    return RepairResult(command=command, status=RepairStatus.APPLIED)


def execute_bookkeeping_repair_command(
    command: RepairCommand,
    run_state: RunState,
    snapshot: RecoveryBookkeepingSnapshot,
    config: DispatcherConfig,
) -> RepairResult:
    """Apply one typed, observed, crash-safe recovery bookkeeping mutation."""
    if command.code != COMMAND_BOOKKEEPING:
        return RepairResult(
            command=command,
            status=RepairStatus.FAILED,
            diagnostics=(f"unsupported recovery repair command: {command.code}",),
        )
    finding_codes = set(command_finding_codes(command))
    if (
        LAUNCH_HISTORY_STALE in finding_codes
        and command.scope is ConsistencyScope.REPOSITORY
    ):
        return _apply_launch_history_bookkeeping(command, run_state, snapshot, config)
    if RECOVERY_COUNTERS_STALE in finding_codes:
        return _apply_counter_bookkeeping(command, run_state, snapshot, config)
    if RUN_STATE_MISSING in finding_codes:
        return _apply_missing_entry_bookkeeping(command, run_state, snapshot, config)
    return _skipped(command, "bookkeeping command has no recovery finding")
