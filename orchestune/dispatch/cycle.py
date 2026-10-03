"""1サイクル分のディスパッチオーケストレーション本体。

各フェーズの実処理は対応するフェーズコーディネーターモジュール
(`dispatch_cycle_context`/`dispatch_phase_reconciliation`/`dispatch_phase_gc`/
`dispatch_phase_scheduling`/`dispatch_phase_rebase`)に委譲し、
`run_dispatch_cycle`自体はそれらを決まった順序で呼び出すパイプライン制御に
特化する（#477）。
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time as time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from orchestune.consistency.desired import (
    DesiredTaskInput,
    DispatchPolicy,
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
    DesiredRepositoryState,
    ObservedRepositoryState,
    RepairCommand,
    RepairResult,
    RepairStatus,
)
from orchestune.consistency.observation import (
    EXECUTION_KIND_CLOUD,
    EXECUTION_KIND_LOCAL,
    ExecutionRecord,
    ForgeSnapshot,
    ObservationCollector,
)
from orchestune.consistency.repairs.execution import (
    COMMAND_BOOKKEEPING,
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
    FunctionRepairPlanner,
    RepairDisposition,
    repair_command_finding_codes,
)
from orchestune.dispatch.config import (
    DEFAULT_SELF_HEALING_REPAIR_ALLOWLIST,
    DispatcherConfig,
)
from orchestune.dispatch.cycle_actions import CycleActionAdapter
from orchestune.dispatch.cycle_context import (
    _build_cycle_context,
    _build_task_mappings,
    _fetch_issues,
    discard_reclaim_counts_for_closed_issues,
)
from orchestune.dispatch.cycle_execution import execute_cycle, execute_pipeline
from orchestune.dispatch.cycle_report import (
    CycleReport,
)
from orchestune.dispatch.cycle_report import (
    append_event_log as append_event_log,
)
from orchestune.dispatch.cycle_report import (
    build_event_log_entry as build_event_log_entry,
)
from orchestune.dispatch.cycle_state_changes import (
    _event_changes as _event_changes,
)
from orchestune.dispatch.cycle_state_changes import (
    _event_issue_number as _event_issue_number,
)
from orchestune.dispatch.cycle_state_changes import (
    _lock_state_changes as _lock_state_changes,
)
from orchestune.dispatch.cycle_state_changes import (
    _pipeline_state_changes as _pipeline_state_changes,
)
from orchestune.dispatch.cycle_state_changes import (
    _scheduling_state_changes as _scheduling_state_changes,
)
from orchestune.dispatch.execution_repair import (
    DispatchRepairExecutorAdapter,
    RepairCommandHandler,
)
from orchestune.dispatch.external_execution import probe_runtime_status
from orchestune.dispatch.phase_rebase import (
    ensure_parent_branch_ready,
)
from orchestune.dispatch.phase_scheduling import (
    run_scheduling_phase as run_scheduling_phase,
)
from orchestune.dispatch.prior_parent_merge import reconcile_prior_parent_merges
from orchestune.dispatch.progress import progress_phase
from orchestune.dispatch.recovery import (
    LAUNCH_ATTEMPT_PENDING,
    LAUNCH_HISTORY_STALE,
    RecoveryBookkeepingAdapter,
    execute_bookkeeping_repair_command,
    execute_recovery_requeue_command,
    plan_recovery_bookkeeping_repairs,
    recovery_bookkeeping_invariants,
)
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.status_dependency_policy import (
    completed_dependency_ids,
    desired_dependency_ids,
)
from orchestune.dispatch.status_repair import (
    VerifiedStatusTransition,
    execute_status_repair_command,
    reconcile_status_repair_intents,
    status_intent_journal_path,
    task_lifecycle,
)
from orchestune.dispatch.status_repair_dependencies import (
    CompletionEvidenceView,
    DependencyAssessmentView,
)
from orchestune.infra.process_utils import is_process_alive
from orchestune.infra.process_utils import run_state_lock as run_state_lock
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import load_run_state as load_run_state
from orchestune.pr_link_notice import (
    notice_expected_bases,
    notify_open_pr_links,
)
from orchestune.task_metadata import TaskMetadata, require_raw_tasks

if TYPE_CHECKING:
    from collections.abc import Sequence

    from orchestune.consistency.supervisor import ConsistencyScanResult
    from orchestune.dispatch.cycle_context import IssuesByStatus
    from orchestune.dispatch.phase_scheduling import SchedulingPhaseResult
    from orchestune.dispatch.prior_parent_merge import PriorParentMergeReconciliation
    from orchestune.ledger.run_state import RunState
    from orchestune.lock_contracts import ExternalLockScanResult
    from orchestune.models import PrRecord

__all__ = ["CycleReport", "run_dispatch_cycle"]


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
        """外部実行の実行状態（running/stopped/unknown）。成果物の完了判定ではない。"""
        return probe_runtime_status(self.config, external_id)


def _repository_id() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "orchestune-repository"


class _DispatchConsistencyAdapter:
    """Maps one dispatch-cycle view onto the consistency kernel contracts."""

    def __init__(
        self,
        config: DispatcherConfig,
        run_state: RunState,
        issues: IssuesByStatus,
        ctx: CycleContext,
        *,
        fresh: bool,
        include_status_intents: bool = True,
    ) -> None:
        self._config = config
        self._run_state = run_state
        self._cached_issues = issues
        self._cached_prs = ctx.pull_requests()
        self._cached_branches = {
            task.issue_number: branch
            for task in ctx.tasks()
            if (branch := ctx.canonical_branch(task.issue_number)) is not None
        }
        self._fresh = fresh
        self._completion_evidence: DependencyAssessmentView = ctx
        self._include_status_intents = include_status_intents
        self._tasks_by_issue: dict[int, TaskMetadata] = {
            task.issue_number: task for task in ctx.tasks()
        }

    def _source_records(self) -> tuple[IssuesByStatus, Sequence[PrRecord]]:
        if not self._fresh:
            return self._cached_issues, self._cached_prs
        issues = _fetch_issues(self._config)
        prs = self._config.resolved_forge.list_open_prs(paginate_files=True)
        return issues, prs

    def _executions(self) -> tuple[ExecutionRecord, ...]:
        return tuple(
            ExecutionRecord(
                issue_number=active.core.issue_number,
                branch=active.core.branch,
                worktree_path=active.core.worktree_path,
                pid=active.launch.pid,
                external_id=active.launch.external_id,
                started_at=active.launch.started_at,
                kind=(
                    EXECUTION_KIND_CLOUD
                    if active.launch.external_id is not None
                    else EXECUTION_KIND_LOCAL
                    if active.launch.pid is not None
                    else None
                ),
                owner_kind=active.claim.owner_kind,
                claim_id=active.claim.claim_id,
                claim_stage=active.claim.claim_stage,
                launch_phase=active.launch.launch_phase,
            )
            for _, active in sorted(self._run_state.active_worktrees.items())
        )

    def _branches_by_issue(self) -> dict[int, str]:
        branches = {
            active.core.issue_number: active.core.branch
            for active in self._run_state.active_worktrees.values()
        }
        for task in self._tasks_by_issue.values():
            branch = self._cached_branches.get(task.issue_number)
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
                str(task.issue_number)
                for task in self._tasks_by_issue.values()
                if StatusLabel.IN_PROGRESS in task.status_labels and task.subtask_id
            )
        )

    def _desired_inputs(
        self,
    ) -> tuple[tuple[DesiredTaskInput, ...], frozenset[str]]:
        """#799: `DesiredTaskInput.task_id`/`depends_on`はIssue番号ベースで
        構築する。`subtask_id`は1つの分解計画（EPIC）内でしか一意性が
        保証されないため、`task_id=task.subtask_id`のままでは
        `--parent-issue`無指定時に別EPICの同名subtask_idが衝突し、
        `derive_desired_repository_state`が重複`task_id`でValueErrorを
        送出しうる（サイクル全体が例外停止するバグでもあった）。

        未解決の依存は、`completed_ids`に絶対に一致しない合成IDへ割り当てる
        ことで、`consistency.desired`側の仕組みをそのまま使って「恒久的に
        未解決」を表現する（依存なし扱いへ倒さない）。
        """
        forced_serial_issues = {
            active.core.issue_number
            for active in self._run_state.active_worktrees.values()
            if active.launch.forced_serial
        }
        assessments = {
            task.issue_number: self._completion_evidence.assess_dependencies(
                task.issue_number
            )
            for task in self._tasks_by_issue.values()
        }
        completed_ids = completed_dependency_ids(assessments.values())
        desired_tasks = tuple(
            DesiredTaskInput(
                task_id=str(task.issue_number),
                subject_id=str(task.issue_number),
                depends_on=desired_dependency_ids(
                    task.issue_number, assessments.get(task.issue_number)
                ),
                footprint=task.footprint,
                lifecycle=task_lifecycle(
                    task.status_labels,
                    completed=self._completion_evidence.is_completion_confirmed(
                        task.issue_number
                    ),
                ),
                forced_serial=task.issue_number in forced_serial_issues,
            )
            for task in sorted(
                self._tasks_by_issue.values(), key=lambda task: task.issue_number
            )
            if task.subtask_id
        )
        return desired_tasks, completed_ids

    def derive(self, observed: ObservedRepositoryState) -> DesiredRepositoryState:
        desired_tasks, completed_ids = self._desired_inputs()
        intents = (
            IntentJournal(status_intent_journal_path(self._config)).pending(
                now=observed.observed_at
            )
            if self._include_status_intents
            else ()
        )
        return derive_desired_repository_state(
            observed.repository_id,
            desired_tasks,
            active_task_ids=self._active_task_ids(),
            completed_task_ids=completed_ids,
            policy=DispatchPolicy(
                max_concurrent=self._config.max_concurrent,
                task_timeout_seconds=self._config.task_timeout_seconds,
                zombie_gc_enabled=self._config.zombie_gc,
            ),
            intents=intents,
            now=observed.observed_at,
        )

    @property
    def tasks_by_issue(self) -> dict[int, TaskMetadata]:
        return self._tasks_by_issue

    @property
    def completion_evidence(self) -> CompletionEvidenceView:
        return self._completion_evidence


@dataclass(frozen=True, slots=True)
class _DispatchRepairExecutor:
    config: DispatcherConfig
    adapter: _DispatchConsistencyAdapter | RecoveryBookkeepingAdapter
    completion_evidence: CompletionEvidenceView | None = None
    on_status_verified: Callable[[VerifiedStatusTransition], None] | None = None
    execution_handlers: Mapping[str, RepairCommandHandler] = field(default_factory=dict)

    def execute(self, command: RepairCommand) -> RepairResult:
        if command.code.startswith("status."):
            if self.completion_evidence is None:
                return RepairResult(
                    command=command,
                    status=RepairStatus.FAILED,
                    diagnostics=("status completion evidence is unavailable",),
                )
            raw_tasks = require_raw_tasks(
                self.adapter.tasks_by_issue.values(),
                operation="_DispatchRepairExecutor.execute",
            )
            return execute_status_repair_command(
                command,
                {task.issue_number: task for task in raw_tasks},
                completion_evidence=self.completion_evidence,
                config=self.config,
                on_verified=self.on_status_verified,
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


@dataclass(frozen=True, slots=True)
class _ContextRepairExecutor:
    """Adapt the Supervisor's generic executor name to the v3 action port."""

    ctx: CycleContext

    def execute(self, command: RepairCommand) -> RepairResult:
        return self.ctx.execute_repair(command)


