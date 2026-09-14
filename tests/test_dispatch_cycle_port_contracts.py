"""Issue #881: 実行portの型契約と全件queryを固定する。

親#823「接続契約 v3」が定めた署名をそのまま検査する。

1. `ActivePhaseResult` / `StackBase` のフィールドと不変性、L3 `phase_gc`から
   L2へ移設した`GcPhaseResult`の再export
2. `CycleActions` / `CycleQueries` Protocolの署名——正例はこのファイル自身の
   型付き代入をCIの`mypy orchestune tests`が検証し、method欠落と戻り値誤りは
   別プロセスのnegative fixtureでmypy失敗を確認する
3. 全件query `tasks` / `issue_records` / `pull_requests` / `is_prior_merge_held`

配線（phaseからportを呼ぶ）と旧raw属性の撤去は#873以降の担当であり、ここでは
署名と観測の区別だけを固定する。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from orchestune.consistency.models import (
    ConsistencyScope,
    RepairCommand,
    RepairResult,
    RepairStatus,
)
from orchestune.consistency.supervisor import ConsistencyCycleReport, ConsistencyMode
from orchestune.dispatch import phase_gc
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_action_contracts import (
    ActivePhaseResult,
    CycleActions,
    CycleQueries,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import SchedulingResult
from orchestune.dispatch.state import ActiveWorktree, RunState
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, PrRecord, Task

_TMP = Path(tempfile.mkdtemp(prefix="orchestune-test-cycle-port-contracts-"))
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _task(issue_number, **overrides):
    defaults = dict(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(StatusLabel.QUEUED,),
        created_at="2026-01-01T00:00:00Z",
        issue_state="OPEN",
    )
    defaults.update(overrides)
    return Task(**defaults)


def _issue(issue_number, **overrides):
    defaults = dict(
        number=issue_number,
        title=f"issue {issue_number}",
        body="",
        labels=(StatusLabel.QUEUED,),
        created_at="2026-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return IssueRecord(**defaults)


def _pr(number, **overrides):
    defaults = dict(
        number=number,
        head_ref=f"claude/issue-{number}-task",
        changed_files=(),
    )
    defaults.update(overrides)
    return PrRecord(**defaults)


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        dependency_resolution={},
        done_issue_numbers=set(),
        ci_passed_pr_issue_numbers=set(),
        changes_requested_issue_numbers=set(),
        branch_by_issue_number={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(
            events_log_path=_TMP / "events.jsonl",
            run_state_path=_TMP / "run_state.json",
            worktree_root=_TMP / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


class _FakeCycleActions:
    """v3の7署名をそのまま満たす最小実装（型付き代入でmypyが構造検査する）。"""

    def process_active_worktrees(self) -> ActivePhaseResult:
        return ActivePhaseResult(
            completion_events=(), deviation_events=(), any_forced_serial=False
        )

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult:
        return GcPhaseResult(
            completion_events=[],
            consistency=ConsistencyCycleReport(mode=ConsistencyMode.OFF),
        )

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]:
        return ()

    def scan_external_locks(self) -> ExternalLockScanResult:
        return ExternalLockScanResult(to_lock=[], to_unlock=[])

    def select_tasks(self, candidates: tuple[Task, ...]) -> SchedulingResult:
        return SchedulingResult(selected=[], decisions=[])

    def launch_tasks(
        self,
        selected: tuple[Task, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[Task, ...],
    ) -> tuple[Task, ...]:
        return ()

    def execute_repair(self, command: RepairCommand) -> RepairResult:
        return RepairResult(command=command, status=RepairStatus.SKIPPED)


class TestPortValueTypes:
    """`ActivePhaseResult` / `StackBase` / 移設した`GcPhaseResult`。"""

    def test_active_phase_result_declares_exactly_three_fields(self):
        assert [f.name for f in dataclasses.fields(ActivePhaseResult)] == [
            "completion_events",
            "deviation_events",
            "any_forced_serial",
        ]

    def test_stack_base_declares_exactly_two_fields(self):
        assert [f.name for f in dataclasses.fields(StackBase)] == [
            "issue_number",
            "branch",
        ]

    def test_port_value_types_are_frozen(self):
        result = ActivePhaseResult(
            completion_events=({"event": "completed"},),
            deviation_events=(),
            any_forced_serial=True,
        )
        base = StackBase(issue_number=1, branch="claude/issue-1-task")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.any_forced_serial = False
        with pytest.raises(dataclasses.FrozenInstanceError):
            base.branch = "other"

    def test_gc_phase_result_is_reexported_from_phase_gc(self):
        # 定義はL2へ移設した。L2からL3をimportしないための移設であり、既存の
        # `from orchestune.dispatch.phase_gc import GcPhaseResult`は同じクラスを
        # 指し続ける。
        assert phase_gc.GcPhaseResult is GcPhaseResult
        assert "GcPhaseResult" in phase_gc.__all__

    def test_gc_phase_result_keeps_its_fields(self):
        assert [f.name for f in dataclasses.fields(GcPhaseResult)] == [
            "completion_events",
            "consistency",
        ]


class TestPortProtocols:
    """Protocol署名の正例・負例。"""

    def test_fake_actions_satisfies_cycle_actions(self):
        actions: CycleActions = _FakeCycleActions()
        assert actions.process_active_worktrees() == ActivePhaseResult(
            completion_events=(), deviation_events=(), any_forced_serial=False
        )
        assert (
            actions.execute_repair(
                RepairCommand(
                    code="noop", scope=ConsistencyScope.TASK, idempotency_key="k"
                )
            ).status
            is RepairStatus.SKIPPED
        )

    def test_cycle_context_satisfies_cycle_queries(self):
        queries: CycleQueries = _ctx(tasks_by_issue={1: _task(1)})
        assert [task.issue_number for task in queries.tasks()] == [1]

    def test_mypy_rejects_missing_method_and_wrong_return_type(self):
        """欠落method・戻り値誤りをmypyが拒否する（正例は同時に通ることを確認）。

        `CompliantActions`がエラーを出さないことも同じ実行で確認するため、
        import解決の失敗で負例が通ってしまう（偽陽性）ことがない。
        """
        completed = subprocess.run(
            [sys.executable, "-m", "mypy", "-c", _PROTOCOL_FIXTURE],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        errors = [line for line in completed.stdout.splitlines() if ": error:" in line]

        assert completed.returncode != 0, completed.stdout
        assert len(errors) == 2, completed.stdout
        assert "CompliantActions" not in completed.stdout, completed.stdout
        assert "MissingMethodActions" in errors[0]
        assert "execute_repair" in completed.stdout
        assert "WrongReturnActions" in errors[1]


# `mypy -c`へ渡す負例。`tests/`配下に型エラーを含むファイルを置くとCIの
# `mypy orchestune tests`自体が落ちるため、tree には残さずここから注入する。
_PROTOCOL_FIXTURE = """\
from orchestune.consistency.models import RepairCommand, RepairResult
from orchestune.dispatch.cycle_action_contracts import (
    ActivePhaseResult,
    CycleActions,
    GcPhaseResult,
    StackBase,
)
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.scoring import SchedulingResult, Task


