"""Cycle task query view and mutable facts shared with the cycle recorder."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.invariants.status import (
    primary_status_labels,
)
from orchestune.dag.models import SubTask
from orchestune.dependencies.assessment import (
    DependencyAssessment,
)
from orchestune.dependencies.assessment import (
    assess_dependencies as assess_dependency_lifecycle,
)
from orchestune.dependencies.resolution import TaskDependencies, build_legacy_dag_inputs
from orchestune.dispatch.status_repair_dependencies import task_lifecycle
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, PrRecord, Task
from orchestune.task_branch_resolution import TaskBranchResolution
from orchestune.task_metadata import CycleTask

_TERMINAL_LIFECYCLE = (TaskLifecycle.DONE, TaskLifecycle.NOT_NEEDED)


@dataclass(frozen=True, slots=True)
class LaunchFact:
    """`ActiveWorktree`から投影した、起動の確定事実。

    `ActiveWorktree`自体への参照ではなく、必要なフィールドだけの不変値。
    """

    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    started_at: float | None
    external_id: str | None
    launch_attempt_id: str | None
    owner_kind: str = "dispatch"
    claim_id: str | None = None
    reservation_kind: str = "footprint"


@dataclass
class _LaunchState:
    """1Issue分の起動状態。`fact=None`かつ`indeterminate=True`は、起動前・
    結果不明・handle欠如、または同一Issueに複数のActiveWorktreeが観測された
    曖昧な状態を表す——いずれも候補viewから除外し、`record_launch`はこの状態が
    解消されるまで（`execution_active=False`の回収遷移まで）常に競合を返す。
    """

    fact: LaunchFact | None
    active: bool
    indeterminate: bool


def _owned_task(task: Task) -> Task:
    """Freeze nested collections while retaining already immutable DTOs."""
    fields = (
        "footprint",
        "symbols",
        "status_labels",
        "depends_on",
        "native_depends_on",
    )
    if all(isinstance(getattr(task, name), tuple) for name in fields):
        return task
    return dataclasses.replace(
        task,
        footprint=tuple(task.footprint),
        symbols=tuple(task.symbols),
        status_labels=tuple(task.status_labels),
        depends_on=tuple(task.depends_on),
        native_depends_on=tuple(task.native_depends_on),
    )


def _owned_issue_record(record: IssueRecord) -> IssueRecord:
    """可変な`parent`を切り離し、コレクションをtupleへ正規化したコピーを返す。

    所有時と返却時の両方で使う。`IssueRecord`自体はfrozenだが`parent`はdictなので、
    共有したままにすると呼出側の変更が内部観測へ伝わる。すでに
    `parent is None`かつコレクションがtupleなら、値全体が不変なのでそのまま返す。
    """
    if (
        record.parent is None
        and isinstance(record.labels, tuple)
        and isinstance(record.blocked_by, tuple)
    ):
        return record
    return dataclasses.replace(
        record,
        labels=tuple(record.labels),
        blocked_by=tuple(record.blocked_by),
        parent=None if record.parent is None else dict(record.parent),
    )


def _owned_pull_request(pr: PrRecord) -> PrRecord:
    """コレクションをtupleへ正規化したコピーを返す。

    正規化後の`PrRecord`は全フィールドが不変値なので、返却ごとのコピーは不要。
    """
    if isinstance(pr.changed_files, tuple) and isinstance(
        pr.closes_issue_numbers, tuple
    ):
        return pr
    return dataclasses.replace(
        pr,
        changed_files=tuple(pr.changed_files),
        closes_issue_numbers=tuple(pr.closes_issue_numbers),
    )


def _owned_dependencies(deps: TaskDependencies) -> TaskDependencies:
    return TaskDependencies(
        resolved=tuple(deps.resolved),
        unresolved=tuple(
            dataclasses.replace(dep, candidates=tuple(dep.candidates))
            for dep in deps.unresolved
        ),
    )


def _owned_cycle_records(
    issue_records_by_number: Mapping[int, IssueRecord],
    prs: Iterable[PrRecord],
) -> tuple[tuple[IssueRecord, ...], tuple[PrRecord, ...]]:
    issue_records = tuple(
        sorted(
            (
                _owned_issue_record(record)
                for record in issue_records_by_number.values()
            ),
            key=lambda record: record.number,
        )
    )
    pull_requests = tuple(
        sorted(
            (_owned_pull_request(pr) for pr in prs),
            key=lambda pr: pr.number,
        )
    )
    return issue_records, pull_requests


@dataclass
class CycleLedger:
    """Mutable facts recorded during one cycle and read by its task view."""

    effective_labels: dict[int, tuple[str, ...]]
    recorded_completions: set[int]
    launch_states: dict[int, _LaunchState]


class CycleTaskView:
    """Read-only semantic task queries over owned observations and cycle facts."""

    def __init__(
        self,
        observations: Mapping[str, Any],
        ledger: CycleLedger,
        *,
        completion_blocked: Callable[[int], bool] | None = None,
    ) -> None:
        self._tasks = {
            number: _owned_task(task)
            for number, task in observations["tasks_by_issue"].items()
        }
        self._dependency_resolution = {
            number: _owned_dependencies(deps)
            for number, deps in observations["dependency_resolution"].items()
        }
        self._ci_passed = set(observations["ci_passed_pr_issue_numbers"])
        self._changes_requested = set(observations["changes_requested_issue_numbers"])
        self._branch_by_issue = dict(observations["branch_by_issue_number"])
        self._branch_resolutions = dict(observations["branch_resolutions_by_issue"])
        self._prior_completed = frozenset(
            observations["prior_parent_merge_completed_issue_numbers"]
        )
        self._prior_held = frozenset(
            observations["prior_parent_merge_hold_issue_numbers"]
        )
        self._prior_repair_attempted = frozenset(
            observations["prior_parent_merge_repair_attempted_issue_numbers"]
        )
        self._issue_records, self._pull_requests = _owned_cycle_records(
            observations["issue_records_by_number"], observations["prs"]
        )
        self._ledger = ledger
        self._completion_blocked = completion_blocked or (lambda issue_number: False)

    def has_task(self, issue_number: int) -> bool:
        return issue_number in self._tasks

    def labels(self, issue_number: int) -> tuple[str, ...]:
        if issue_number in self._ledger.effective_labels:
            return self._ledger.effective_labels[issue_number]
        task = self._tasks.get(issue_number)
        if task is not None:
            return tuple(task.status_labels)
        record = next(
            (r for r in self._issue_records if r.number == issue_number), None
        )
        return () if record is None else tuple(record.labels)

    def effective_task(self, issue_number: int) -> Task | None:
        base = self._tasks.get(issue_number)
        if base is None:
            return None
        labels = self.labels(issue_number)
        if labels == base.status_labels:
            return base
        return dataclasses.replace(base, status_labels=labels)

    def task(self, issue_number: int) -> CycleTask | None:
        task = self.effective_task(issue_number)
        return None if task is None else CycleTask.from_task(task)

    def dependencies_of(self, issue_number: int) -> TaskDependencies | None:
        if issue_number not in self._tasks:
            return None
        return self._dependency_resolution.get(issue_number)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        deps = self.dependencies_of(issue_number)
        if deps is None:
            return None
        return assess_dependency_lifecycle(deps, self)

    def is_effectively_done(self, issue_number: int) -> bool:
        if self._completion_blocked(issue_number):
            return False
        completed_override = (
            issue_number in self._ledger.recorded_completions
            or issue_number in self._prior_completed
        )
        if issue_number not in self._tasks:
            return completed_override
        return task_lifecycle(
            self.labels(issue_number), completed=completed_override
        ) in (_TERMINAL_LIFECYCLE)

    def is_completion_confirmed(self, issue_number: int) -> bool:
        """Return only completion established by a verified in-cycle transition."""
        if self._completion_blocked(issue_number):
            return False
        return (
            issue_number in self._ledger.recorded_completions
            or issue_number in self._prior_completed
        )

    def has_changes_requested(self, issue_number: int) -> bool:
        return issue_number in self._tasks and issue_number in self._changes_requested

    def is_ci_passed(self, issue_number: int) -> bool:
        return issue_number in self._tasks and issue_number in self._ci_passed

    def canonical_branch(self, issue_number: int) -> str | None:
        if issue_number not in self._tasks:
            return None
        launch_state = self._ledger.launch_states.get(issue_number)
        if launch_state is not None and launch_state.fact is not None:
            return launch_state.fact.branch
        return self._branch_by_issue.get(issue_number)

    def branch_resolution(self, issue_number: int) -> TaskBranchResolution | None:
        if issue_number not in self._tasks:
            return None
        return self._branch_resolutions.get(issue_number)

    def launch_fact(self, issue_number: int) -> LaunchFact | None:
        if issue_number not in self._tasks:
            return None
        launch_state = self._ledger.launch_states.get(issue_number)
        return None if launch_state is None else launch_state.fact

    def is_in_progress(self, issue_number: int) -> bool:
        launch_state = self._ledger.launch_states.get(issue_number)
        return launch_state is not None and launch_state.active

    def current_primary(self, issue_number: int) -> str | None:
        labels = self.labels(issue_number)
        primaries = primary_status_labels(labels)
        return primaries[0] if len(primaries) == 1 else None

    def candidate_tasks(self, wanted: str) -> tuple[CycleTask, ...]:
        matches: list[CycleTask] = []
        for issue_number, base in self._tasks.items():
            if base.issue_state != "OPEN":
                continue
            if self._completion_blocked(issue_number):
                continue
            if self.is_effectively_done(issue_number):
                continue
            if self.is_in_progress(issue_number):
                continue
            if self.current_primary(issue_number) != wanted:
                continue
            task = self.task(issue_number)
            assert task is not None
            matches.append(task)
        matches.sort(key=lambda t: t.issue_number)
        return tuple(matches)

    def queued_tasks(self) -> tuple[CycleTask, ...]:
        return self.candidate_tasks(StatusLabel.QUEUED)

    def blocked_tasks(self) -> tuple[CycleTask, ...]:
        return self.candidate_tasks(StatusLabel.BLOCKED)

    def tasks(self) -> tuple[CycleTask, ...]:
        """全タスクの実効値をIssue番号昇順で返す。

        `queued_tasks`/`blocked_tasks`は起動候補のviewなので実効完了・実行中・
        非OPENを除外するが、こちらはquota・critical path・conflictが必要とする
        全件母集団であり、除外しない。各要素は`task()`と同じ実効値。
        """
        effective: list[CycleTask] = []
        for issue_number in sorted(self._tasks):
            task = self.task(issue_number)
            assert task is not None
            effective.append(task)
        return tuple(effective)

    def issue_records(self) -> tuple[IssueRecord, ...]:
        """初期Forge観測をIssue番号昇順で返す。

        `record_*`の差分は反映しない——実効ラベルは`task()`が返し、ここは観測
        されたままの生ラベルを保つ。
        """
        return tuple(_owned_issue_record(record) for record in self._issue_records)

    def pull_requests(self) -> tuple[PrRecord, ...]:
        """初期Forge観測をPR番号昇順で返す。"""
        return self._pull_requests

    def is_prior_merge_held(self, issue_number: int) -> bool:
        """検証済み先行マージ等による起動保留集合への所属を返す。

        既存`phase_scheduling`の判定と同じ素の所属であり、タスク母集団に無い
        Issueの保留も保留として扱う（判定条件を増やさない）。
        """
        return issue_number in self._prior_held

    def is_prior_merge_repair_attempted(self, issue_number: int) -> bool:
        """先行マージ修復がこのcycleで書き込みを試みた（成功・失敗とも）Issueかを返す。

        cycle開始時のlistingはその書き込みより前の値なので、これらのIssueは
        同じcycleのstatus repairで古い観測に基づいて変更してはならない（#1300）。
        """
        return issue_number in self._prior_repair_attempted

    def dag_inputs(self, issue_numbers: tuple[int, ...]) -> tuple[SubTask, ...]:
        """指定順のIssue番号を実効`Task`へ解決し、派生`SubTask`入力を返す。

        `task()`と同じ実効値（record反映後）を使う。未知のIssue番号は母集団を
        黙って縮めず`ValueError`にする。raw宣言を返すqueryではない
        （`build_legacy_dag_inputs`がidentity境界内だけでraw値を扱う）。
        """
        tasks: list[Task] = []
        for issue_number in issue_numbers:
            task = self.effective_task(issue_number)
            if task is None:
                raise ValueError(f"unknown issue number: {issue_number}")
            tasks.append(task)
        return build_legacy_dag_inputs(tuple(tasks))
