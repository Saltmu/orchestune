from orchestune.dispatch_cycle import CycleContext
from orchestune.dispatch_launch import (
    _decide_duplicate_candidates,
    _decide_task_launch_plan,
    _decide_yaml_error_tasks,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import CompletedWorktree, RunState
from orchestune.dispatcher import DispatcherConfig
from orchestune.github import PrRecord


def _ctx(**overrides):
    defaults = dict(
        run_state=RunState(active_worktrees={}),
        tasks_by_issue={},
        issue_number_by_subtask_id={},
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=set(),
        changes_requested_subtask_ids=set(),
        subtask_branch_map={},
        prs=[],
        pr_by_branch={},
        config=DispatcherConfig(run_state_path="dummy.json", worktree_root="worktrees"),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


def _task(issue_number, subtask_id=None, yaml_error=False):
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id or f"task-{issue_number}",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:queued",),
        created_at="2023-01-01T00:00:00+00:00",
        depends_on=(),
        yaml_error=yaml_error,
    )


class TestDecideYamlErrorTasks:
    """decide層: 副作用なしでYAMLエラーのタスクのみを判定する。"""

    def test_returns_only_yaml_error_tasks(self):
        ok_task = _task(1)
        bad_task = _task(2, yaml_error=True)
        assert _decide_yaml_error_tasks([ok_task, bad_task]) == [bad_task]

    def test_returns_empty_when_no_errors(self):
        assert _decide_yaml_error_tasks([_task(1)]) == []


class TestDecideTaskLaunchPlan:
    """decide層: githubやworktreeへの副作用なしで起動計画のみを算出する。"""

    def test_uses_stack_base_branch_when_available(self):
        task = _task(1)
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )
        plans = _decide_task_launch_plan([task], {1: "claude/issue-0-task-0"}, config)
        assert len(plans) == 1
        assert plans[0].branch_name == "claude/issue-1-task-1"
        assert plans[0].base_branch_for_launch == "claude/issue-0-task-0"
        assert plans[0].base_branch_for_state == "claude/issue-0-task-0"

    def test_falls_back_to_origin_main_without_parent(self):
        task = _task(1)
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )
        plans = _decide_task_launch_plan([task], {}, config)
        assert plans[0].base_branch_for_launch is None
        assert plans[0].base_branch_for_state == "origin/main"

    def test_uses_parent_branch_when_configured(self):
        task = _task(1)
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            parent_issue_number=99,
        )
        plans = _decide_task_launch_plan([task], {}, config)
        assert plans[0].base_branch_for_launch == "parent/issue-99"
        assert plans[0].base_branch_for_state == "parent/issue-99"


class TestDecideDuplicateCandidates:
    """decide層: git ls-remoteの読み取りのみで重複判定し、githubへの書き込みは行わない。"""

    def test_no_existing_pr_is_not_duplicate(self):
        task = _task(1)
        decisions = _decide_duplicate_candidates([task], _ctx())
        assert len(decisions) == 1
        assert decisions[0].is_duplicate is False
        assert decisions[0].existing_pr is None

    def test_existing_pr_without_completion_history_is_duplicate(self):
        task = _task(1)
        pr = PrRecord(
            number=5,
            head_ref="claude/issue-1-task-1",
            changed_files=(),
            closes_issue_numbers=(1,),
        )
        ctx = _ctx(
            run_state=RunState(active_worktrees={}, completed_worktrees=[]),
            prs=[pr],
            pr_by_branch={"claude/issue-1-task-1": pr},
        )
        decisions = _decide_duplicate_candidates([task], ctx)
        assert decisions[0].is_duplicate is True
        assert decisions[0].existing_pr is pr

    def test_ls_remote_failure_is_treated_as_duplicate(self, monkeypatch):
        task = _task(1)
        pr = PrRecord(
            number=5,
            head_ref="claude/issue-1-task-1",
            changed_files=(),
            closes_issue_numbers=(1,),
        )
        run_state = RunState(
            active_worktrees={},
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="task-1",
                    branch="claude/issue-1-task-1",
                    started_at=0.0,
                    completed_at=1.0,
                    commit_sha="abc123",
                )
            ],
        )

        def _raise(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "orchestune.dispatch_launch.subprocess.run",
            _raise,
        )
        ctx = _ctx(
            run_state=run_state,
            prs=[pr],
            pr_by_branch={"claude/issue-1-task-1": pr},
        )
        decisions = _decide_duplicate_candidates([task], ctx)
        assert decisions[0].is_duplicate is True