def _status_repair_supervisor() -> ConsistencySupervisor:
    return ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine(status_invariants()),
        repair_planners=(FunctionRepairPlanner(plan_status_repairs),),
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
    initial_scan: ConsistencyScanResult,
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
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
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
    *,
    issues: IssuesByStatus,
    run_state: RunState,
    ctx: CycleContext,
    config: DispatcherConfig,
) -> tuple[_DispatchConsistencyAdapter, _DispatchConsistencyAdapter]:
    common = {
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
    ctx: CycleContext,
    config: DispatcherConfig,
) -> tuple[ConsistencyScanResult, ConsistencyCycleReport]:
    supervisor = _status_repair_supervisor()
    initial_scan = supervisor.full_scan(
        boundary, observer=cached_adapter, deriver=cached_adapter
    )
    supervisor.repair_until_stable(
        initial_scan,
        observer=fresh_adapter,
        deriver=fresh_adapter,
        executor=_ContextRepairExecutor(ctx),
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
    issues: IssuesByStatus,
    run_state: RunState,
    ctx: CycleContext,
    config: DispatcherConfig,
    cycle_state: _RepairCycleState,
) -> list[dict[str, object]]:
    """Run one status finding family through Supervisor and typed executor."""
    cached_adapter, fresh_adapter = _status_boundary_adapters(
        issues=issues,
        run_state=run_state,
        ctx=ctx,
        config=config,
    )
    initial_scan, boundary_report = _status_boundary_report(
        boundary,
        finding_code,
        cached_adapter=cached_adapter,
        fresh_adapter=fresh_adapter,
        ctx=ctx,
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
    config: DispatcherConfig,
    run_state: RunState,
    issues: IssuesByStatus,
    ctx: CycleContext,
) -> _ConsistencyRuntime | None:
    if config.consistency_mode is ConsistencyMode.OFF:
        return None
    supervisor = ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine((*status_invariants(), *execution_invariants())),
        repair_planners=(
            FunctionRepairPlanner(plan_status_repairs),
            FunctionRepairPlanner(plan_execution_repairs),
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


def _run_final_repair_pass(
    runtime: _ConsistencyRuntime,
    final_scan: ConsistencyScanResult,
    report: CycleReport,
    ctx: CycleContext,
    now: float,
    config: DispatcherConfig,
    repair_cycle: _RepairCycleState,
) -> None:
    """サイクル終端のconsistency修復パスを実行する。

    #859: `done_issue_numbers`は検証済み先行マージを含むが、active worktreeの
    同一サイクル完了は含まない。最終境界の修復も、パイプライン内の各フェーズと
    同じ実効完了を見る。
    """
    final_allowlist = (
        config.consistency_repair_allowlist - repair_cycle.claimed_repair_codes
        if config.apply
        else frozenset()
    )
    runtime.supervisor.repair_until_stable(
        final_scan,
        observer=runtime.fresh_adapter,
        deriver=runtime.fresh_adapter,
        executor=_ContextRepairExecutor(ctx),
        allowlist=final_allowlist,
        max_passes=config.consistency_max_repair_passes,
    )


def _finish_consistency_runtime(
    runtime: _ConsistencyRuntime | None,
    report: CycleReport,
    ctx: CycleContext,
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
        _run_final_repair_pass(
            runtime,
            final_scan,
            report,
            ctx,
            now,
            config,
            repair_cycle,
        )
    main_report = runtime.supervisor.cycle_report(mode=config.consistency_mode)
    report.consistency = _merge_consistency_reports(main_report, repair_cycle.reports)


def _run_recovery_bookkeeping_boundary(
    run_state: RunState, config: DispatcherConfig, *, now: float
) -> ConsistencyCycleReport:
    """Run startup recovery only through Supervisor and typed repair handlers."""
    adapter = RecoveryBookkeepingAdapter(_repository_id(), run_state, config, now=now)
    supervisor = ConsistencySupervisor(
        repository_id=_repository_id(),
        engine=ConsistencyEngine(recovery_bookkeeping_invariants()),
        repair_planners=(FunctionRepairPlanner(plan_recovery_bookkeeping_repairs),),
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
            execution_handlers=handlers,
        ),
        allowlist=allowlist,
        max_passes=1,
    )
    return supervisor.cycle_report(mode=ConsistencyMode.REPAIR)


def _recovery_requeued(report: ConsistencyCycleReport) -> bool:
    return any(
        (
            result.command.code == COMMAND_REQUEUE
            or LAUNCH_ATTEMPT_PENDING in repair_command_finding_codes(result.command)
        )
        and result.status is RepairStatus.APPLIED
        for repair_pass in report.repair_passes
        for result in repair_pass.results
    )


def _prepare_cycle_issues(
    run_state: RunState, config: DispatcherConfig, _now: float
) -> IssuesByStatus:
    with progress_phase(config.progress, "parent_branch"):
        ensure_parent_branch_ready(config)
    with progress_phase(config.progress, "issues_fetch"):
        issues = _fetch_issues(config)
    # #512: 完了・クローズ済みIssueの回収回数を台帳から落とす。親Issueでの
    # 絞り込み前の一覧で判定し、他の親配下のIssueも取り漏らさないようにする。
    discard_reclaim_counts_for_closed_issues(run_state, issues, config)
    return issues


def _notify_pr_links(ctx: CycleContext, config: DispatcherConfig) -> None:
    """#676: 親ブランチ宛てPRを、対象Issue側へコメントで相互リンクする。

    GitHubは既定ブランチ以外を対象とするPRをIssueの「Development」欄へ
    自動リンクしないため、ディスパッチャーが検知した時点で補完する。
    通知はベストエフォートで、失敗してもサイクルを止めない。
    """
    if not config.apply:
        return
    events = notify_open_pr_links(
        config.resolved_forge,
        ctx.pull_requests(),
        notice_expected_bases(ctx.tasks()),
    )
    for event in events:
        print(
            f"Linked PR #{event['pr_number']} to issue #{event['issue_number']} "
            "with a notice comment.",
            file=sys.stderr,
        )


def _run_pre_scheduling_reconciliation(
    *,
    ctx: CycleContext,
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
    repair_cycle: _RepairCycleState,
) -> tuple[list[dict[str, object]], ExternalLockScanResult]:
    promotion_events = _run_status_repair_boundary(
        "status-blocked-promotion",
        BLOCKED_WITH_RESOLVED_DEPENDENCIES,
        issues=issues,
        run_state=run_state,
        ctx=ctx,
        config=config,
        cycle_state=repair_cycle,
    )
    promotion_events.extend(ctx.reconcile_recovery())
    lock_result = ctx.scan_external_locks()
    _run_status_repair_boundary(
        "status-primary-reconciliation",
        PRIMARY_STATUS_CONFLICT,
        issues=issues,
        run_state=run_state,
        ctx=ctx,
        config=config,
        cycle_state=repair_cycle,
    )
    return promotion_events, lock_result


def _pipeline_report(
    scheduling: SchedulingPhaseResult,
    lock_result: ExternalLockScanResult,
    *,
    deviation_events: list[dict[str, object]],
    completion_events: list[dict[str, object]],
    promotion_events: list[dict[str, object]],
    applied: bool,
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
        skips=scheduling.skips,
        external_lock_conflicts={
            issue_number: [dataclasses.asdict(c) for c in conflicts]
            for issue_number, conflicts in lock_result.conflicts.items()
        },
        forge_warnings=[
            event
            for event in completion_events
            if event.get("action") == "completion_skipped_forge_error"
        ],
    )


def _run_gc_reclaim_phase(
    ctx: CycleContext,
    config: DispatcherConfig,
    completion_events: list[dict[str, object]],
    repair_cycle: _RepairCycleState,
) -> list[dict[str, object]]:
    gc_result = ctx.run_gc(tuple(completion_events))
    repair_cycle.add_report(gc_result.consistency)
    return gc_result.completion_events


def _execute_cycle_pipeline(
    ctx: CycleContext,
    issues: IssuesByStatus,
    run_state: RunState,
    config: DispatcherConfig,
    now: float,
    repair_cycle: _RepairCycleState,
    prior_parent_merge_events: tuple[dict[str, object], ...] = (),
) -> CycleReport:
    return execute_pipeline(
        sys.modules[__name__],
        ctx,
        issues,
        run_state,
        config,
        now,
        repair_cycle,
        prior_parent_merge_events,
    )


def _prepare_cycle_context(
    run_state: RunState, config: DispatcherConfig, now: float
) -> tuple[
    IssuesByStatus, CycleContext, ConsistencyCycleReport, PriorParentMergeReconciliation
]:
    """Issue取得・status intent整合・recovery・先行マージ整合を経てContextを構築する。"""
    issues = _prepare_cycle_issues(run_state, config, now)
    with progress_phase(config.progress, "status_intents"):
        reconcile_status_repair_intents(config, now=datetime.fromtimestamp(now, UTC))
    with progress_phase(config.progress, "recovery"):
        recovery_report = _run_recovery_bookkeeping_boundary(run_state, config, now=now)
    if _recovery_requeued(recovery_report):
        issues = _fetch_issues(config)
    tasks_by_issue, _, _ = _build_task_mappings(issues.all())
    with progress_phase(config.progress, "prior_parent_merges"):
        prior_merges = reconcile_prior_parent_merges(
            config.resolved_forge,
            tasks_by_issue,
            apply=config.apply,
            issues_by_number={issue.number: issue for issue in issues.all()},
            active_issue_numbers=frozenset(
                active.core.issue_number
                for active in run_state.active_worktrees.values()
            ),
        )
    with progress_phase(config.progress, "context_build"):
        actions = CycleActionAdapter(run_state, config, now)
        ctx = _build_cycle_context(
            issues,
            run_state,
            config,
            prior_parent_merge_hold_issue_numbers=prior_merges.held_issue_numbers,
            prior_parent_merge_completed_issue_numbers=prior_merges.completed_issue_numbers,
            actions=actions,
        )
        actions.bind_context(ctx)
    return issues, ctx, recovery_report, prior_merges


def run_dispatch_cycle(config: DispatcherConfig) -> CycleReport:
    return execute_cycle(sys.modules[__name__], config)
