from pathlib import Path
from unittest.mock import patch

from orchestune.dispatch_cycle import (
    CycleContext,
    _apply_external_lock_sync,
    _decide_blocked_promotions,
    _decide_external_lock_sync,
    _fetch_issues,
    _group_by_status,
    _process_active_worktrees,
    _self_heal_run_state,
)
from orchestune.dispatch_locks import ExternalLockScanResult
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.dispatcher import DispatcherConfig
from orchestune.github import IssueRecord


def _task(**overrides):
    defaults = dict(
        issue_number=1,
        subtask_id="task-a",
        footprint=(),
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
        declared_footprint=(),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


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


class TestDecideBlockedPromotions:
    """decide層: 依存解決済みタスクの判定のみを行い、githubラベルは変更しない。"""

    def test_no_depends_on_is_not_promotable(self):
        task = _task(depends_on=())
        promotable = _decide_blocked_promotions([], [], set(), {1: task})
        assert promotable == []

    def test_unresolved_dependency_is_not_promotable(self):
        task = _task(depends_on=("task-x",))
        issue = IssueRecord(
            number=1, title="t", body="", labels=(), created_at="2026-01-01T00:00:00Z"
        )
        promotable = _decide_blocked_promotions([issue], [], set(), {1: task})
        assert promotable == []

    def test_resolved_via_completed_subtask_ids_is_promotable(self):
        task = _task(depends_on=("task-x",))
        issue = IssueRecord(
            number=1, title="t", body="", labels=(), created_at="2026-01-01T00:00:00Z"
        )
        promotable = _decide_blocked_promotions([issue], [], {"task-x"}, {1: task})
        assert promotable == [task]


class TestDecideExternalLockSync:
    """decide層: githubからの読み取りとscan_external_locksの純粋計算のみを行い、
    ラベルの書き込みは行わない。"""

    def test_no_bare_branches_means_no_locks(self):
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.dispatch_cycle.github.list_remote_branches",
                return_value=[],
            ),
        ):
            result = _decide_external_lock_sync({}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_ignores_invalid_remote_branch_name(self):
        run_state = RunState(active_worktrees={})
        task = _task(issue_number=1, footprint=("src/foo.py",))
        with (
            patch(
                "orchestune.dispatch_cycle.github.list_remote_branches",
                return_value=["origin/feature/foo@bar"],
            ),
        ):
            result = _decide_external_lock_sync({1: task}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []


class TestApplyExternalLockSync:
    def test_unlocking_blocked_task_does_not_requeue_it(self):
        task = _task(status_labels=("status:blocked", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        with (
            patch("orchestune.dispatch_cycle.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_cycle.github.remove_label") as mock_remove_label,
        ):
            _apply_external_lock_sync(lock_result, DispatcherConfig(apply=True))

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_in_progress_task_does_not_requeue_it(self):
        task = _task(status_labels=("status:in-progress", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        with (
            patch("orchestune.dispatch_cycle.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_cycle.github.remove_label") as mock_remove_label,
        ):
            _apply_external_lock_sync(lock_result, DispatcherConfig(apply=True))

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_queued_task_requeues_it(self):
        task = _task(status_labels=("status:queued", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        with (
            patch("orchestune.dispatch_cycle.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_cycle.github.remove_label") as mock_remove_label,
        ):
            _apply_external_lock_sync(lock_result, DispatcherConfig(apply=True))

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_called_once_with(1, "status:queued")


def _sub_issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"issue-{number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
        parent={"number": 100},
    )


class TestGroupByStatus:
    """#156: list_sub_issuesが返す親Issue配下の全Issueを、list_issues_by_label
    のstate引数（open/all）と同じ意味論でステータスラベル別に分類する。"""

    def test_groups_each_open_status_label(self):
        issues = [
            _sub_issue(1, labels=("status:queued",)),
            _sub_issue(2, labels=("status:external-lock",)),
            _sub_issue(3, labels=("status:in-progress",)),
            _sub_issue(4, labels=("status:blocked",)),
        ]
        result = _group_by_status(issues)
        assert [i.number for i in result.queued] == [1]
        assert [i.number for i in result.locked] == [2]
        assert [i.number for i in result.in_progress] == [3]
        assert [i.number for i in result.blocked] == [4]
        assert result.done == []
        assert result.not_needed == []

    def test_closed_open_only_labels_are_excluded(self):
        """status:queued/external-lock/in-progress/blockedはclosedなIssueを含めない
        （list_issues_by_labelの既定state="open"と同じ意味論）。"""
        issues = [_sub_issue(1, labels=("status:queued",), state="CLOSED")]
        result = _group_by_status(issues)
        assert result.queued == []

    def test_done_and_not_needed_include_closed(self):
        """status:done/not-neededはclosedでも含める
        （list_issues_by_labelのstate="all"呼び出しと同じ意味論）。"""
        issues = [
            _sub_issue(1, labels=("status:done",), state="CLOSED"),
            _sub_issue(2, labels=("status:not-needed",), state="CLOSED"),
        ]
        result = _group_by_status(issues)
        assert [i.number for i in result.done] == [1]
        assert [i.number for i in result.not_needed] == [2]

    def test_issue_with_multiple_status_labels_appears_in_each_bucket(self):
        issues = [_sub_issue(1, labels=("status:done", "status:not-needed"))]
        result = _group_by_status(issues)
        assert [i.number for i in result.done] == [1]
        assert [i.number for i in result.not_needed] == [1]


class TestFetchIssues:
    """#156: parent_issue_number指定時はlist_sub_issues経由のfast pathを、
    未指定時は従来通りlist_issues_by_labelを使う。"""

    def test_uses_list_sub_issues_when_parent_issue_number_is_set(self):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            parent_issue_number=100,
        )
        with (
            patch(
                "orchestune.dispatch_cycle.github.list_sub_issues",
                return_value=[_sub_issue(1, labels=("status:queued",))],
            ) as mock_sub_issues,
            patch(
                "orchestune.dispatch_cycle.github.list_issues_by_label",
                side_effect=AssertionError("Should not scan the whole repository"),
            ),
        ):
            result = _fetch_issues(config)

        mock_sub_issues.assert_called_once_with(100)
        assert [i.number for i in result.queued] == [1]

    def test_uses_list_issues_by_label_when_parent_issue_number_is_none(self):
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )
        with (
            patch(
                "orchestune.dispatch_cycle.github.list_issues_by_label",
                return_value=[],
            ) as mock_list,
            patch(
                "orchestune.dispatch_cycle.github.list_sub_issues",
                side_effect=AssertionError("Should not use the parent fast path"),
            ),
        ):
            _fetch_issues(config)

        assert mock_list.call_count == 6


class TestSelfHealRunState:
    """#156: run_state.jsonは複数の親Issue（big rock）にまたがって共有されうる
    ため、parent_issue_number指定時のfast pathでスコープが絞られていても、
    自己修復は常にリポジトリ全体のstatus:in-progress Issueを読み直す。"""

    def test_noop_when_run_state_file_exists(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")
        config = DispatcherConfig(
            run_state_path=run_state_path, worktree_root="worktrees", apply=True
        )
        run_state = RunState(active_worktrees={})
        with patch(
            "orchestune.dispatch_cycle.github.list_issues_by_label",
            side_effect=AssertionError("Should not fetch when file exists"),
        ):
            _self_heal_run_state(run_state, config)

    def test_noop_when_not_apply(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root="worktrees",
            apply=False,
        )
        run_state = RunState(active_worktrees={})
        with patch(
            "orchestune.dispatch_cycle.github.list_issues_by_label",
            side_effect=AssertionError("Should not fetch when apply=False"),
        ):
            _self_heal_run_state(run_state, config)

    def test_fetches_repo_wide_in_progress_issues_regardless_of_parent_scope(
        self, tmp_path
    ):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root="worktrees",
            apply=True,
            parent_issue_number=100,
        )
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.dispatch_cycle.github.list_issues_by_label",
                return_value=[],
            ) as mock_list,
            patch(
                "orchestune.dispatch_cycle.recover_run_state", return_value=False
            ) as mock_recover,
        ):
            _self_heal_run_state(run_state, config)

        mock_list.assert_called_once_with("status:in-progress")
        mock_recover.assert_called_once_with(run_state, [], config)


class TestProcessActiveWorktrees:
    """_process_active_worktreesの結合テストケース。

    各ruleの中身(条件判定=decide/実処理=act)は対応するact側モジュール
    (dispatch_gc/dispatch_escalation/dispatch_rebase)に定義されているため、
    patch対象はそれらのモジュールを指す（#86のComposite化に伴う移設）。
    """

    def test_auto_rebase_not_needed_falls_through_to_footprint_deviation(self):
        active = _active(
            branch="feature",
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
            recompute_count=0,
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            footprint=("a.py",),
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
        )

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch_rebase._handle_footprint_deviation",
                return_value={
                    "action": "recomputed",
                    "issue_number": 1,
                    "deviated_files": ["b.py"],
                },
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert completion_events == []
        assert len(deviation_events) == 1
        assert deviation_events[0]["action"] == "recomputed"
        assert deviation_events[0]["deviated_files"] == ["b.py"]
        assert any_forced_serial is False
        assert completed_subtask_ids == set()

    def test_dirty_worktree_skips_completion_and_does_not_fall_through(self):
        active = _active(
            branch="feature",
            declared_footprint=("a.py",),
            worktree_path="worktrees/w1",
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            footprint=("a.py",),
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
        )

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
            patch(
                "orchestune.dispatch_rebase._try_auto_rebase",
                side_effect=AssertionError("Should not call auto rebase"),
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                side_effect=AssertionError("Should not call check footprint deviation"),
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "completion_skipped_dirty_worktree"
        assert deviation_events == []
        assert any_forced_serial is False
        assert completed_subtask_ids == set()

    def test_not_needed_label_takes_precedence_over_stale_entry(self):
        active = _active(issue_number=1)
        task = _task(
            issue_number=1,
            status_labels=("status:not-needed", "status:blocked"),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_gc._decide_stale_active_entry",
                side_effect=AssertionError("Should not call decide stale active entry"),
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "not_needed"
        assert deviation_events == []
        assert completed_subtask_ids == {task.subtask_id}
        assert "1" not in ctx.run_state.active_worktrees

    def test_auto_rebase_failure_discards_active_entry(self):
        active = _active(
            branch="feature",
            worktree_path="worktrees/w1",
            pid=123,
        )
        task = _task(
            issue_number=1,
            subtask_id="task-child",
            depends_on=("task-parent",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            ci_passed_pr_subtask_ids={"task-parent"},
            subtask_branch_map={"task-parent": "parent-branch"},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        def mock_subprocess_run(args, **kwargs):
            import subprocess

            if "rebase" in args:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)

        with (
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
            patch("orchestune.dispatch_rebase.github.remove_label") as mock_remove,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_add,
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert completion_events == []
        assert deviation_events == []
        assert completed_subtask_ids == set()
        assert "1" not in ctx.run_state.active_worktrees
        mock_remove.assert_called_once_with(1, "status:in-progress")
        mock_add.assert_called_once_with(1, "status:manual-merge-required")
        mock_comment.assert_called_once_with(
            1,
            "自動リベース中にコンフリクトが発生しました。手動でマージを行ってください。\n対象の依存元ブランチ: parent-branch",
        )

    def test_forced_serial_persists_with_early_termination_rules(self):
        active = _active(
            issue_number=1,
            forced_serial=True,
        )
        task = _task(
            issue_number=1,
            status_labels=("status:not-needed",),
        )
        run_state = RunState(active_worktrees={"1": active})
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx)

        assert len(completion_events) == 1
        assert completion_events[0]["action"] == "not_needed"
        assert deviation_events == []
        assert completed_subtask_ids == {task.subtask_id}
        assert any_forced_serial is False
        assert "1" not in ctx.run_state.active_worktrees

        # 追加検証：もう1つの worktree があって、そちらは早期終了せず forced_serial=True の場合
        active_early = _active(issue_number=1, forced_serial=True)
        active_keep = _active(issue_number=2, forced_serial=True)
        task_early = _task(issue_number=1, status_labels=("status:not-needed",))
        task_keep = _task(issue_number=2, status_labels=("status:in-progress",))

        run_state_two = RunState(active_worktrees={"1": active_early, "2": active_keep})
        ctx_two = _ctx(
            run_state=run_state_two,
            tasks_by_issue={1: task_early, 2: task_keep},
            config=DispatcherConfig(
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch_gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch_gc._is_worktree_complete",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.is_process_alive",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
        ):
            (
                completion_events,
                deviation_events,
                any_forced_serial,
                completed_subtask_ids,
            ) = _process_active_worktrees(ctx_two)

        assert any_forced_serial is True
        assert "1" not in ctx_two.run_state.active_worktrees


class TestDispatchCycleRecomputeExclusionAndRecovery:
    def test_same_cycle_recompute_exclusion(self, tmp_path):
        """同一サイクルで逸脱が発生した際、競合先タスクが candidate_tasks から除外されること"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path, worktree_root="worktrees", apply=True
        )

        # 競合先タスクのTask定義
        blocked_task = _task(
            issue_number=2, subtask_id="task-blocked", status_labels=("status:queued",)
        )
        normal_task = _task(
            issue_number=3, subtask_id="task-normal", status_labels=("status:queued",)
        )

        blocked_body = "## Footprint\n" "```yaml\n" "subtask_id: task-blocked\n" "```\n"
        normal_body = "## Footprint\n" "```yaml\n" "subtask_id: task-normal\n" "```\n"
        blocked_issue = IssueRecord(
            2, "t", blocked_body, ("status:queued",), "2026-01-01T00:00:00Z"
        )
        normal_issue = IssueRecord(
            3, "t", normal_body, ("status:queued",), "2026-01-01T00:00:00Z"
        )

        class MockIssues:
            queued = [blocked_issue, normal_issue]
            locked = []
            in_progress = []
            blocked = []
            done = []
            not_needed = []

            def all(self):
                return [blocked_issue, normal_issue]

            def filtered_by_parent(self, parent):
                return self

        run_state = RunState(active_worktrees={})

        # _process_active_worktrees から返される deviation_events をシミュレート
        deviation_events = [
            {
                "action": "recomputed",
                "conflicts": [
                    {
                        "subtask_id": "task-active",
                        "other_subtask_id": "task-blocked",
                        "similarity": 0.8,
                        "blocked_subtask_id": "task-blocked",
                    }
                ],
            }
        ]

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch_cycle._process_active_worktrees",
                return_value=([], deviation_events, False, set()),
            ),
            patch("orchestune.dispatch_cycle._promote_blocked_tasks", return_value=[]),
            patch("orchestune.dispatch_cycle._sync_external_locks"),
            patch("orchestune.dispatch_cycle.github.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.github.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.dispatch_cycle.github.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([blocked_task, normal_task], {}),
            ),
            patch("orchestune.dispatch_cycle.select_next_tasks") as mock_select,
        ):
            run_dispatch_cycle(config)

        called_candidates = mock_select.call_args[0][0]
        assert normal_task in called_candidates
        assert blocked_task not in called_candidates

    def test_recompute_resolved_automatic_recovery(self, tmp_path):
        """アクティブタスクが完了し競合が解消されたら、status:blocked-recomputeタスクが復帰すること"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path, worktree_root="worktrees", apply=True
        )

        # アクティブワークツリーは空（競合なし）
        run_state = RunState(active_worktrees={})

        # status:blocked-recompute を持つ Issue
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-blocked\n"
            "footprint:\n"
            "  - src/bar.py\n"
            "```\n"
        )
        blocked_issue = IssueRecord(
            number=2,
            title="t",
            body=body,
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        class MockIssues:
            queued = []
            locked = []
            in_progress = []
            blocked = [blocked_issue]
            done = []
            not_needed = []

            def all(self):
                return [blocked_issue]

            def filtered_by_parent(self, parent):
                return self

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch("orchestune.dispatch_cycle.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_cycle.github.add_label") as mock_add_label,
            patch("orchestune.dispatch_cycle.github.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.github.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.dispatch_cycle.github.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
        ):
            run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked-recompute")
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")

    def test_recompute_recovery_error_fail_closed(self, tmp_path):
        """逸脱検知でエラー（None）が発生した際、自動復帰が抑止されること (fail-closed)"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        # アクティブワークツリーが存在する（逸脱検知を走らせるため）
        active = ActiveWorktree(
            issue_number=1,
            branch="branch-active",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1000.0,
            declared_footprint=("src/foo.py",),
            base_branch="origin/main",
        )
        run_state = RunState(active_worktrees={1: active})

        # status:blocked-recompute を持つ Issue
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-blocked\n"
            "footprint:\n"
            "  - src/bar.py\n"
            "```\n"
        )
        blocked_issue = IssueRecord(
            number=2,
            title="t",
            body=body,
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        active_body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-active\n"
            "footprint:\n"
            "  - src/foo.py\n"
            "```\n"
        )
        active_issue = IssueRecord(
            number=1,
            title="active",
            body=active_body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00Z",
        )

        class MockIssues:
            queued = []
            locked = []
            in_progress = [active_issue]
            blocked = [blocked_issue]
            done = []
            not_needed = []

            def all(self):
                return [active_issue, blocked_issue]

            def filtered_by_parent(self, parent):
                return self

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch_cycle._process_active_worktrees",
                return_value=([], [], False, set()),
            ),
            patch("orchestune.dispatch_cycle.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_cycle.github.add_label"),
            patch("orchestune.dispatch_cycle.github.add_comment"),
            patch("orchestune.dispatch_cycle.github.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.github.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.dispatch_cycle.github.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
            patch(
                "orchestune.dispatch_locks.check_footprint_deviation", return_value=None
            ),  # エラー発生を模す
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()

    def test_blocked_promotion_excludes_blocked_recompute(self):
        """status:blocked-recomputeを持つタスクは、通常のblocked昇格判定から除外されること"""
        from orchestune.dispatch_cycle import _decide_blocked_promotions

        task = _task(
            issue_number=2,
            subtask_id="task-blocked",
            status_labels=("status:blocked", "status:blocked-recompute"),
        )
        issue = IssueRecord(
            number=2,
            title="t",
            body="subtask_id: task-blocked\ndepends_on:\n  - task-dep",
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        # 依存先が完了している（done_issuesに含まれる）が、status:blocked-recompute があるため昇格しないはず
        dep_issue = IssueRecord(
            3, "dep", "subtask_id: task-dep", ("status:done",), "2026-01-01T00:00:00Z"
        )
        dep_task = _task(
            issue_number=3, subtask_id="task-dep", status_labels=("status:done",)
        )

        promotable = _decide_blocked_promotions(
            [issue],
            [dep_issue],
            completed_subtask_ids=set(),
            tasks_by_issue={2: task, 3: dep_task},
        )

        assert task not in promotable

    def test_recompute_dag_error_fail_closed(self, tmp_path):
        """DAG再計算で例外が発生した際、自動復帰が抑止されること (fail-closed)"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        active = ActiveWorktree(
            issue_number=1,
            branch="branch-active",
            worktree_path="worktrees/w1",
            pid=None,
            started_at=1000.0,
            declared_footprint=("src/foo.py",),
            base_branch="origin/main",
        )
        run_state = RunState(active_worktrees={1: active})

        # status:blocked-recompute を持つ Issue
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-blocked\n"
            "footprint:\n"
            "  - src/bar.py\n"
            "```\n"
        )
        blocked_issue = IssueRecord(
            number=2,
            title="t",
            body=body,
            labels=("status:blocked", "status:blocked-recompute"),
            created_at="2026-01-01T00:00:00Z",
        )

        active_body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-active\n"
            "footprint:\n"
            "  - src/foo.py\n"
            "```\n"
        )
        active_issue = IssueRecord(
            number=1,
            title="active",
            body=active_body,
            labels=("status:in-progress",),
            created_at="2026-01-01T00:00:00Z",
        )

        class MockIssues:
            queued = []
            locked = []
            in_progress = [active_issue]
            blocked = [blocked_issue]
            done = []
            not_needed = []

            def all(self):
                return [active_issue, blocked_issue]

            def filtered_by_parent(self, parent):
                return self

        with (
            patch("orchestune.dispatch_cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch_cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch_cycle._process_active_worktrees",
                return_value=([], [], False, set()),
            ),
            patch("orchestune.dispatch_cycle.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_cycle.github.add_label"),
            patch("orchestune.dispatch_cycle.github.add_comment"),
            patch("orchestune.dispatch_cycle.github.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatch_cycle.github.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.dispatch_cycle.github.get_actor_permission",
                return_value="write",
            ),
            patch("orchestune.dispatch_cycle.save_run_state"),
            patch(
                "orchestune.dispatch_cycle._determine_candidate_tasks",
                return_value=([], {}),
            ),
            patch(
                "orchestune.dispatch_locks.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),  # 逸脱ありとする
            patch(
                "orchestune.dag.recompute_dag_for_footprint_change",
                side_effect=ValueError("DAG error"),
            ),  # DAG計算エラー
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()
