"""dispatch_cycle内の外部ロック同期（dispatch_locks.py関連）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、外部ロックの判定・適用と、
自己ロック回避のためのブランチ名正規化テストを分割している（#343）。
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import run_dispatch_cycle
from orchestune.dispatch_locks import ExternalLockScanResult
from orchestune.dispatch_phase_rebase import (
    _apply_external_lock_sync,
    _decide_external_lock_sync,
)
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    save_run_state,
)
from orchestune.models import PrRecord
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


class TestDecideExternalLockSync:
    """decide層: githubからの読み取りとscan_external_locksの純粋計算のみを行い、
    ラベルの書き込みは行わない。"""

    def test_no_bare_branches_means_no_locks(self):
        run_state = RunState(active_worktrees={})
        with (
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches",
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
                "orchestune.dispatch_phase_rebase.list_remote_branches",
                return_value=["origin/feature/foo@bar"],
            ),
        ):
            result = _decide_external_lock_sync({1: task}, [], run_state)
        assert result.to_lock == []
        assert result.to_unlock == []

    def test_diff_failure_keeps_existing_lock_and_locks_queued_tasks(self):
        """#245: 差分取得不能（branch_changed_files=None）はfail closed。
        既存のexternal-lockは解除されず、footprintを持つqueued taskはlockされる。"""
        run_state = RunState(active_worktrees={})
        locked_task = _task(
            issue_number=1,
            footprint=("src/foo.py",),
            status_labels=("status:external-lock",),
        )
        queued_task = _task(issue_number=2, footprint=("src/bar.py",))
        with (
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches",
                return_value=["origin/feat/x"],
            ),
            patch(
                "orchestune.dispatch_phase_rebase.branch_changed_files",
                return_value=None,
            ),
        ):
            result = _decide_external_lock_sync(
                {1: locked_task, 2: queued_task}, [], run_state
            )
        assert result.to_unlock == []
        assert [t.issue_number for t in result.to_lock] == [2]


class TestApplyExternalLockSync:
    def test_unlocking_blocked_task_does_not_requeue_it(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:blocked", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _apply_external_lock_sync(
            lock_result,
            DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True),
        )

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_in_progress_task_does_not_requeue_it(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:in-progress", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _apply_external_lock_sync(
            lock_result,
            DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True),
        )

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_not_called()

    def test_unlocking_queued_task_requeues_it(self, tmp_path, fake_forge):
        task = _task(status_labels=("status:queued", "status:external-lock"))
        lock_result = ExternalLockScanResult(to_lock=[], to_unlock=[task])

        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add_label = fake_forge.add_label
        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove_label = fake_forge.remove_label
        _apply_external_lock_sync(
            lock_result,
            DispatcherConfig(events_log_path=tmp_path / "events.jsonl", apply=True),
        )

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        mock_add_label.assert_called_once_with(1, "status:queued")


class TestRunDispatchCycleBranchNormalization:
    """#194: リモートブランチ名のorigin/プレフィックス正規化。"""

    def test_does_not_self_lock_own_active_branch(self, tmp_path, fake_forge):
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
                        declared_footprint=("src/shared.py",),
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(2, footprint=("src/shared.py",))
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches",
                return_value=["origin/claude/issue-1-task-a"],
            ),
            patch(
                "orchestune.dispatch_phase_rebase.branch_changed_files",
                return_value=["src/shared.py"],
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.lock_changes["to_lock"] == []

    def test_excludes_branch_with_open_pr_multisegment_headref(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        fake_forge.list_issues_by_label.return_value = []
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = [
            PrRecord(number=1, head_ref="feature/foo", changed_files=("src/x.py",))
        ]
        with (
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches",
                return_value=["origin/feature/foo"],
            ),
            patch(
                "orchestune.dispatch_phase_rebase.branch_changed_files"
            ) as mock_branch_files,
        ):
            run_dispatch_cycle(config)

        mock_branch_files.assert_not_called()

    def test_unrelated_external_branch_still_locks_overlapping_task(
        self, tmp_path, fake_forge
    ):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _full_issue(1, footprint=("src/shared.py",))
        fake_forge.list_issues_by_label.reset_mock(side_effect=True)
        mock_list = fake_forge.list_issues_by_label
        fake_forge.list_open_prs.reset_mock(side_effect=True)
        fake_forge.list_open_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches",
                return_value=["origin/someone-elses-branch"],
            ),
            patch(
                "orchestune.dispatch_phase_rebase.branch_changed_files",
                return_value=["src/shared.py"],
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert [t.issue_number for t in report.lock_changes["to_lock"]] == [1]
