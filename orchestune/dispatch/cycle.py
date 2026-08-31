"""1サイクル分のディスパッチオーケストレーション本体。

各フェーズの実処理は対応するフェーズコーディネーターモジュール
(`dispatch_cycle_context`/`dispatch_phase_reconciliation`/`dispatch_phase_gc`/
`dispatch_phase_scheduling`/`dispatch_phase_rebase`)に委譲し、
`run_dispatch_cycle`自体はそれらを決まった順序で呼び出すパイプライン制御に
特化する（#477）。
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    TaskLifecycle,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.execution import execution_invariants
from orchestune.consistency.invariants.status import (
    BLOCKED_WITH_RESOLVED_DEPENDENCIES,
    PRIMARY_STATUS_CONFLICT,
    status_invariants,
)
from orchestune.consistency.models import (
    ConsistencyReport,
    ConsistencyScope,
    DesiredRepositoryState,
    ObservedRepositoryState,
    RepairCommand,
    RepairResult,
    RepairStatus,
    StateChanged,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    FACT_BRANCH_NAME,
    FACT_EXECUTION_KIND,
    FACT_ISSUE_LABELS,
    FACT_PULL_REQUEST_STATE,
    FACT_WORKTREE_PATH,
    ExecutionRecord,
    ForgeSnapshot,
    ObservationCollector,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
    COMMAND_RECLAIM,
    COMMAND_REQUEUE,
    plan_execution_repairs,
)
from orchestune.consistency.repairs.status import plan_status_repairs
from orchestune.consistency.supervisor import (
    ConsistencyCycleReport,
    ConsistencyMode,
    ConsistencyRepairOutcome,
    ConsistencyRepairPass,
    ConsistencySupervisor,
    RepairDisposition,
    repair_command_finding_codes,
)
from orchestune.dispatch.config import (
    DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST,
    DispatcherConfig,
)
from orchestune.dispatch.cycle_context import (
    _build_cycle_context,
    _build_task_mappings,
    _fetch_issues,
    discard_reclaim_counts_for_closed_issues,
)
from orchestune.dispatch.cycle_report import (
    CycleReport,
    append_event_log,
    build_event_log_entry,
)
from orchestune.dispatch.execution_repair import (
    DispatchRepairExecutorAdapter,
    RepairCommandHandler,
)
from orchestune.dispatch.phase_gc import build_gc_reclaim_handler, run_gc_phase
from orchestune.dispatch.phase_rebase import (
    _sync_external_locks,
    ensure_parent_branch_ready,
)
from orchestune.dispatch.phase_reconciliation import (
    _process_active_worktrees,
    run_post_gc_reconciliation,
)
from orchestune.dispatch.phase_scheduling import run_scheduling_phase
from orchestune.dispatch.recovery import (
    LAUNCH_HISTORY_STALE,
    RecoveryBookkeepingAdapter,
    execute_bookkeeping_repair_command,
    execute_recovery_requeue_command,
    plan_recovery_bookkeeping_repairs,
    recovery_bookkeeping_invariants,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import load_run_state
from orchestune.dispatch.status_repair import (
    execute_status_repair_command,
    reconcile_status_repair_intents,
    status_intent_journal_path,
    task_lifecycle,
)
from orchestune.dispatch.targets import DispatchHandle
from orchestune.dispatch.worktree import file_lock
from orchestune.infra.process_utils import is_process_alive
from orchestune.labels import StatusLabel
from orchestune.pr_link_notice import (
    notice_expected_bases,
    notify_open_pr_links,
)

__all__ = ["CycleReport", "run_dispatch_cycle"]


@dataclass(frozen=True, slots=True)
class _FunctionPlanner:
    function: Callable[[ConsistencyReport], tuple[RepairCommand, ...]]

    def plan(self, report: ConsistencyReport) -> tuple[RepairCommand, ...]:
        return self.function(report)


@dataclass(frozen=True, slots=True)
class _BranchProbe:
    config: DispatcherConfig

    def branch_exists(self, branch: str) -> bool:
        return self.config.resolved_forge.branch_exists(branch)


class _WorktreeProbe:
    def worktree_exists(self, path: str) -> bool:
        return Path(path).exists()


class _ProcessProbe:
    def is_alive(self, pid: int) -> bool:
        return is_process_alive(pid)


@dataclass(frozen=True, slots=True)
class _ExternalExecutionProbe:
    config: DispatcherConfig

    def status(self, external_id: str) -> str:
        assert self.config.dispatch_target is not None
        handle = DispatchHandle(external_id=external_id)
        return self.config.dispatch_target.completion_status(
            handle, forge=self.config.resolved_forge
        )


def _repository_id() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository"


class _DispatchConsistencyAdapter:
    """Maps one dispatch-cycle view onto the consistency kernel contracts."""

    def __init__(
        self,
        config,
        run_state,
        issues,
        ctx,
        *,
        fresh: bool,
        completed_subtask_ids=(),
        include_status_intents: bool = True,
    ) -> None:
        self._config = config
        self._run_state = run_state
        self._cached_issues = issues
        self._cached_prs = ctx.prs
        self._cached_branches = ctx.subtask_branch_map
        self._fresh = fresh
        self._completed_subtask_ids = frozenset(completed_subtask_ids)
        self._include_status_intents = include_status_intents
        self._tasks_by_issue: dict[int, Task] = ctx.tasks_by_issue

    def _source_records(self):
        if not self._fresh:
            return self._cached_issues, self._cached_prs
        issues = _fetch_issues(self._config).filtered_by_parent(
            self._config.parent_issue_number
        )
        prs = self._config.resolved_forge.list_open_prs(paginate_files=True)
        return issues, prs

    def _executions(self) -> tuple[ExecutionRecord, ...]:
        return tuple(
            ExecutionRecord(
                issue_number=active.issue_number,
                branch=active.branch,
                worktree_path=active.worktree_path,
                pid=active.pid,
                external_id=active.external_id,
                started_at=active.started_at,
                kind=(
                    EXECUTION_KIND_CLOUD
                    if active.external_id is not None
                    else EXECUTION_KIND_LOCAL
                    if active.pid is not None
                    else None
                ),
            )
            for _, active in sorted(self._run_state.active_worktrees.items())
        )

    def _branches_by_issue(self) -> dict[int, str]:
        branches = {
            active.issue_number: active.branch
            for active in self._run_state.active_worktrees.values()
        }
        for task in self._tasks_by_issue.values():
            branch = self._cached_branches.get(task.subtask_id)
            if branch is not None:
                branches.setdefault(task.issue_number, branch)
        return branches

    def observe(self) -> ObservedRepositoryState:
        issues, prs = self._source_records()
        self._tasks_by_issue, _, _ = _build_task_mappings(issues.all())
        observed_at = datetime.now(UTC)
        collector = ObservationCollector(
            repository_id=_repository_id(),
            git_probe=_BranchProbe(self._config),
            worktree_probe=_WorktreeProbe(),
            process_probe=_ProcessProbe(),
            external_probe=_ExternalExecutionProbe(self._config),
            clock=lambda: observed_at,
        )
        return collector.collect(
            forge=ForgeSnapshot(
                issues=tuple(issues.all()),
                pull_requests=tuple(prs),
                fetched_at=observed_at,
            ),
            executions=self._executions(),
            branches_by_issue=self._branches_by_issue(),
        )

    def _active_task_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                task.subtask_id
                for task in self._tasks_by_issue.values()
                if StatusLabel.IN_PROGRESS in task.status_labels and task.subtask_id
            )
        )

    def _desired_tasks(self) -> tuple[DesiredTaskInput, ...]:
        forced_serial_issues = {
            active.issue_number
            for active in self._run_state.active_worktrees.values()
            if active.forced_serial
        }
        return tuple(
            DesiredTaskInput(
                task_id=task.subtask_id,
                subject_id=str(task.issue_number),
                depends_on=task.depends_on,
                footprint=task.footprint,
                lifecycle=task_lifecycle(
                    task.status_labels,
                    completed=task.subtask_id in self._completed_subtask_ids,
                ),
                forced_serial=task.issue_number in forced_serial_issues,
            )
            for task in sorted(
                self._tasks_by_issue.values(), key=lambda task: task.subtask_id
            )
            if task.subtask_id
        )

    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState:
        intents = (
            IntentJournal(status_intent_journal_path(self._config)).pending(
                now=observed.observed_at
            )
            if self._include_status_intents
            else ()
        )
        return derive_desired_repository_state(
            observed.repository_id,
            self._desired_tasks(),
            active_task_ids=self._active_task_ids(),
            completed_task_ids=self._completed_subtask_ids,
            policy=DispatchPolicy(
                max_concurrent=self._config.max_concurrent,
                task_timeout_seconds=self._config.task_timeout_seconds,
                zombie_gc_enabled=self._config.zombie_gc,
            ),
            intents=intents,
            now=observed.observed_at,
        )

    @property
    def tasks_by_issue(self) -> dict[int, Task]:
        return self._tasks_by_issue


@dataclass(frozen=True, slots=True)
class _DispatchRepairExecutor:
    config: DispatcherConfig
    adapter: _DispatchConsistencyAdapter | RecoveryBookkeepingAdapter
    completed_subtask_ids: frozenset[str]
    execution_handlers: Mapping[str, RepairCommandHandler] = field(default_factory=dict)

    def execute(self, command: RepairCommand) -> RepairResult:
        if command.code.startswith("status."):
            return execute_status_repair_command(
                command,
                self.adapter.tasks_by_issue,
                completed_subtask_ids=self.completed_subtask_ids,
                config=self.config,
            )
        return DispatchRepairExecutorAdapter(self.execution_handlers).execute(command)


@dataclass(frozen=True, slots=True)
class _ConsistencyRuntime:
    supervisor: ConsistencySupervisor
    cached_adapter: _DispatchConsistencyAdapter
    fresh_adapter: _DispatchConsistencyAdapter


@dataclass(slots=True)
class _RepairCycleState:
    claimed_repair_codes: set[str] = field(default_factory=set)
    reports: list[ConsistencyCycleReport] = field(default_factory=list)

    def add_report(self, report: ConsistencyCycleReport) -> None:
        self.claimed_repair_codes.update(_attempted_repair_codes(report))
        has_repair_scope = any(
            scan.report.findings or scan.repair_candidates for scan in report.scans
        )
        if report.repair_passes or has_repair_scope:
            self.reports.append(report)


def _status_repair_supervisor() -> ConsistencySupervisor:
    return ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine(status_invariants()),
        repair_planners=(_FunctionPlanner(plan_status_repairs),),
    )


def _command_finding_code(command: RepairCommand) -> str | None:
    value = dict(command.parameters).get("finding_code")
    return value if isinstance(value, str) else None


def _attempted_repair_codes(report: ConsistencyCycleReport) -> set[str]:
    return {
        code
        for repair_pass in report.repair_passes
        for result in repair_pass.results
        for code in (
            result.command.code,
            *repair_command_finding_codes(result.command),
        )
    }


def _status_repair_commands(
    boundary_report: ConsistencyCycleReport,
    initial_scan,
    finding_code: str,
    *,
    applied: bool,
) -> tuple[RepairCommand, ...]:
    if not applied:
        return tuple(
            command
            for command in initial_scan.repair_candidates
            if _command_finding_code(command) == finding_code
        )
    return tuple(
        result.command
        for repair_pass in boundary_report.repair_passes
        for result in repair_pass.results
        if result.status is RepairStatus.APPLIED
        and _command_finding_code(result.command) == finding_code
    )


def _promotion_events(
    commands: tuple[RepairCommand, ...], adapter: _DispatchConsistencyAdapter
) -> list[dict]:
    events = []
    for command in commands:
        if command.subject_id is None:
            continue
        try:
            task = adapter.tasks_by_issue.get(int(command.subject_id))
        except ValueError:
            task = None
        if task is not None:
            events.append(
                {"issue_number": task.issue_number, "subtask_id": task.subtask_id}
            )
    return events


def _status_boundary_adapters(
    *, issues, run_state, ctx, completed_subtask_ids, config
) -> tuple[_DispatchConsistencyAdapter, _DispatchConsistencyAdapter]:
    common = {
        "completed_subtask_ids": completed_subtask_ids,
        "include_status_intents": False,
    }
    cached = _DispatchConsistencyAdapter(
        config, run_state, issues, ctx, fresh=False, **common
    )
    fresh = _DispatchConsistencyAdapter(
        config, run_state, issues, ctx, fresh=True, **common
    )
    return cached, fresh


def _status_boundary_report(
    boundary: str,
    finding_code: str,
    *,
    cached_adapter: _DispatchConsistencyAdapter,
    fresh_adapter: _DispatchConsistencyAdapter,
    completed_subtask_ids,
    config: DispatcherConfig,
):
    supervisor = _status_repair_supervisor()
    initial_scan = supervisor.full_scan(
        boundary, observer=cached_adapter, deriver=cached_adapter
    )
    supervisor.repair_until_stable(
        initial_scan,
        observer=fresh_adapter,
        deriver=fresh_adapter,
        executor=_DispatchRepairExecutor(
            config=config,
            adapter=fresh_adapter,
            completed_subtask_ids=frozenset(completed_subtask_ids),
        ),
        allowlist=(
            (finding_code,)
            if config.apply and finding_code in DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST
            else ()
        ),
        max_passes=1,
    )
    return initial_scan, supervisor.cycle_report(mode=ConsistencyMode.REPAIR)


def _run_status_repair_boundary(
    boundary: str,
    finding_code: str,
    *,
    issues,
    run_state,
    ctx,
    completed_subtask_ids,
    config: DispatcherConfig,
    cycle_state: _RepairCycleState,
) -> list[dict]:
    """Run one status finding family through Supervisor and typed executor."""
    cached_adapter, fresh_adapter = _status_boundary_adapters(
        issues=issues,
        run_state=run_state,
        ctx=ctx,
        completed_subtask_ids=completed_subtask_ids,
        config=config,
    )
    initial_scan, boundary_report = _status_boundary_report(
        boundary,
        finding_code,
        cached_adapter=cached_adapter,
        fresh_adapter=fresh_adapter,
        completed_subtask_ids=completed_subtask_ids,
        config=config,
    )
    cycle_state.add_report(boundary_report)
    commands = _status_repair_commands(
        boundary_report,
        initial_scan,
        finding_code,
        applied=config.apply,
    )
    return _promotion_events(commands, fresh_adapter)


def _merge_consistency_reports(
    main: ConsistencyCycleReport,
    boundaries: list[ConsistencyCycleReport],
) -> ConsistencyCycleReport:
    if not boundaries:
        return main
    boundary_scans = tuple(scan for boundary in boundaries for scan in boundary.scans)
    scans = (*main.scans[:1], *boundary_scans, *main.scans[1:])
    raw_passes = [
        repair_pass
        for report in (*boundaries, main)
        for repair_pass in report.repair_passes
    ]
    repair_passes = tuple(
        ConsistencyRepairPass(number=index, results=repair_pass.results)
        for index, repair_pass in enumerate(raw_passes, start=1)
    )
    outcomes: dict[tuple[str, str, str], ConsistencyRepairOutcome] = {}
    for consistency_report in (*boundaries, main):
        for outcome in consistency_report.repair_outcomes:
            key = (outcome.scope.value, outcome.subject_id or "", outcome.finding_code)
            previous = outcomes.get(key)
            if previous is None:
                outcomes[key] = outcome
                continue
            disposition = (
                RepairDisposition.FAILED
                if RepairDisposition.FAILED
                in {previous.disposition, outcome.disposition}
                else outcome.disposition
            )
            outcomes[key] = ConsistencyRepairOutcome(
                finding_code=outcome.finding_code,
                scope=outcome.scope,
                subject_id=outcome.subject_id,
                disposition=disposition,
                diagnostics=tuple(
                    dict.fromkeys((*previous.diagnostics, *outcome.diagnostics))
                ),
            )
    return ConsistencyCycleReport(
        mode=main.mode,
        scans=scans,
        repair_passes=repair_passes,
        repair_outcomes=tuple(outcomes[key] for key in sorted(outcomes)),
    )


def _start_consistency_runtime(
    config, run_state, issues, ctx
) -> _ConsistencyRuntime | None:
    if config.consistency_mode is ConsistencyMode.OFF:
        return None
    supervisor = ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine((*status_invariants(), *execution_invariants())),
        repair_planners=(
            _FunctionPlanner(plan_status_repairs),
            _FunctionPlanner(plan_execution_repairs),
        ),
    )
    runtime = _ConsistencyRuntime(
        supervisor=supervisor,
        cached_adapter=_DispatchConsistencyAdapter(
            config, run_state, issues, ctx, fresh=False
        ),
        fresh_adapter=_DispatchConsistencyAdapter(
            config, run_state, issues, ctx, fresh=True
        ),
    )
    supervisor.full_scan(
        "start", observer=runtime.cached_adapter, deriver=runtime.cached_adapter
    )
    return runtime


def _event_issue_number(event: dict, ctx) -> int | None:
    issue_number = event.get("issue_number")
    if isinstance(issue_number, int) and not isinstance(issue_number, bool):
        return issue_number
    subtask_id = event.get("subtask_id")
    if isinstance(subtask_id, str):
        mapped = ctx.issue_number_by_subtask_id.get(subtask_id)
        if isinstance(mapped, int) and not isinstance(mapped, bool):
            return mapped
    return None


def _event_changes(
    events: list[dict], ctx, fields: tuple[str, ...], source: str, occurred_at
) -> list[StateChanged]:
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(issue_number),
            fields=fields,
            source=source,
            occurred_at=occurred_at,
        )
        for event in events
        if (issue_number := _event_issue_number(event, ctx)) is not None
    ]


def _lock_state_changes(report: CycleReport, occurred_at) -> list[StateChanged]:
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(task.issue_number),
            fields=(FACT_ISSUE_LABELS,),
            source=f"dispatch.external-lock.{action}",
            occurred_at=occurred_at,
        )
        for action, tasks in report.lock_changes.items()
        for task in tasks
    ]


def _scheduling_state_changes(report: CycleReport, occurred_at) -> list[StateChanged]:
    if not report.applied:
        return []
    return [
        StateChanged(
            scope=ConsistencyScope.TASK,
            subject_id=str(task.issue_number),
            fields=(
                FACT_BRANCH_NAME,
                FACT_EXECUTION_KIND,
                FACT_ISSUE_LABELS,
                FACT_WORKTREE_PATH,
            ),
            source="dispatch.scheduling",
            occurred_at=occurred_at,
        )
        for task in report.selected
    ]


def _pipeline_state_changes(
    report: CycleReport, ctx, now: float
) -> tuple[StateChanged, ...]:
    if not report.applied:
        return ()
    occurred_at = datetime.fromtimestamp(now, UTC)
    changes = _event_changes(
        report.promotion_events,
        ctx,
        (FACT_ISSUE_LABELS,),
        "dispatch.promotion",
        occurred_at,
    )
    changes.extend(
        _event_changes(
            report.completion_events,
            ctx,
            (FACT_EXECUTION_KIND, FACT_ISSUE_LABELS, FACT_PULL_REQUEST_STATE),
            "dispatch.completion",
            occurred_at,
        )
    )
    changes.extend(
        _event_changes(
            report.deviation_events,
            ctx,
            (FACT_BRANCH_NAME, FACT_ISSUE_LABELS),
            "dispatch.deviation",
            occurred_at,
        )
    )
    changes.extend(_lock_state_changes(report, occurred_at))
    changes.extend(_scheduling_state_changes(report, occurred_at))
    return tuple(changes)


def _finish_consistency_runtime(
    runtime: _ConsistencyRuntime | None,
    report: CycleReport,
    ctx,
    now: float,
    config: DispatcherConfig,
    repair_cycle: _RepairCycleState,
) -> None:
    if runtime is None:
        report.consistency = _merge_consistency_reports(
            ConsistencyCycleReport(mode=config.consistency_mode), repair_cycle.reports
        )
        return
    runtime.supervisor.targeted_scan(
        "pipeline",
        _pipeline_state_changes(report, ctx, now),
        observer=runtime.fresh_adapter,
        deriver=runtime.fresh_adapter,
    )
    final_scan = runtime.supervisor.full_scan(
        "end", observer=runtime.fresh_adapter, deriver=runtime.fresh_adapter
    )
    if config.consistency_mode is ConsistencyMode.REPAIR:
        final_allowlist = (
            config.consistency_repair_allowlist - repair_cycle.claimed_repair_codes
            if config.apply
            else frozenset()
        )
        runtime.supervisor.repair_until_stable(
            final_scan,
            observer=runtime.fresh_adapter,
            deriver=runtime.fresh_adapter,
            executor=_DispatchRepairExecutor(
                config=config,
                adapter=runtime.fresh_adapter,
                completed_subtask_ids=frozenset(ctx.done_subtask_ids),
                execution_handlers=_final_execution_repair_handlers(
                    runtime,
                    report,
                    ctx,
                    config,
                    now=now,
                ),
            ),
            allowlist=final_allowlist,
            max_passes=config.consistency_max_repair_passes,
        )
    main_report = runtime.supervisor.cycle_report(mode=config.consistency_mode)
    report.consistency = _merge_consistency_reports(main_report, repair_cycle.reports)


def _execute_final_recovery_command(
    command: RepairCommand,
    run_state,
    config: DispatcherConfig,
    *,
    now: float,
) -> RepairResult:
    adapter = RecoveryBookkeepingAdapter(_repository_id(), run_state, config, now=now)
    adapter.observe()
    if command.code == COMMAND_REQUEUE:
        return execute_recovery_requeue_command(
            command, run_state, adapter.snapshot, config
        )
    return execute_bookkeeping_repair_command(
        command, run_state, adapter.snapshot, config
    )


def _final_execution_repair_handlers(
    runtime: _ConsistencyRuntime,
    report: CycleReport,
    ctx,
    config: DispatcherConfig,
    *,
    now: float,
) -> Mapping[str, RepairCommandHandler]:
    def recovery(command: RepairCommand) -> RepairResult:
        return _execute_final_recovery_command(command, ctx.run_state, config, now=now)

    return {
        COMMAND_RECLAIM: build_gc_reclaim_handler(
            ctx.run_state,
            runtime.fresh_adapter.tasks_by_issue,
            config,
            report.completion_events,
            ctx.prs,
            now=now,
        ),
        COMMAND_REQUEUE: recovery,
        COMMAND_BOOKKEEPING: recovery,
    }


def _run_recovery_bookkeeping_boundary(
    run_state, config: DispatcherConfig, *, now: float
) -> ConsistencyCycleReport:
    """Run startup recovery only through Supervisor and typed repair handlers."""
    adapter = RecoveryBookkeepingAdapter(_repository_id(), run_state, config, now=now)
    supervisor = ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine(recovery_bookkeeping_invariants()),
        repair_planners=(_FunctionPlanner(plan_recovery_bookkeeping_repairs),),
    )
    initial_scan = supervisor.full_scan(
        "recovery-bookkeeping", observer=adapter, deriver=adapter
    )
    handlers: Mapping[str, RepairCommandHandler] = {
        COMMAND_REQUEUE: lambda command: execute_recovery_requeue_command(
            command, run_state, adapter.snapshot, config
        ),
        COMMAND_BOOKKEEPING: lambda command: execute_bookkeeping_repair_command(
            command, run_state, adapter.snapshot, config
        ),
    }
    allowlist = (
        DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST & {COMMAND_REQUEUE, COMMAND_BOOKKEEPING}
        if config.apply
        else {LAUNCH_HISTORY_STALE}
    )
    supervisor.repair_until_stable(
        initial_scan,
        observer=adapter,
        deriver=adapter,
        executor=_DispatchRepairExecutor(
            config=config,
            adapter=adapter,
            completed_subtask_ids=frozenset(),
            execution_handlers=handlers,
        ),
        allowlist=allowlist,
        max_passes=1,
    )
    return supervisor.cycle_report(mode=ConsistencyMode.REPAIR)


def _recovery_requeued(report: ConsistencyCycleReport) -> bool:
    return any(
        result.command.code == COMMAND_REQUEUE and result.status is RepairStatus.APPLIED
        for repair_pass in report.repair_passes
        for result in repair_pass.results
    )


def _prepare_cycle_issues(run_state, config: DispatcherConfig, _now: float):
    ensure_parent_branch_ready(config)
    issues = _fetch_issues(config)
    # #512: 完了・クローズ済みIssueの回収回数を台帳から落とす。親Issueでの
    # 絞り込み前の一覧で判定し、他の親配下のIssueも取り漏らさないようにする。
    discard_reclaim_counts_for_closed_issues(run_state, issues, config)
    return issues.filtered_by_parent(config.parent_issue_number)


def _notify_pr_links(ctx, config: DispatcherConfig) -> None:
    """#676: 親ブランチ宛てPRを、対象Issue側へコメントで相互リンクする。

    GitHubは既定ブランチ以外を対象とするPRをIssueの「Development」欄へ
    自動リンクしないため、ディスパッチャーが検知した時点で補完する。
    通知はベストエフォートで、失敗してもサイクルを止めない。
    """
    if not config.apply:
        return
    events = notify_open_pr_links(
        config.resolved_forge,
        ctx.prs,
        notice_expected_bases(ctx.tasks_by_issue.values()),
    )
    for event in events:
        print(
            f"Linked PR #{event['pr_number']} to issue #{event['issue_number']} "
            "with a notice comment.",
            file=sys.stderr,
        )


def _completed_subtask_ids(ctx, completed_in_cycle: Iterable[str]) -> set[str]:
    terminal = {
        TaskLifecycle.DONE,
        TaskLifecycle.NOT_NEEDED,
    }
    persisted = {
        task.subtask_id
        for task in ctx.tasks_by_issue.values()
        if task.subtask_id and task_lifecycle(task.status_labels) in terminal
    }
    return persisted | set(completed_in_cycle)


def _run_pre_scheduling_reconciliation(
    *,
    ctx,
    issues,
    run_state,
    completed_in_cycle,
    completed,
    config,
    repair_cycle,
):
    promotion_events = _run_status_repair_boundary(
        "status-blocked-promotion",
        BLOCKED_WITH_RESOLVED_DEPENDENCIES,
        issues=issues,
        run_state=run_state,
        ctx=ctx,
        completed_subtask_ids=completed,
        config=config,
        cycle_state=repair_cycle,
    )
    promotion_events.extend(
        run_post_gc_reconciliation(issues, run_state, ctx, completed_in_cycle, config)
    )
    lock_result = _sync_external_locks(
        ctx.tasks_by_issue, ctx.prs, ctx.run_state, config
    )
    _run_status_repair_boundary(
        "status-primary-reconciliation",
        PRIMARY_STATUS_CONFLICT,
        issues=issues,
        run_state=run_state,
        ctx=ctx,
        completed_subtask_ids=completed,
        config=config,
        cycle_state=repair_cycle,
    )
    return promotion_events, lock_result


def _pipeline_report(
    scheduling,
    lock_result,
    *,
    deviation_events,
    completion_events,
    promotion_events,
    applied,
) -> CycleReport:
    return CycleReport(
        selected=scheduling.selected,
        quota_slots_available=scheduling.quota_slots_available,
        lock_changes={
            "to_lock": lock_result.to_lock,
            "to_unlock": lock_result.to_unlock,
        },
        deviation_events=deviation_events,
        completion_events=completion_events,
        promotion_events=promotion_events,
        applied=applied,
        scheduling_decisions=scheduling.decisions,
        execution_selections=scheduling.execution_selections,
    )


def _run_gc_reclaim_phase(ctx, config, completion_events, repair_cycle):
    gc_result = run_gc_phase(
        ctx.run_state,
        ctx.tasks_by_issue,
        config,
        completion_events,
        open_prs=ctx.prs,
    )
    repair_cycle.add_report(gc_result.consistency)
    return gc_result.completion_events


def _execute_cycle_pipeline(
    ctx,
    issues,
    run_state,
    config: DispatcherConfig,
    now: float,
    repair_cycle: _RepairCycleState,
) -> CycleReport:
    (
        completion_events,
        deviation_events,
        any_forced_serial,
        completed_subtask_ids,
    ) = _process_active_worktrees(ctx)
    _notify_pr_links(ctx, config)

    completion_events = _run_gc_reclaim_phase(
        ctx, config, completion_events, repair_cycle
    )
    completed = _completed_subtask_ids(ctx, completed_subtask_ids)
    promotion_events, lock_result = _run_pre_scheduling_reconciliation(
        ctx=ctx,
        issues=issues,
        run_state=run_state,
        completed_in_cycle=completed_subtask_ids,
        completed=completed,
        config=config,
        repair_cycle=repair_cycle,
    )
    scheduling = run_scheduling_phase(
        ctx,
        issues,
        lock_result,
        completed_subtask_ids,
        any_forced_serial,
        deviation_events,
        now,
        config,
    )
    return _pipeline_report(
        scheduling,
        lock_result,
        deviation_events=deviation_events,
        completion_events=completion_events,
        promotion_events=promotion_events,
        applied=config.apply,
    )


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:
    lock_path = Path(config.run_state_path).with_suffix(".lock")
    with file_lock(lock_path):
        run_state = load_run_state(config.run_state_path)
        now = time.time()
        issues = _prepare_cycle_issues(run_state, config, now)
        reconcile_status_repair_intents(config, now=datetime.fromtimestamp(now, UTC))
        recovery_report = _run_recovery_bookkeeping_boundary(run_state, config, now=now)
        if _recovery_requeued(recovery_report):
            issues = _fetch_issues(config).filtered_by_parent(
                config.parent_issue_number
            )
        ctx = _build_cycle_context(issues, run_state, config)
        consistency_runtime = _start_consistency_runtime(config, run_state, issues, ctx)
        repair_cycle = _RepairCycleState()
        repair_cycle.add_report(recovery_report)
        report = _execute_cycle_pipeline(
            ctx, issues, run_state, config, now, repair_cycle
        )
        _finish_consistency_runtime(
            consistency_runtime, report, ctx, now, config, repair_cycle
        )

        if config.apply:
            append_event_log(build_event_log_entry(report, now), config.events_log_path)

        return report
