import tempfile
from pathlib import Path

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_launch import (
    _decide_duplicate_candidates,
    _decide_task_launch_plan,
    _decide_yaml_error_tasks,
    _get_stack_eligible_tasks,
)
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import CompletedWorktree, RunState
from orchestune.models import IssueRecord, PrRecord

tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
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

    def test_uses_stack_base_branch_when_available(self, tmp_path):
        task = _task(1)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        plans = _decide_task_launch_plan([task], {1: "claude/issue-0-task-0"}, config)
        assert len(plans) == 1
        assert plans[0].branch_name == "claude/issue-1-task-1"
        assert plans[0].base_branch_for_launch == "claude/issue-0-task-0"
        assert plans[0].base_branch_for_state == "claude/issue-0-task-0"

    def test_falls_back_to_origin_main_without_parent(self, tmp_path):
        task = _task(1)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        plans = _decide_task_launch_plan([task], {}, config)
        assert plans[0].base_branch_for_launch is None
        assert plans[0].base_branch_for_state == "origin/main"

    def test_uses_parent_branch_when_configured(self, tmp_path):
        task = _task(1)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
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

    def test_unrelated_closes_issue_pr_is_not_duplicate(self):
        task = _task(1)
        pr = PrRecord(
            number=6,
            head_ref="human/experiment",
            changed_files=(),
            closes_issue_numbers=(1,),
        )
        ctx = _ctx(
            run_state=RunState(active_worktrees={}, completed_worktrees=[]),
            prs=[pr],
            pr_by_branch={},
        )
        decisions = _decide_duplicate_candidates([task], ctx)
        assert decisions[0].is_duplicate is False
        assert decisions[0].existing_pr is None

    def test_orchestune_branch_closes_issue_pr_is_duplicate(self):
        task = _task(1)
        pr = PrRecord(
            number=7,
            head_ref="claude/issue-1-retry",
            changed_files=(),
            closes_issue_numbers=(1,),
        )
        ctx = _ctx(
            run_state=RunState(active_worktrees={}, completed_worktrees=[]),
            prs=[pr],
            pr_by_branch={},
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
            "orchestune.git_cli.subprocess.run",
            _raise,
        )
        ctx = _ctx(
            run_state=run_state,
            prs=[pr],
            pr_by_branch={"claude/issue-1-task-1": pr},
        )
        decisions = _decide_duplicate_candidates([task], ctx)
        assert decisions[0].is_duplicate is True


class TestApplyTaskLaunches:
    def test_invalid_subtask_id_blocks_only_affected_task(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import TaskLaunchPlan, _apply_task_launches
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        ok_task = _task(1, subtask_id="valid-task")
        bad_task = _task(2, subtask_id="invalid task@")

        plans = [
            TaskLaunchPlan(ok_task, "claude/issue-1-valid-task", None, "origin/main"),
            TaskLaunchPlan(
                bad_task, "claude/issue-2-invalid task@", None, "origin/main"
            ),
        ]

        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            selected = _apply_task_launches(plans, run_state, 1000.0, config)

        assert selected == [ok_task]
        assert mock_add_label.called
        label_calls = [
            (call.args[0], call.args[1]) for call in mock_add_label.call_args_list
        ]
        assert (2, "status:blocked-human-review") in label_calls
        assert mock_add_comment.called
        assert mock_add_comment.call_args[0][0] == 2

    def test_active_worktree_started_at_uses_dispatch_boundary_time_not_cycle_now(
        self, tmp_path
    ):
        """#262レビュー対応 Reproducer: ActiveWorktree.started_atがサイクル共通の
        `now`だと、各タスクの実起動（cloud routine fire等）は必ずそれより後の
        時刻になる。GitHub側created_at（秒精度）との丸め差でPR完了判定が
        誤ってstale扱いする境界ケースを避けるため、`create_worktree_and_launch`
        がworktree準備（prune/backup/add）完了後・dispatch_target.launch()
        呼び出し直前に取得した`dispatch_started_at`を使う（Codexレビュー指摘:
        launch呼び出し前に取得すると、worktree準備中に作成された無関係な
        既存PRまで新sessionの成果物と誤認する窓が生まれるため）。"""
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import TaskLaunchPlan, _apply_task_launches
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        task = _task(1, subtask_id="task-1")
        plans = [TaskLaunchPlan(task, "claude/issue-1-task-1", None, "origin/main")]
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )
        run_state = RunState(active_worktrees={})
        cycle_now = 1_000.0
        dispatch_boundary_time = 1_050.0

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch(
                "orchestune.dispatch_worktree.time.time",
                return_value=dispatch_boundary_time,
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(plans, run_state, cycle_now, config)

        active = run_state.active_worktrees["1"]
        assert active.started_at == dispatch_boundary_time
        assert active.started_at != cycle_now

    def test_invalid_subtask_id_with_resolved_dependency_is_not_requeued_on_next_cycle(
        self, tmp_path
    ):
        """不正なsubtask_idを持つタスクが解決済み依存関係を持っていても、1サイクル目で
        status:blocked-human-reviewに遷移し、2サイクル目以降にstatus:queuedへ自動昇格
        （再キュー）されないことを検証する。"""
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import TaskLaunchPlan, _apply_task_launches
        from orchestune.dispatch_reconciliation import _decide_blocked_promotions
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        # 解決済みの依存先dep-taskを持つ不正タスク(issue #2)
        bad_task = _task(2, subtask_id="invalid task@")
        bad_task = Task(
            issue_number=2,
            subtask_id="invalid task@",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:queued",),
            created_at="2023-01-01T00:00:00+00:00",
            depends_on=("dep-task",),
            yaml_error=False,
        )

        plans = [
            TaskLaunchPlan(
                bad_task, "claude/issue-2-invalid task@", None, "origin/main"
            ),
        ]

        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )
        run_state = RunState(active_worktrees={})

        added_labels = []

        def fake_add_label(issue_num, label):
            added_labels.append((issue_num, label))

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch("orchestune.forge.GitHubForge.add_label", side_effect=fake_add_label),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            selected = _apply_task_launches(plans, run_state, 1000.0, config)

        assert selected == []
        assert (2, "status:blocked-human-review") in added_labels

        # 2サイクル目: GitHub側は status:blocked-human-review ラベルが付与された状態
        # blocked_issues には status:blocked-human-review のIssueは入らない
        blocked_issues_cycle2 = []
        promotable = _decide_blocked_promotions(
            blocked_issues=blocked_issues_cycle2,
            done_issues=[],
            completed_subtask_ids={"dep-task"},
            tasks_by_issue={2: bad_task},
        )
        assert bad_task not in promotable


