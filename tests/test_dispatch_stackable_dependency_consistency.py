"""#860: スタッキング判定・自動リベース対象選定・ベースブランチ解決の3経路の一貫性テスト。

3経路:
1. launch._is_task_stack_eligible / _get_stack_eligible_tasks
2. rebase._decide_rebase_target
3. reconciliation._resolve_base_branch_for_task

同一の依存状況に対して、同一の「積んでよい依存先ブランチ」を導出し、
CI未通過や未解決依存がある場合は推測せず揃ってフォールバック/除外することを固定する。
"""

from __future__ import annotations

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.dependency_resolution import (
    REASON_AMBIGUOUS,
    TaskDependencies,
    UnresolvedDependency,
    resolve_stackable_dependency_issue,
)
from orchestune.dispatch.launch import _is_task_stack_eligible
from orchestune.dispatch.rebase import _decide_rebase_target
from orchestune.dispatch.reconciliation import _resolve_base_branch_for_task
from orchestune.models import Task


def _task(
    issue_number: int = 2,
    subtask_id: str = "task-b",
    depends_on: tuple[str, ...] = ("task-a",),
    parent_number: int | None = 100,
) -> Task:
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=depends_on,
        parent_number=parent_number,
    )


@pytest.fixture
def config(tmp_path):
    return DispatcherConfig(
        events_log_path=tmp_path / "events.jsonl",
        run_state_path=tmp_path / "run_state.json",
        parent_issue_number=100,
    )


class TestStackableDependencyConsistency:
    """3経路が同一入力に対して一貫した判断を行うことを検証する。"""

    def test_when_single_dependency_ci_passed_all_three_agree(self, config):
        """未完了依存がちょうど1件かつCI通過済みの場合、3経路すべてがそのブランチを採用する。"""
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        dep_resolution = {
            2: TaskDependencies(resolved=(1,)),
            1: TaskDependencies(),
        }
        done_issue_numbers = set()
        ci_passed_pr_issue_numbers = {1}
        branch_by_issue_number = {1: "claude/issue-1-task-a"}

        # 共通ヘルパ
        dep_issue = resolve_stackable_dependency_issue(
            task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
        )
        assert dep_issue == 1

        # 1. launch
        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is True
        assert stackable_deps == [1]

        # 2. rebase
        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target == "claude/issue-1-task-a"

        # 3. reconciliation
        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "claude/issue-1-task-a"

    def test_when_single_dependency_ci_not_passed_all_three_decline(self, config):
        """未完了依存のPRがCI未通過の場合、3経路すべてがスタック・リベースを見送り親/mainへ倒す。"""
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        dep_resolution = {
            2: TaskDependencies(resolved=(1,)),
            1: TaskDependencies(),
        }
        done_issue_numbers = set()
        ci_passed_pr_issue_numbers = set()  # CI未通過
        branch_by_issue_number = {1: "claude/issue-1-task-a"}

        # 共通ヘルパ
        dep_issue = resolve_stackable_dependency_issue(
            task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
        )
        assert dep_issue is None

        # 1. launch
        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is False

        # 2. rebase
        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target is None

        # 3. reconciliation
        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "parent/issue-100"

    def test_when_single_dependency_changes_requested_all_three_decline(self, config):
        """CHANGES_REQUESTEDによりci_passedから除外されている場合、3経路すべてが見送る。"""
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        dep_resolution = {
            2: TaskDependencies(resolved=(1,)),
            1: TaskDependencies(),
        }
        done_issue_numbers = set()
        ci_passed_pr_issue_numbers = set()  # CHANGES_REQUESTED除外
        branch_by_issue_number = {1: "claude/issue-1-task-a"}

        assert (
            resolve_stackable_dependency_issue(
                task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
            )
            is None
        )

        all_ok, _ = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is False

        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target is None

        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "parent/issue-100"

    def test_when_dependency_is_unresolved_all_three_decline(self, config):
        """未解決依存がある場合、3経路すべてが依存先を推測せず見送る/親フォールバックする。"""
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        dep_resolution = {
            2: TaskDependencies(
                unresolved=(
                    UnresolvedDependency(raw="task-a", reason=REASON_AMBIGUOUS),
                ),
            )
        }
        done_issue_numbers = set()
        ci_passed_pr_issue_numbers = {1}
        branch_by_issue_number = {1: "claude/issue-1-task-a"}

        assert (
            resolve_stackable_dependency_issue(
                task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
            )
            is None
        )

        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is False
        assert stackable_deps == []

        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target is None

        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "parent/issue-100"

    def test_when_all_dependencies_done_all_three_agree_no_stack_needed(self, config):
        """依存がすべて完了済みの場合、スタック対象は無く、親/mainが土台となる。"""
        task = _task(issue_number=2, subtask_id="task-b", depends_on=("task-a",))
        dep_resolution = {
            2: TaskDependencies(resolved=(1,)),
        }
        done_issue_numbers = {1}
        ci_passed_pr_issue_numbers = {1}
        branch_by_issue_number = {1: "claude/issue-1-task-a"}

        assert (
            resolve_stackable_dependency_issue(
                task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
            )
            is None
        )

        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is True
        assert stackable_deps == []  # スタックは不要（通常起動可能）

        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target is None

        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "parent/issue-100"

    def test_when_multiple_dependencies_all_three_decline(self, config):
        """未完了依存が複数件ある場合、単一に絞れないため3経路すべてが依存先ブランチを採用しない。"""
        task = _task(
            issue_number=3, subtask_id="task-c", depends_on=("task-a", "task-b")
        )
        dep_resolution = {
            3: TaskDependencies(resolved=(1, 2)),
            1: TaskDependencies(),
            2: TaskDependencies(),
        }
        done_issue_numbers = set()
        ci_passed_pr_issue_numbers = {1, 2}
        branch_by_issue_number = {
            1: "claude/issue-1-task-a",
            2: "claude/issue-2-task-b",
        }

        assert (
            resolve_stackable_dependency_issue(
                task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
            )
            is None
        )

        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is True
        # launch では複数スタック可能でも len == 1 でないためスタック起動の対象外となる
        assert len(stackable_deps) == 2

        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target is None

        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "parent/issue-100"

    def test_when_no_dependencies_all_three_agree_no_stack(self, config):
        """依存が宣言されていない場合、3経路すべてがスタック・リベース対象外（親/main）とする。"""
        task = _task(issue_number=1, subtask_id="task-a", depends_on=())
        dep_resolution = {1: TaskDependencies()}
        done_issue_numbers = set()
        ci_passed_pr_issue_numbers = set()
        branch_by_issue_number = {}

        assert (
            resolve_stackable_dependency_issue(
                task, dep_resolution, done_issue_numbers, ci_passed_pr_issue_numbers
            )
            is None
        )

        all_ok, stackable_deps = _is_task_stack_eligible(
            task,
            dep_resolution,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            resolved_grand_deps=set(),
        )
        assert all_ok is True
        assert stackable_deps == []

        rebase_target = _decide_rebase_target(
            task,
            done_issue_numbers,
            ci_passed_pr_issue_numbers,
            branch_by_issue_number,
            dep_resolution,
        )
        assert rebase_target is None

        base_branch = _resolve_base_branch_for_task(
            task,
            config,
            branch_by_issue_number,
            done_issue_numbers,
            dep_resolution,
            ci_passed_pr_issue_numbers,
        )
        assert base_branch == "parent/issue-100"
