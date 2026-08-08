"""dispatch_cycle内の状態調整・整合性修復（dispatch_reconciliation.py関連）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、二重ステータス整合・
blocked昇格・自己修復・footprint逸脱recompute後の自動復帰系を分割している
（#343）。
"""

import subprocess
from contextlib import ExitStack, contextmanager
from unittest.mock import ANY, patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import (
    run_dispatch_cycle,
)
from orchestune.dispatch_reconciliation import (
    _decide_blocked_promotions,
    _decide_dual_status_reconciliation,
    _reconcile_dual_status_tasks,
    _self_heal_run_state,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    save_run_state,
)
from orchestune.models import IssueRecord
from tests.conftest import make_issue


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


def _full_issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    """`_issue()`より詳細なFootprint YAMLブロックを持つIssueRecordを作る。

    `run_dispatch_cycle`をエンドツーエンドで駆動する系のテスト（旧
    `test_dispatcher.py`の`TestRunDispatchCycle*`群）が要求するフィールド
    （footprint/symbols/subtask_id/depends_on/parent_number）を持つため、
    より単純な`_issue()`とは別名にし、`tests/conftest.py`の`make_issue`に
    委譲する薄いラッパーにしている。
    """
    parent = {"number": parent_number} if parent_number is not None else None
    return make_issue(
        number,
        title="t",
        labels=labels,
        footprint=footprint,
        symbols=symbols,
        subtask_id=subtask_id,
        depends_on=depends_on,
        created_at=created_at,
        parent=parent,
    )


@contextmanager
def _patch_gc_process_alive(*, return_value: bool):
    """Patch every consumer split from the former dispatch_gc dependency."""
    with ExitStack() as stack:
        for target in (
            "orchestune.dispatch_gc.is_process_alive",
            "orchestune.dispatch_gc_completion.is_process_alive",
            "orchestune.dispatch_gc_zombies.is_process_alive",
        ):
            stack.enter_context(patch(target, return_value=return_value))
        yield


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default():
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    with (
        patch(
            "orchestune.forge.GitHubForge.get_label_actor",
            return_value="trusted-actor",
        ),
        patch(
            "orchestune.forge.GitHubForge.get_actor_permission",
            return_value="write",
        ),
    ):
        yield


class TestDualStatusReconciliation:
    def test_detects_tasks_with_both_done_and_queued(self):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        queued_only_task = _task(
            issue_number=2,
            subtask_id="task-b",
            status_labels=("status:queued",),
        )
        done_only_task = _task(
            issue_number=3,
            subtask_id="task-c",
            status_labels=("status:done",),
        )

        result = _decide_dual_status_reconciliation(
            {1: dual_status_task, 2: queued_only_task, 3: done_only_task}
        )

        assert [t.issue_number for t in result] == [1]

    def test_apply_removes_status_done_for_dual_status_tasks(self, tmp_path):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )

        with patch("orchestune.forge.GitHubForge.remove_label") as mock_remove:
            events = _reconcile_dual_status_tasks({1: dual_status_task}, config)

        mock_remove.assert_called_once_with(1, "status:done")
        assert events == [{"issue_number": 1, "subtask_id": "task-a"}]

    def test_dry_run_does_not_call_github(self, tmp_path):
        dual_status_task = _task(
            issue_number=1,
            subtask_id="task-a",
            status_labels=("status:done", "status:queued"),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )

        with patch("orchestune.forge.GitHubForge.remove_label") as mock_remove:
            _reconcile_dual_status_tasks({1: dual_status_task}, config)

        mock_remove.assert_not_called()


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


class TestSelfHealRunState:
    """#156: run_state.jsonは複数の親Issue（big rock）にまたがって共有されうる
    ため、parent_issue_number指定時のfast pathでスコープが絞られていても、
    自己修復は常にリポジトリ全体のstatus:in-progress Issueを読み直す。"""

    def test_noop_when_run_state_file_exists(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
        )
        run_state = RunState(active_worktrees={})
        with patch(
            "orchestune.forge.GitHubForge.list_issues_by_label",
            side_effect=AssertionError("Should not fetch when file exists"),
        ):
            _self_heal_run_state(run_state, config)

    def test_noop_when_not_apply(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
        )
        run_state = RunState(active_worktrees={})
        with patch(
            "orchestune.forge.GitHubForge.list_issues_by_label",
            side_effect=AssertionError("Should not fetch when apply=False"),
        ):
            _self_heal_run_state(run_state, config)

    def test_fetches_repo_wide_in_progress_issues_regardless_of_parent_scope(
        self, tmp_path
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=True,
            parent_issue_number=100,
        )
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.forge.GitHubForge.list_issues_by_label",
                return_value=[],
            ) as mock_list,
            patch(
                "orchestune.dispatch_reconciliation.recover_run_state",
                return_value=False,
            ) as mock_recover,
        ):
            _self_heal_run_state(run_state, config)

        mock_list.assert_called_once_with("status:in-progress")
        mock_recover.assert_called_once_with(run_state, [], config)


