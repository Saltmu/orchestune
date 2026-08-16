"""dispatch_rebase内のリベース判定ルール（decide層）テスト。

`tests/test_dispatch_rebase.py`の肥大化解消のため分割している（#347）。
実際のgitコマンド実行を伴うrebase適用テストは`test_dispatch_rebase_git.py`、
通知処理・エンドツーエンド統合テストは`test_dispatch_rebase.py`に残している。
"""

from unittest.mock import MagicMock, patch

from orchestune.dag_models import compile_extra_ignore_patterns
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_rebase import (
    RebaseContext,
    _decide_footprint_deviation_outcome,
    _decide_rebase_needed,
    _decide_rebase_target,
    _try_auto_rebase,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:in-progress",),
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=(),
    )
    defaults.update(overrides)
    return Task(**defaults)


def _active(**overrides):
    defaults = dict(
        issue_number=1,
        branch="claude/issue-1-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _context(
    active: ActiveWorktree,
    task: Task,
    run_state: RunState,
    config: DispatcherConfig,
    *,
    ci_passed_pr_subtask_ids: set[str],
    subtask_branch_map: dict[str, str],
) -> RebaseContext:
    return RebaseContext(
        active=active,
        active_task=task,
        key="1",
        run_state=run_state,
        done_subtask_ids=set(),
        ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
        subtask_branch_map=subtask_branch_map,
        config=config,
    )


