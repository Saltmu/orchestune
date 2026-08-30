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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
    derive_desired_repository_state,
)
from orchestune.consistency.engine import ConsistencyEngine
from orchestune.consistency.intents import IntentJournal
from orchestune.consistency.invariants.execution import execution_invariants
from orchestune.consistency.invariants.status import status_invariants
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
from orchestune.consistency.repairs.execution import plan_execution_repairs
from orchestune.consistency.repairs.status import plan_status_repairs
from orchestune.consistency.supervisor import (
    ConsistencyMode,
    ConsistencySupervisor,
)
from orchestune.dispatch.config import DispatcherConfig
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
from orchestune.dispatch.phase_gc import run_gc_phase
from orchestune.dispatch.phase_rebase import (
    _sync_external_locks,
    ensure_parent_branch_ready,
)
from orchestune.dispatch.phase_reconciliation import (
    _process_active_worktrees,
    run_blocked_promotion_phase,
    run_dual_status_reconciliation,
    run_self_heal_phase,
)
from orchestune.dispatch.phase_scheduling import run_scheduling_phase
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import load_run_state
from orchestune.dispatch.status_repair import (
    execute_status_repair_command,
    status_intent_journal_path,
    task_lifecycle,
)
from orchestune.dispatch.targets import DispatchHandle
from orchestune.dispatch.worktree import file_lock
from orchestune.infra.process_utils import is_process_alive
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

    def __init__(self, config, run_state, issues, ctx, *, fresh: bool) -> None:
        self._config = config
        self._run_state = run_state
        self._cached_issues = issues
        self._cached_prs = ctx.prs
        self._cached_branches = ctx.subtask_branch_map
        self._fresh = fresh
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
                if "status:in-progress" in task.status_labels and task.subtask_id
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
                lifecycle=task_lifecycle(task.status_labels),
                forced_serial=task.issue_number in forced_serial_issues,
            )
            for task in sorted(
                self._tasks_by_issue.values(), key=lambda task: task.subtask_id
            )
            if task.subtask_id
        )

    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState:
        intents = IntentJournal(status_intent_journal_path(self._config)).pending(
            now=observed.observed_at
        )
        return derive_desired_repository_state(
            observed.repository_id,
            self._desired_tasks(),
            active_task_ids=self._active_task_ids(),
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
    adapter: _DispatchConsistencyAdapter
    completed_subtask_ids: frozenset[str]

    def execute(self, command: RepairCommand) -> RepairResult:
        if command.code.startswith("status."):
            return execute_status_repair_command(
                command,
                self.adapter.tasks_by_issue,
                completed_subtask_ids=self.completed_subtask_ids,
                config=self.config,
            )
        return RepairResult(
            command=command,
            status=RepairStatus.SKIPPED,
            diagnostics=("repair remains owned by its existing execution phase",),
        )


@dataclass(frozen=True, slots=True)
class _ConsistencyRuntime:
    supervisor: ConsistencySupervisor
    cached_adapter: _DispatchConsistencyAdapter
    fresh_adapter: _DispatchConsistencyAdapter


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
) -> None:
    if runtime is None:
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
        runtime.supervisor.repair_until_stable(
            final_scan,
            observer=runtime.fresh_adapter,
            deriver=runtime.fresh_adapter,
            executor=_DispatchRepairExecutor(
                config=config,
                adapter=runtime.fresh_adapter,
                completed_subtask_ids=frozenset(ctx.done_subtask_ids),
            ),
            allowlist=(config.consistency_repair_allowlist if config.apply else ()),
            max_passes=config.consistency_max_repair_passes,
        )
    report.consistency = runtime.supervisor.cycle_report(mode=config.consistency_mode)


def _prepare_cycle_issues(run_state, config: DispatcherConfig, now: float):
    ensure_parent_branch_ready(config)
    issues = _fetch_issues(config)
    # #512: 完了・クローズ済みIssueの回収回数を台帳から落とす。親Issueでの
    # 絞り込み前の一覧で判定し、他の親配下のIssueも取り漏らさないようにする。
    discard_reclaim_counts_for_closed_issues(run_state, issues, config)
    run_self_heal_phase(run_state, config, now)
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


def _execute_cycle_pipeline(
    ctx, issues, run_state, config: DispatcherConfig, now: float
) -> CycleReport:
    (
        completion_events,
        deviation_events,
        any_forced_serial,
        completed_subtask_ids,
    ) = _process_active_worktrees(ctx)
    _notify_pr_links(ctx, config)

    completion_events = run_gc_phase(
        ctx.run_state,
        ctx.tasks_by_issue,
        config,
        completion_events,
        open_prs=ctx.prs,
    )
    promotion_events = run_blocked_promotion_phase(
        issues, run_state, ctx, completed_subtask_ids, config
    )
    lock_result = _sync_external_locks(
        ctx.tasks_by_issue, ctx.prs, ctx.run_state, config
    )
    run_dual_status_reconciliation(ctx, config)
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
        applied=config.apply,
        scheduling_decisions=scheduling.decisions,
        execution_selections=scheduling.execution_selections,
    )


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:
    lock_path = Path(config.run_state_path).with_suffix(".lock")
    with file_lock(lock_path):
        run_state = load_run_state(config.run_state_path)
        now = time.time()
        issues = _prepare_cycle_issues(run_state, config, now)
        ctx = _build_cycle_context(issues, run_state, config)
        consistency_runtime = _start_consistency_runtime(config, run_state, issues, ctx)
        report = _execute_cycle_pipeline(ctx, issues, run_state, config, now)
        _finish_consistency_runtime(consistency_runtime, report, ctx, now, config)

        if config.apply:
            append_event_log(build_event_log_entry(report, now), config.events_log_path)

        return report