class TestDispatchCycleRecomputeExclusionAndRecovery:
    def test_same_cycle_recompute_exclusion(self, tmp_path):
        """同一サイクルで逸脱が発生した際、競合先タスクが candidate_tasks から除外されること"""
        from orchestune.dispatch_cycle import run_dispatch_cycle

        run_state_path = tmp_path / "run_state.json"
        run_state_path.write_text("{}")

        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
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
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            apply=True,
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
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
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
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
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
        from orchestune.dispatch_reconciliation import _decide_blocked_promotions

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
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch(
                "orchestune.forge.GitHubForge.get_label_actor",
                return_value="some-user",
            ),
            patch(
                "orchestune.forge.GitHubForge.get_actor_permission",
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
                "orchestune.dispatch_reconciliation.recompute_dag_for_footprint_change",
                side_effect=ValueError("DAG error"),
            ),  # DAG計算エラー
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()


class TestRunDispatchCycleBlockedPromotion:
    """#193: 依存解決によるstatus:blocked → status:queued昇格。"""

    def _config(self, tmp_path, **overrides):
        defaults = dict(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_promotes_blocked_task_when_dependency_already_done(self, tmp_path):
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_promotes_blocked_task_when_dependency_done_and_closed(self, tmp_path):
        """#236: 完了Issueが通常のGitHub運用でCloseされていても、
        status:done検索がstate="all"で呼ばれる限り依存解決できる。"""
        config = self._config(tmp_path)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, state="open"):
                # closedなIssueもstatus:done検索に含まれるのはstate="all"の
                # 呼び出しのみ（実際のgh issue list --state open/allの挙動を模す）。
                if label == "status:done" and state == "all":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_does_not_promote_when_dependency_unresolved(self, tmp_path):
        config = self._config(tmp_path)
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            mock_list.side_effect = lambda label, **_: (
                [blocked_issue] if label == "status:blocked" else []
            )
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.promotion_events == []

    def test_promotes_when_dependency_completes_in_same_cycle(self, tmp_path):
        """依存先が同一サイクル内で完了検知された場合も即座に昇格させる。"""
        run_state_path = tmp_path / "run_state.json"
        save_run_state(
            RunState(
                active_worktrees={
                    "1": ActiveWorktree(
                        issue_number=1,
                        branch="claude/issue-1-task-a",
                        worktree_path=str(tmp_path / "w1"),
                        pid=111,
                        started_at=1_699_999_000.0,
                        declared_footprint=("src/foo.py",),
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(tmp_path, run_state_path=run_state_path)
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # Completion now also consults the all-state PR list to rule out an
            # abandoned (closed-unmerged) PR before finalizing as "completed".
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
        ):

            def _list(label, **_):
                if label == "status:in-progress":
                    return [in_progress_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert {"issue_number": 2, "subtask_id": "task-b"} in report.promotion_events

    def test_dry_run_promotion_does_not_call_github(self, tmp_path):
        config = self._config(tmp_path, apply=False)
        done_issue = _full_issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _full_issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]

    def test_yaml_error_transitions_to_blocked(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        body = (
            "## Footprint\n"
            "```yaml\n"
            "subtask_id: task-invalid\n"
            "footprint:\n"
            "  - [invalid-yaml-structure:\n"
            "```\n"
        )
        issue = IssueRecord(
            number=9,
            title="t",
            body=body,
            labels=("status:queued",),
            created_at="2026-01-01T00:00:00+00:00",
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:queued" else []
            )

            report = run_dispatch_cycle(config)

            assert report.selected == []
            mock_remove_label.assert_any_call(9, "status:queued")
            mock_add_label.assert_any_call(9, "status:blocked")
            mock_add_comment.assert_called_once_with(9, ANY)

    def test_worktree_launch_failure_transitions_to_blocked(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        issue = _full_issue(1)
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:queued" else []
            )
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=128,
                cmd="git worktree add",
            )
            report = run_dispatch_cycle(config)

            assert report.selected == []
            mock_remove_label.assert_any_call(1, "status:queued")
            mock_add_label.assert_any_call(1, "status:blocked")
            mock_add_comment.assert_called_once_with(1, ANY)