class TestApplyTaskLaunchesLabelOrdering:
    """#381: ラベル遷移はadd→removeの順で行われなければならない。remove→addの
    順だと、途中でプロセスがクラッシュした場合にIssueがどのstatus:*ラベルも
    持たない状態になり、以降のどのサイクルからも発見できなくなる。"""

    def test_success_path_adds_in_progress_before_removing_queued(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import TaskLaunchPlan, _apply_task_launches
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        task = _task(1, subtask_id="task-1")
        plans = [TaskLaunchPlan(task, "claude/issue-1-task-1", None, "origin/main")]
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )
        run_state = RunState(active_worktrees={})
        call_order: list[tuple[str, str]] = []

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(plans, run_state, 1000.0, config)

        assert call_order == [
            ("add", "status:in-progress"),
            ("remove", "status:queued"),
        ]

    def test_failure_path_adds_new_status_before_removing_queued(self, tmp_path):
        from unittest.mock import patch

        from orchestune.dispatch_launch import TaskLaunchPlan, _apply_task_launches
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        bad_task = _task(1, subtask_id="invalid task@")
        plans = [
            TaskLaunchPlan(
                bad_task, "claude/issue-1-invalid task@", None, "origin/main"
            )
        ]
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )
        run_state = RunState(active_worktrees={})
        call_order: list[tuple[str, str]] = []

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _apply_task_launches(plans, run_state, 1000.0, config)

        assert call_order == [
            ("add", "status:blocked-human-review"),
            ("remove", "status:queued"),
        ]


