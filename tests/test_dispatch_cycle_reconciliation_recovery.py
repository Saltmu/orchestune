"""dispatch_cycle内の状態調整・整合性修復（dispatch_reconciliation.py関連）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、二重ステータス整合・
blocked昇格・自己修復・footprint逸脱recompute後の自動復帰系を分割している
（#343）。
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import (
    run_dispatch_cycle,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
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
            "orchestune.dispatch.execution_repair.is_process_alive",
            "orchestune.dispatch.gc.is_process_alive",
            "orchestune.dispatch.gc.completion.is_process_alive",
            "orchestune.dispatch.gc.zombies.is_process_alive",
        ):
            stack.enter_context(patch(target, return_value=return_value))
        yield


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default(fake_forge):
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    fake_forge.get_label_actor.reset_mock(side_effect=True)
    fake_forge.get_label_actor.return_value = "trusted-actor"
    fake_forge.get_actor_permission.reset_mock(side_effect=True)
    fake_forge.get_actor_permission.return_value = "write"
    yield


class TestDispatchCycleRecomputeExclusionAndRecovery:
    def test_same_cycle_recompute_exclusion(self, tmp_path, fake_forge):
        """同一サイクルで逸脱が発生した際、競合先タスクが candidate_tasks から除外されること"""

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

        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "some-user"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "write"
        with (
            patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch.cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch.cycle._process_active_worktrees",
                return_value=([], deviation_events, False, set()),
            ),
            patch(
                "orchestune.dispatch.cycle._run_status_repair_boundary",
                return_value=[],
            ),
            patch("orchestune.dispatch.cycle._sync_external_locks"),
            patch("orchestune.dispatch.phase_scheduling.save_run_state"),
            patch(
                "orchestune.dispatch.phase_scheduling._determine_candidate_tasks",
                return_value=([blocked_task, normal_task], {}, []),
            ),
            patch(
                "orchestune.dispatch.phase_scheduling.select_tasks_with_decisions"
            ) as mock_select,
        ):
            run_dispatch_cycle(config)

        called_candidates = mock_select.call_args[0][0]
        assert normal_task in called_candidates
        assert blocked_task not in called_candidates

    def test_recompute_resolved_automatic_recovery(self, tmp_path, fake_forge):
        """アクティブタスクが完了し競合が解消されたら、status:blocked-recomputeタスクが復帰すること"""

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

        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "some-user"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "write"
        with (
            patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch.cycle._fetch_issues", return_value=MockIssues()),
            patch("orchestune.dispatch.phase_scheduling.save_run_state"),
            patch(
                "orchestune.dispatch.phase_scheduling._determine_candidate_tasks",
                return_value=([], {}, []),
            ),
        ):
            run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked-recompute")
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")

    def test_recompute_recovery_error_fail_closed(self, tmp_path, fake_forge):
        """逸脱検知でエラー（None）が発生した際、自動復帰が抑止されること (fail-closed)"""

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

        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_label.reset_mock(side_effect=True)
        fake_forge.add_comment.reset_mock(side_effect=True)
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "some-user"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "write"
        with (
            patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch.cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch.cycle._process_active_worktrees",
                return_value=([], [], False, set()),
            ),
            patch("orchestune.dispatch.phase_scheduling.save_run_state"),
            patch(
                "orchestune.dispatch.phase_scheduling._determine_candidate_tasks",
                return_value=([], {}, []),
            ),
            patch(
                "orchestune.dispatch.locks.check_footprint_deviation", return_value=None
            ),  # エラー発生を模す
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()

    def test_recompute_dag_error_fail_closed(self, tmp_path, fake_forge):
        """DAG再計算で例外が発生した際、自動復帰が抑止されること (fail-closed)"""

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

        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        fake_forge.add_label.reset_mock(side_effect=True)
        fake_forge.add_comment.reset_mock(side_effect=True)
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.get_label_actor.reset_mock(side_effect=True)
        fake_forge.get_label_actor.return_value = "some-user"
        fake_forge.get_actor_permission.reset_mock(side_effect=True)
        fake_forge.get_actor_permission.return_value = "write"
        with (
            patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
            patch("orchestune.dispatch.cycle._fetch_issues", return_value=MockIssues()),
            patch(
                "orchestune.dispatch.cycle._process_active_worktrees",
                return_value=([], [], False, set()),
            ),
            patch("orchestune.dispatch.phase_scheduling.save_run_state"),
            patch(
                "orchestune.dispatch.phase_scheduling._determine_candidate_tasks",
                return_value=([], {}, []),
            ),
            patch(
                "orchestune.dispatch.locks.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),  # 逸脱ありとする
            patch(
                "orchestune.dispatch.reconciliation.recompute_dag_for_footprint_change",
                side_effect=ValueError("DAG error"),
            ),  # DAG計算エラー
        ):
            run_dispatch_cycle(config)

        # remove_label が呼ばれない（fail-closed）ことを検証
        mock_remove_label.assert_not_called()
