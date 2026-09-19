"""1サイクル分の実行portと状態queryの型契約（#823 接続契約v3 / #881）。

`CycleQueries`は`CycleContext`が公開する意味付きの状態窓口、`CycleActions`は
各フェーズが呼ぶ実行portで、L3 `cycle_actions.py`が実装する（#884〜#886）。
本モジュールは署名と値型だけを固定し、実装adapterは持たない。

Protocolのために**L2からL3をimportしない**。`GcPhaseResult`はそのために
L3 `phase_gc`から内容を変えずここへ移設したもので、`phase_gc`が再exportして
既存importを維持する。`ExternalLockScanResult`や`SchedulingResult`のような
既存の結果型は再利用し、共有状態のSnapshotへ転用しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestune.consistency.models import RepairCommand, RepairResult
from orchestune.consistency.supervisor import ConsistencyCycleReport
from orchestune.dag.models import SubTask
from orchestune.dispatch.cycle_context_state import LaunchFact, RecordResult
from orchestune.dispatch.dependency_assessment import DependencyAssessment
from orchestune.dispatch.dependency_resolution import TaskDependencies
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.scoring import SchedulingResult
from orchestune.dispatch.state import ActiveWorktree
from orchestune.models import IssueRecord, PrRecord
from orchestune.task_branch_resolution import TaskBranchResolution
from orchestune.task_metadata import CycleTask, TaskMetadata


@dataclass(frozen=True, slots=True)
class ActivePhaseResult:
    """active worktreeフェーズが返すレポート用イベントと直列化フラグ。

    イベントdictは既存レポート用のコピーであり、内部Task/RunStateへの別名参照を
    持たない。
    """

    completion_events: tuple[dict[str, object], ...]
    deviation_events: tuple[dict[str, object], ...]
    any_forced_serial: bool


@dataclass(frozen=True, slots=True)
class StackBase:
    """起動時にstack可能と判定された依存先のbase。"""

    issue_number: int
    branch: str


@dataclass(frozen=True, slots=True)
class GcPhaseResult:
    """Events and typed repair audit data produced at the GC boundary."""

    completion_events: list[dict]
    consistency: ConsistencyCycleReport


class CycleQueries(Protocol):
    """1サイクルの意味付き状態を読み書きする単一窓口。

    `task`/`tasks`はrecord反映後の実効値、`issue_records`/`pull_requests`は
    初期Forge観測を返す。全件queryはIssue/PR番号昇順。Taskの返却型は
    raw依存宣言を持たない不変な`CycleTask`として返す。
    """

    def task(self, issue_number: int) -> CycleTask | None: ...

    def dependencies_of(self, issue_number: int) -> TaskDependencies | None: ...

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None: ...

    def is_effectively_done(self, issue_number: int) -> bool: ...

    def is_completion_confirmed(self, issue_number: int) -> bool: ...

    def has_changes_requested(self, issue_number: int) -> bool: ...

    def is_ci_passed(self, issue_number: int) -> bool: ...

    def canonical_branch(self, issue_number: int) -> str | None: ...

    def branch_resolution(self, issue_number: int) -> TaskBranchResolution | None: ...

    def launch_fact(self, issue_number: int) -> LaunchFact | None: ...

    def queued_tasks(self) -> tuple[CycleTask, ...]: ...

    def blocked_tasks(self) -> tuple[CycleTask, ...]: ...

    def tasks(self) -> tuple[CycleTask, ...]: ...

    def issue_records(self) -> tuple[IssueRecord, ...]: ...

    def pull_requests(self) -> tuple[PrRecord, ...]: ...

    def is_prior_merge_held(self, issue_number: int) -> bool: ...

    def dag_inputs(self, issue_numbers: tuple[int, ...]) -> tuple[SubTask, ...]: ...

    def record_completion(self, issue_number: int) -> RecordResult: ...

    def record_launch(self, active: ActiveWorktree) -> RecordResult: ...

    def record_transition(
        self,
        issue_number: int,
        *,
        expected_labels: tuple[str, ...],
        verified_labels: tuple[str, ...],
        execution_active: bool,
    ) -> RecordResult: ...


class CycleActions(Protocol):
    """フェーズが呼ぶ実行port。実装だけが同一のRunStateを低レベルactへ渡す。"""

    def process_active_worktrees(self) -> ActivePhaseResult: ...

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult: ...

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]: ...

    def scan_external_locks(self) -> ExternalLockScanResult: ...

    def select_tasks(
        self, candidates: tuple[TaskMetadata, ...]
    ) -> SchedulingResult: ...

    def launch_tasks(
        self,
        selected: tuple[TaskMetadata, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[TaskMetadata, ...],
    ) -> tuple[TaskMetadata, ...]: ...

    def execute_repair(self, command: RepairCommand) -> RepairResult: ...


__all__ = [
    "ActivePhaseResult",
    "CycleActions",
    "CycleQueries",
    "GcPhaseResult",
    "StackBase",
]