class TestDecideFootprintDeviationOutcome:
    """decide層: DAG再計算自体は純粋計算で、githubへの通知やactive/run_stateの
    変更は行わない。"""

    def test_already_forced_serial_is_noop(self, tmp_path):
        active = _active(forced_serial=True)
        decision = _decide_footprint_deviation_outcome(
            active,
            ["src/foo.py"],
            {},
            DispatcherConfig(
                events_log_path=tmp_path / "events.jsonl",
            ),
        )
        assert decision.action == "already_forced_serial"

    def test_unknown_subtask_is_skipped(self, tmp_path):
        active = _active()
        decision = _decide_footprint_deviation_outcome(
            active,
            ["src/foo.py"],
            {},
            DispatcherConfig(
                events_log_path=tmp_path / "events.jsonl",
            ),
        )
        assert decision.action == "skipped_unknown_subtask"

    def test_retry_limit_exceeded_forces_serial(self, tmp_path):
        active = _active(recompute_count=2)
        task = _task()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", max_recompute_retries=2
        )
        decision = _decide_footprint_deviation_outcome(
            active, ["src/foo.py"], {1: task}, config
        )
        assert decision.action == "forced_serial"
        assert decision.recompute_count == 2
        # decide層はactive.forced_serialを書き換えない
        assert active.forced_serial is False

    def test_under_retry_limit_recomputes(self, tmp_path):
        active = _active(recompute_count=0)
        task = _task()
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl", max_recompute_retries=2
        )
        decision = _decide_footprint_deviation_outcome(
            active, ["src/bar.py"], {1: task}, config
        )
        assert decision.action == "recomputed"
        assert decision.subtask_id == "task-a"
        # decide層はactive.recompute_countを書き換えない
        assert active.recompute_count == 0

    def test_recompute_reports_conflict_for_shared_manifest_by_default(self, tmp_path):
        # #398/#404: dag_ignore_patterns未指定時は既存挙動どおり、
        # 実行中に新たに触れたファイル（package.json）が既存の他サブタスクと
        # 衝突すれば競合として検知されること（後方互換の確認）
        active = _active(recompute_count=0, declared_footprint=("src/only_a.py",))
        task_a = _task(subtask_id="task-a", footprint=("src/only_a.py",))
        task_b = _task(issue_number=2, subtask_id="task-b", footprint=("package.json",))
        config = DispatcherConfig(events_log_path=tmp_path / "events.jsonl")
        decision = _decide_footprint_deviation_outcome(
            active, ["package.json"], {1: task_a, 2: task_b}, config
        )
        assert decision.action == "recomputed"
        assert len(decision.conflicts) == 1

    def test_dag_ignore_patterns_suppresses_recompute_conflict(self, tmp_path):
        # #398/#404: orchestune-dag向けに設定したdag_ignore_patternsは、
        # dispatcherの実行時DAG再計算（footprint逸脱時）にも適用され、
        # 初回検証で無視される設定のファイルの衝突を誤って競合検知しないこと
        active = _active(recompute_count=0, declared_footprint=("src/only_a.py",))
        task_a = _task(subtask_id="task-a", footprint=("src/only_a.py",))
        task_b = _task(issue_number=2, subtask_id="task-b", footprint=("package.json",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            dag_ignore_patterns=compile_extra_ignore_patterns([r"(^|/)package\.json$"]),
        )
        decision = _decide_footprint_deviation_outcome(
            active, ["package.json"], {1: task_a, 2: task_b}, config
        )
        assert decision.action == "recomputed"
        assert decision.conflicts == []

    def test_dag_similarity_threshold_is_forwarded_to_recompute(self, tmp_path):
        """#407/#415レビュー指摘: dag_similarity_thresholdもdag_ignore_patterns
        と同様に実行時DAG再計算（footprint逸脱時）へ伝搬させること。伝搬しないと、
        orchestune-dagで意図的に低い閾値へ変更したエッジがrecompute側の
        既定閾値で計算し直され、結果が食い違いうる。"""
        active = _active(recompute_count=0, declared_footprint=("src/only_a.py",))
        task_a = _task(subtask_id="task-a", footprint=("src/only_a.py",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            dag_similarity_threshold=0.1,
        )

        with patch(
            "orchestune.dispatch_rebase.recompute_dag_for_footprint_change",
            return_value=(MagicMock(), []),
        ) as mock_recompute:
            _decide_footprint_deviation_outcome(
                active, ["package.json"], {1: task_a}, config
            )

        assert mock_recompute.call_args.kwargs.get("threshold") == 0.1


class TestDecideRebaseTarget:
    def test_no_depends_on_returns_none(self):
        assert _decide_rebase_target(_task(depends_on=()), set(), set(), {}) is None

    def test_returns_branch_when_exactly_one_ci_passed_dependency_exists(self):
        task = _task(depends_on=("task-x", "task-y"))
        branch = _decide_rebase_target(
            task,
            {"task-x"},
            {"task-y"},
            {"task-y": "claude/issue-2-task-y"},
        )
        assert branch == "claude/issue-2-task-y"

    def test_no_ci_passed_dependency_returns_none(self):
        task = _task(depends_on=("task-x",))
        assert _decide_rebase_target(task, set(), set(), {}) is None

    def test_multiple_ci_passed_dependencies_return_none(self):
        task = _task(depends_on=("task-x", "task-y"))
        assert (
            _decide_rebase_target(
                task,
                set(),
                {"task-x", "task-y"},
                {
                    "task-x": "claude/issue-2-task-x",
                    "task-y": "claude/issue-3-task-y",
                },
            )
            is None
        )

    def test_unresolved_dependency_blocks_auto_rebase(self):
        task = _task(depends_on=("task-x", "task-y"))
        assert (
            _decide_rebase_target(
                task,
                set(),
                {"task-y"},
                {"task-y": "claude/issue-3-task-y"},
            )
            is None
        )

    def test_done_dependencies_are_ignored_when_exactly_one_ci_passed(self):
        task = _task(depends_on=("task-x", "task-y"))
        branch = _decide_rebase_target(
            task,
            {"task-x"},
            {"task-y"},
            {"task-y": "claude/issue-3-task-y"},
        )
        assert branch == "claude/issue-3-task-y"


class TestDecideRebaseNeeded:
    def test_ancestor_means_no_rebase_needed(self):
        with (
            patch("orchestune.dispatch_rebase.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
        ):
            mock_run.return_value.returncode = 0
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is False

    def test_not_ancestor_means_rebase_needed(self):
        with (
            patch("orchestune.dispatch_rebase.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
        ):
            mock_run.return_value.returncode = 1
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is True

    def test_returncode_128_logs_warning_and_returns_false(self):
        with (
            patch("orchestune.dispatch_rebase.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="nonexistent-branch",
            ),
            patch("orchestune.dispatch_rebase.logger.warning") as mock_warn,
        ):
            mock_run.return_value.returncode = 128
            mock_run.return_value.stderr = (
                "fatal: Not a valid object name nonexistent-branch"
            )
            assert (
                _decide_rebase_needed("nonexistent-branch", "feature", "worktrees/w1")
                is False
            )
            mock_warn.assert_called_once()
            assert "128" in mock_warn.call_args[0][0]

    def test_missing_ref_resolution_failure_logs_warning_and_returns_false(self):
        with (
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                side_effect=ValueError("Invalid ref name"),
            ),
            patch("orchestune.dispatch_rebase.logger.warning") as mock_warn,
        ):
            assert (
                _decide_rebase_needed("invalid..ref", "feature", "worktrees/w1")
                is False
            )
            mock_warn.assert_called_once()
            assert "Failed to resolve branch" in mock_warn.call_args[0][0]

    def test_os_error_logs_warning_and_returns_false(self):
        with (
            patch(
                "orchestune.dispatch_rebase.subprocess.run",
                side_effect=OSError("command not found"),
            ),
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="main",
            ),
            patch("orchestune.dispatch_rebase.logger.warning") as mock_warn,
        ):
            assert _decide_rebase_needed("main", "feature", "worktrees/w1") is False
            mock_warn.assert_called_once()
            assert "OSError" in mock_warn.call_args[0][0]


class TestTryAutoRebase:
    def test_rebase_not_needed_returns_false(self, tmp_path):
        active = _active(branch="feature")
        task = _task(depends_on=("task-parent",))

        ci_passed_pr_subtask_ids = {"task-parent"}
        subtask_branch_map = {"task-parent": "parent-branch"}

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch("orchestune.dispatch_rebase._apply_auto_rebase") as mock_apply,
        ):
            result = _try_auto_rebase(
                _context(
                    active,
                    task,
                    run_state,
                    config,
                    ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
                    subtask_branch_map=subtask_branch_map,
                )
            )

        assert result is False
        mock_apply.assert_not_called()

    def test_rebase_needed_returns_true(self, tmp_path):
        active = _active(branch="feature")
        task = _task(depends_on=("task-parent",))

        ci_passed_pr_subtask_ids = {"task-parent"}
        subtask_branch_map = {"task-parent": "parent-branch"}

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        )

        with (
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=True,
            ),
            patch("orchestune.dispatch_rebase._apply_auto_rebase") as mock_apply,
        ):
            context = _context(
                active,
                task,
                run_state,
                config,
                ci_passed_pr_subtask_ids=ci_passed_pr_subtask_ids,
                subtask_branch_map=subtask_branch_map,
            )
            result = _try_auto_rebase(context)

        assert result is True
        mock_apply.assert_called_once_with(context, "parent-branch")