class CompliantActions:
    def process_active_worktrees(self) -> ActivePhaseResult:
        raise NotImplementedError

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult:
        raise NotImplementedError

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]:
        raise NotImplementedError

    def scan_external_locks(self) -> ExternalLockScanResult:
        raise NotImplementedError

    def select_tasks(self, candidates: tuple[Task, ...]) -> SchedulingResult:
        raise NotImplementedError

    def launch_tasks(
        self,
        selected: tuple[Task, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[Task, ...],
    ) -> tuple[Task, ...]:
        raise NotImplementedError

    def execute_repair(self, command: RepairCommand) -> RepairResult:
        raise NotImplementedError


class MissingMethodActions:
    def process_active_worktrees(self) -> ActivePhaseResult:
        raise NotImplementedError

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult:
        raise NotImplementedError

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]:
        raise NotImplementedError

    def scan_external_locks(self) -> ExternalLockScanResult:
        raise NotImplementedError

    def select_tasks(self, candidates: tuple[Task, ...]) -> SchedulingResult:
        raise NotImplementedError

    def launch_tasks(
        self,
        selected: tuple[Task, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[Task, ...],
    ) -> tuple[Task, ...]:
        raise NotImplementedError


class WrongReturnActions:
    def process_active_worktrees(self) -> GcPhaseResult:
        raise NotImplementedError

    def run_gc(self, events: tuple[dict[str, object], ...]) -> GcPhaseResult:
        raise NotImplementedError

    def reconcile_recovery(self) -> tuple[dict[str, object], ...]:
        raise NotImplementedError

    def scan_external_locks(self) -> ExternalLockScanResult:
        raise NotImplementedError

    def select_tasks(self, candidates: tuple[Task, ...]) -> SchedulingResult:
        raise NotImplementedError

    def launch_tasks(
        self,
        selected: tuple[Task, ...],
        bases: tuple[StackBase, ...],
        candidates: tuple[Task, ...],
    ) -> tuple[Task, ...]:
        raise NotImplementedError

    def execute_repair(self, command: RepairCommand) -> RepairResult:
        raise NotImplementedError


compliant: CycleActions = CompliantActions()
missing: CycleActions = MissingMethodActions()
wrong: CycleActions = WrongReturnActions()
"""


class TestAllTaskQuery:
    """`tasks()`——全タスクのrecord反映後の実効値をIssue番号昇順で返す。"""

    def test_tasks_are_returned_in_issue_number_order(self):
        ctx = _ctx(tasks_by_issue={3: _task(3), 1: _task(1), 2: _task(2)})
        assert [task.issue_number for task in ctx.tasks()] == [1, 2, 3]

    def test_tasks_match_the_single_task_query(self):
        ctx = _ctx(tasks_by_issue={1: _task(1), 2: _task(2)})
        assert ctx.tasks() == (ctx.task(1), ctx.task(2))

    def test_tasks_include_tasks_the_candidate_views_filter_out(self):
        # `queued_tasks`/`blocked_tasks`は候補view（実効完了・実行中・非OPENを
        # 除外する）だが、`tasks()`は全件を返す。
        ctx = _ctx(
            tasks_by_issue={
                1: _task(1),
                2: _task(2, status_labels=(StatusLabel.DONE,), issue_state="CLOSED"),
            }
        )
        assert [task.issue_number for task in ctx.tasks()] == [1, 2]
        assert [task.issue_number for task in ctx.queued_tasks()] == [1]

    def test_tasks_reflect_a_confirmed_completion(self):
        ctx = _ctx(tasks_by_issue={1: _task(1)})
        ctx.record_completion(1)
        assert ctx.tasks() == (ctx.task(1),)
        assert StatusLabel.DONE in ctx.tasks()[0].status_labels

    def test_a_tuple_taken_before_a_record_does_not_change_afterwards(self):
        ctx = _ctx(tasks_by_issue={1: _task(1)})
        before = ctx.tasks()
        ctx.record_completion(1)
        assert before[0].status_labels == (StatusLabel.QUEUED,)
        assert ctx.tasks() != before

    def test_tasks_reflect_a_recorded_launch(self):
        # 実効値は完了だけでなく起動record後も`task()`と一致する。
        ctx = _ctx(tasks_by_issue={1: _task(1)})
        ctx.record_launch(_active_worktree(1))
        assert ctx.tasks() == (ctx.task(1),)
        assert StatusLabel.IN_PROGRESS in ctx.tasks()[0].status_labels


class TestInitialForgeObservationQueries:
    """`issue_records()` / `pull_requests()`——初期Forge観測を返す。"""

    def test_issue_records_are_returned_in_issue_number_order(self):
        ctx = _ctx(issue_records_by_number={3: _issue(3), 1: _issue(1)})
        assert [record.number for record in ctx.issue_records()] == [1, 3]

    def test_pull_requests_are_returned_in_pr_number_order(self):
        ctx = _ctx(prs=[_pr(5), _pr(2)])
        assert [pr.number for pr in ctx.pull_requests()] == [2, 5]

    def test_a_record_does_not_fake_the_observed_labels(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1)},
            issue_records_by_number={1: _issue(1)},
        )
        ctx.record_completion(1)
        assert ctx.issue_records()[0].labels == (StatusLabel.QUEUED,)
        assert StatusLabel.DONE in ctx.task(1).status_labels

    def test_mutating_a_returned_parent_does_not_change_the_observation(self):
        ctx = _ctx(issue_records_by_number={1: _issue(1, parent={"number": 823})})
        returned = ctx.issue_records()[0]
        assert returned.parent is not None
        returned.parent["number"] = 999
        assert ctx.issue_records()[0].parent == {"number": 823}

    def test_observed_collections_are_normalized_to_tuples(self):
        # `run_state.json`やForge実装経由でlistが届いても、所有時にtupleへ正規化
        # して返却値を不変にする（`_owned_task`と同じ扱い）。
        labels = [StatusLabel.QUEUED]
        blocked_by = [7]
        changed_files = ["src/a.py"]
        ctx = _ctx(
            issue_records_by_number={
                1: _issue(1, labels=labels, blocked_by=blocked_by)
            },
            prs=[_pr(2, changed_files=changed_files)],
        )
        labels.append(StatusLabel.DONE)
        blocked_by.append(8)
        changed_files.append("src/b.py")
        record = ctx.issue_records()[0]
        pr = ctx.pull_requests()[0]
        assert record.labels == (StatusLabel.QUEUED,)
        assert record.blocked_by == (7,)
        assert pr.changed_files == ("src/a.py",)

    def test_mutating_the_input_observation_afterwards_does_not_leak(self):
        parent = {"number": 823}
        records = {1: _issue(1, parent=parent)}
        prs = [_pr(2)]
        ctx = _ctx(issue_records_by_number=records, prs=prs)
        parent["number"] = 999
        records[4] = _issue(4)
        prs.append(_pr(9))
        assert ctx.issue_records()[0].parent == {"number": 823}
        assert [record.number for record in ctx.issue_records()] == [1]
        assert [pr.number for pr in ctx.pull_requests()] == [2]


class TestPriorMergeHoldQuery:
    """`is_prior_merge_held()`——既存の保留集合への所属をそのまま返す。"""

    def test_held_issue_is_reported(self):
        ctx = _ctx(
            tasks_by_issue={1: _task(1), 2: _task(2)},
            prior_parent_merge_hold_issue_numbers=frozenset({2}),
        )
        assert ctx.is_prior_merge_held(2) is True
        assert ctx.is_prior_merge_held(1) is False

    def test_membership_does_not_require_a_known_task(self):
        # 既存`phase_scheduling`の2箇所は素の所属判定であり、タスク母集団に
        # 無いIssueの保留も保留として扱う。ここで判定条件を増やさない。
        ctx = _ctx(prior_parent_merge_hold_issue_numbers=frozenset({7}))
        assert ctx.is_prior_merge_held(7) is True

    def test_launch_fact_query_still_requires_a_known_task(self):
        # 保留判定とライフサイクルqueryの違いを固定する（#868の意味を変えない）。
        ctx = _ctx(run_state=RunState(active_worktrees={"1": _active_worktree(1)}))
        assert ctx.launch_fact(1) is None


def _active_worktree(issue_number):
    return ActiveWorktree(
        issue_number=issue_number,
        branch=f"claude/issue-{issue_number}-task",
        worktree_path=f"worktrees/w{issue_number}",
        pid=1000 + issue_number,
        started_at=1_700_000_000.0,
        declared_footprint=(),
    )