class TestApplyYamlErrorBlockingLabelOrdering:
    def test_adds_blocked_before_removing_queued(self, tmp_path):
        from unittest.mock import patch

        from orchestune.dispatch_launch import _apply_yaml_error_blocking

        task = _task(1, subtask_id="task-1", yaml_error=True)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        call_order: list[tuple[str, str]] = []

        with (
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            _apply_yaml_error_blocking([task], config)

        assert call_order == [("add", "status:blocked"), ("remove", "status:queued")]


class TestApplyTaskLaunchesRunStatePersistence:
    """#225レビュー対応: 起動ループ中の中間save_run_state呼び出しがconfig.window_seconds/
    open_prsを反映していないと、launch_historyの誤刈り込みやcompleted_worktreesの
    保護漏れ（重複起動誤判定）がクラッシュ時に再現してしまう。"""

    def _launch_plan(self, tmp_path):
        from orchestune.dispatch_launch import TaskLaunchPlan
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        task = _task(1, subtask_id="task-1")
        plans = [TaskLaunchPlan(task, "claude/issue-1-task-1", None, "origin/main")]
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        return plans, dispatch_target

    def test_preserves_launch_history_within_configured_window(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches
        from orchestune.dispatch_state import load_run_state, save_run_state

        plans, dispatch_target = self._launch_plan(tmp_path)
        run_state_path = tmp_path / "run_state.json"
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            window_seconds=172800,  # 48時間
        )

        now = 5_000_000.0
        launch_36h_ago = now - 129600.0  # デフォルト24時間窓の外、48時間窓の中
        save_run_state(
            RunState(launch_history=[launch_36h_ago]),
            run_state_path,
            now=now,
            launch_window_seconds=config.window_seconds,
        )
        run_state = load_run_state(run_state_path)

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(plans, run_state, now, config)

        # 起動ループ内の中間saveでも、48時間の設定ウィンドウが尊重され、
        # デフォルト24時間で誤って刈り込まれていないこと。
        persisted = load_run_state(run_state_path)
        assert launch_36h_ago in persisted.launch_history

    def test_protects_open_pr_completed_worktree_via_open_prs(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches
        from orchestune.dispatch_state import load_run_state
        from orchestune.models import PrRecord

        plans, dispatch_target = self._launch_plan(tmp_path)
        run_state_path = tmp_path / "run_state.json"
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )

        now = 5_000_000.0  # 30日以上前のcompleted_atは通常なら刈り込まれる
        old_completed = CompletedWorktree(
            issue_number=99,
            subtask_id="old-task",
            branch="claude/issue-99-old-task",
            started_at=100.0,
            completed_at=100.0,
            commit_sha="abc123",
        )
        run_state = RunState(active_worktrees={}, completed_worktrees=[old_completed])
        open_prs = [
            PrRecord(
                number=1,
                head_ref="claude/issue-99-old-task",
                changed_files=(),
                closes_issue_numbers=(99,),
            )
        ]

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(plans, run_state, now, config, open_prs=open_prs)

        # open PRに紐づく重複判定用の完了履歴が、中間saveの30日retentionで
        # 消えてしまわないこと（open_prsが正しく伝播していること）。
        persisted = load_run_state(run_state_path)
        assert any(cw.issue_number == 99 for cw in persisted.completed_worktrees)


class TestLaunchSelectedTasks:
    """#476: build_dispatch_target/_launch_selected_tasksの多引数をDTOへ集約した
    リファクタリング後も、decide+applyの薄いラッパーとしての挙動が維持されることを
    LaunchContext経由の呼び出しで検証する。"""

    def test_launches_selected_task_via_launch_context(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import LaunchContext, _launch_selected_tasks
        from orchestune.dispatch_targets import (
            LocalProcessDispatchTarget,
            default_dry_run_command_builder,
        )

        task = _task(1, subtask_id="task-1")
        dispatch_target = LocalProcessDispatchTarget(
            default_dry_run_command_builder, log_dir=tmp_path / "logs"
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
        )
        run_state = RunState(active_worktrees={})
        ctx = LaunchContext(
            selected=[task],
            task_to_base_branch={},
            candidate_tasks=[task],
            run_state=run_state,
            now=1000.0,
            config=config,
            open_prs=[],
        )

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            selected = _launch_selected_tasks(ctx)

        assert selected == [task]
        assert "1" in run_state.active_worktrees

    def test_blocks_yaml_error_candidate_not_included_in_selected(self, tmp_path):
        from unittest.mock import patch

        from orchestune.dispatch_launch import LaunchContext, _launch_selected_tasks

        bad_task = _task(1, subtask_id="bad-task", yaml_error=True)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )
        run_state = RunState(active_worktrees={})
        ctx = LaunchContext(
            selected=[],
            task_to_base_branch={},
            candidate_tasks=[bad_task],
            run_state=run_state,
            now=1000.0,
            config=config,
        )

        with (
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
        ):
            selected = _launch_selected_tasks(ctx)

        assert selected == []
        assert mock_add_label.called


class TestGetStackEligibleTasks:
    def test_uses_context_tasks_by_issue_over_raw_yaml_depends_on(self):
        """Issue #252: _get_stack_eligible_tasks が parse_task_from_issue で raw YAML を
        再パースせず、tasks_by_issue の context 済み Task（GitHub native blocked_by が反映されたもの）
        を優先して採用することを検証する。"""
        raw_body = """```yaml
subtask_id: task-2
depends_on:
  - yaml-dep
```"""
        issue2 = IssueRecord(
            number=2,
            title="Task 2",
            body=raw_body,
            labels=("status:blocked",),
            created_at="2026-01-01T00:00:00Z",
            blocked_by=(1,),
        )

        task1 = _task(1, subtask_id="gh-native-dep")
        task2 = Task(
            issue_number=2,
            subtask_id="task-2",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:blocked",),
            created_at="2026-01-01T00:00:00Z",
            depends_on=("gh-native-dep",),
            yaml_error=False,
        )

        tasks_by_issue = {1: task1, 2: task2}
        done_subtask_ids = set()
        ci_passed_pr_subtask_ids = {"gh-native-dep"}
        subtask_branch_map = {"gh-native-dep": "claude/issue-1-gh-native-dep"}

        eligible_tasks, base_branches = _get_stack_eligible_tasks(
            blocked_issues=[issue2],
            tasks_by_issue=tasks_by_issue,
            done_subtask_ids=done_subtask_ids,
            ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
            subtask_branch_map=subtask_branch_map,
        )

        assert eligible_tasks == [task2]
        assert base_branches == {2: "claude/issue-1-gh-native-dep"}

    def test_excludes_task_that_still_has_status_in_progress(self):
        # #381レビュー対応(Codex P2): stacked launch成功時のtransition_status_label
        # がadd(status:in-progress)後のremove(status:blocked)に失敗すると、
        # Issueがstatus:blocked/status:in-progressを同時に持つ中断状態のまま
        # 残りうる。稼働中セッションを新たなstack候補として二重に扱わないよう
        # 除外しなければならない。
        issue2 = IssueRecord(
            number=2,
            title="Task 2",
            body="",
            labels=("status:blocked", "status:in-progress"),
            created_at="2026-01-01T00:00:00Z",
        )
        task1 = _task(1, subtask_id="dep-task")
        dual_status_task = Task(
            issue_number=2,
            subtask_id="task-2",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:blocked", "status:in-progress"),
            created_at="2026-01-01T00:00:00Z",
            depends_on=("dep-task",),
            yaml_error=False,
        )

        eligible_tasks, _ = _get_stack_eligible_tasks(
            blocked_issues=[issue2],
            tasks_by_issue={1: task1, 2: dual_status_task},
            done_subtask_ids=set(),
            ci_passed_pr_subtask_ids={"dep-task"},
            subtask_branch_map={"dep-task": "claude/issue-1-dep-task"},
        )

        assert eligible_tasks == []

    def test_respects_unpassed_native_blocked_by_even_if_yaml_dep_passed(self):
        """GitHub blocked_byの依存先がCI未通過の場合、YAMLの依存先がCI通過していても
        スタッキング対象外となることを検証する。"""
        raw_body = """```yaml
subtask_id: task-2
depends_on:
  - yaml-passed-dep
```"""
        issue2 = IssueRecord(
            number=2,
            title="Task 2",
            body=raw_body,
            labels=("status:blocked",),
            created_at="2026-01-01T00:00:00Z",
            blocked_by=(1,),
        )

        task1 = _task(1, subtask_id="gh-unpassed-dep")
        task2 = Task(
            issue_number=2,
            subtask_id="task-2",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:blocked",),
            created_at="2026-01-01T00:00:00Z",
            depends_on=("gh-unpassed-dep",),
            yaml_error=False,
        )

        tasks_by_issue = {1: task1, 2: task2}
        done_subtask_ids = set()
        ci_passed_pr_subtask_ids = {"yaml-passed-dep"}
        subtask_branch_map = {
            "yaml-passed-dep": "claude/issue-0-yaml",
            "gh-unpassed-dep": "claude/issue-1-gh",
        }

        eligible_tasks, base_branches = _get_stack_eligible_tasks(
            blocked_issues=[issue2],
            tasks_by_issue=tasks_by_issue,
            done_subtask_ids=done_subtask_ids,
            ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
            subtask_branch_map=subtask_branch_map,
        )

        assert eligible_tasks == []
        assert base_branches == {}

    def test_fails_closed_when_native_blocker_missing_from_mapping(self):
        """Codex Review 指摘: blocked_by に複数Issue (例: 1, 3) が含まれるが、
        一部のIssue (3) が cycle context / mapping に含まれず省略されている場合、
        既知のblocker (1) が CI 通過していても fail closed となりスタッキング対象外となることを検証する。"""
        issue2 = IssueRecord(
            number=2,
            title="Task 2",
            body="subtask_id: task-2",
            labels=("status:blocked",),
            created_at="2026-01-01T00:00:00Z",
            blocked_by=(1, 3),
        )

        task1 = _task(1, subtask_id="dep-1")
        task2 = Task(
            issue_number=2,
            subtask_id="task-2",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:blocked",),
            created_at="2026-01-01T00:00:00Z",
            depends_on=("dep-1",),
            yaml_error=False,
        )

        tasks_by_issue = {1: task1, 2: task2}
        done_subtask_ids = set()
        ci_passed_pr_subtask_ids = {"dep-1"}
        subtask_branch_map = {"dep-1": "claude/issue-1-dep-1"}

        eligible_tasks, base_branches = _get_stack_eligible_tasks(
            blocked_issues=[issue2],
            tasks_by_issue=tasks_by_issue,
            done_subtask_ids=done_subtask_ids,
            ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
            subtask_branch_map=subtask_branch_map,
        )

        assert eligible_tasks == []
        assert base_branches == {}
