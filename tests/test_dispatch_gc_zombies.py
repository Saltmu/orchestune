"""dispatch_gc内のZombie・Timeout回収（collect/decide層）テスト。

`tests/test_dispatch_gc.py`の肥大化解消のため分割している（#345）。
完了ワークツリー処理は`test_dispatch_gc_completion.py`、gitプリミティブや
`dispatch_gc.py`自身のルール・エンドツーエンド統合テストは
`test_dispatch_gc.py`に残している。apply層（`_apply_zombie_or_timeout_reclaim`の
副作用検証）は再肥大化のため`test_dispatch_gc_zombies_apply.py`へ分割している（#829）。
"""

from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState
from tests.dispatch_gc_test_support import (
    decide_gc_reclaims as _decide_zombie_or_timeout_reclaims,
)
from tests.dispatch_gc_test_support import (
    run_gc_reclaims as _collect_zombies_and_timeouts,
)


def _active(**overrides):
    defaults = dict(
        issue_number=280,
        branch="claude/issue-280-task-a",
        worktree_path="worktrees/w1",
        pid=111,
        started_at=1_699_999_000.0,
        declared_footprint=("src/foo.py",),
    )
    defaults.update(overrides)
    return ActiveWorktree(**defaults)


def _task(**overrides):
    defaults = dict(
        issue_number=280,
        subtask_id="task-a",
        footprint=("src/foo.py",),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=("status:not-needed",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Task(**defaults)


class TestCollectZombiesAndTimeouts:
    def test_handleless_recovery_without_worktree_is_reclaimed(
        self, tmp_path, fake_forge
    ):
        """#383の自己修復entryはカーネル判断で回収される。"""
        active = _active(
            started_at=None,
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
        )
        run_state = RunState(active_worktrees={"280": active})
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
            forge=fake_forge,
        )

        with (
            patch("orchestune.dispatch.phase_gc.time.time", return_value=2_000.0),
        ):
            events = _collect_zombies_and_timeouts(
                run_state, {active.issue_number: task}, config
            )

        assert len(events) == 1
        assert events[0]["reason"] == "process disappeared"
        assert run_state.active_worktrees == {}
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")

    def test_timeout_without_physical_worktree_requeues_issue(
        self, tmp_path, fake_forge
    ):
        """#198: run_stateを削除するGC回収は、worktreeの有無にかかわらず
        GitHubのprimary stateもqueuedへ遷移させる。"""
        active = _active(
            started_at=1_000.0,
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=111,
        )
        run_state = RunState(active_worktrees={"280": active})
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
            forge=fake_forge,
        )

        with (
            patch("orchestune.dispatch.phase_gc.time.time", return_value=2_000.0),
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                autospec=True,
                return_value=True,
            ),
        ):
            events = _collect_zombies_and_timeouts(
                run_state, {active.issue_number: task}, config
            )

        assert events[0]["reason"] == "timeout exceeded"
        assert run_state.active_worktrees == {}
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")

    def test_held_worktree_is_not_reclaimed(self, tmp_path, fake_forge):
        """同一サイクルで人間確認待ちになったworktreeはGC対象から除外する。"""
        active = _active(pid=None)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            forge=fake_forge,
        )

        events = _collect_zombies_and_timeouts(
            run_state,
            {},
            config,
            held_worktree_paths={active.worktree_path},
        )

        assert events == []
        assert run_state.active_worktrees == {"280": active}
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_label.assert_not_called()
        fake_forge.add_comment.assert_not_called()


class TestDecideZombieOrTimeoutReclaims:
    """#233: decide層は副作用（github/os.kill/subprocess呼び出し）を一切行わない。"""

    def test_zombie_dead_process_with_dirty_worktree_is_reclaimed(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=111, started_at=None)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=0,
        )

        with (
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                autospec=True,
                return_value=False,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {280: _task(status_labels=("status:in-progress",))},
                config,
                None,
                now=2_000.0,
            )

        assert len(reclaims) == 1
        reclaim = reclaims[0]
        assert reclaim.reason == "process disappeared"
        assert reclaim.is_timeout is False
        assert reclaim.process_alive is False

    def test_dead_process_with_clean_worktree_is_reclaimed_as_zombie(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=111, started_at=None)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=0,
        )

        with (
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                autospec=True,
                return_value=False,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {280: _task(status_labels=("status:in-progress",))},
                config,
                None,
                now=2_000.0,
            )

        assert len(reclaims) == 1
        reclaim = reclaims[0]
        assert reclaim.reason == "process disappeared"
        assert reclaim.is_timeout is False
        assert reclaim.process_alive is False

    def test_cloud_handle_without_pid_is_not_reclaimed_as_zombie(self, tmp_path):
        """クラウド実行はローカルPIDを持たないため、進行中のセッションを
        process disappeared と誤認してはならない。"""
        active = _active(worktree_path=str(tmp_path), pid=None, started_at=1_000.0)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=0,
        )

        reclaims = _decide_zombie_or_timeout_reclaims(
            run_state, {}, config, None, now=2_000.0
        )

        assert reclaims == []

    def test_timeout_exceeded_reclaims_with_reason_timeout(self, tmp_path):
        active = _active(started_at=1_000.0, pid=111)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
        )

        with patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            autospec=True,
            return_value=True,
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {280: _task(status_labels=("status:in-progress",))},
                config,
                None,
                now=2_000.0,
            )

        assert len(reclaims) == 1
        reclaim = reclaims[0]
        assert reclaim.reason == "timeout exceeded"
        assert reclaim.is_timeout is True
        assert reclaim.process_alive is True

    def test_unknown_start_time_is_not_timed_out(self, tmp_path):
        active = _active(started_at=None, pid=111)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
        )

        with patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            autospec=True,
            return_value=True,
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert reclaims == []

    def test_self_healed_entry_without_worktree_or_start_time_is_reclaimed(
        self, tmp_path
    ):
        """#383の孤立entryはカーネルfindingから回収される。"""
        active = _active(
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
            started_at=None,
        )
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=0,
        )

        reclaims = _decide_zombie_or_timeout_reclaims(
            run_state,
            {280: _task(status_labels=("status:in-progress",))},
            config,
            None,
            now=2_000.0,
        )

        assert len(reclaims) == 1
        reclaim = reclaims[0]
        assert reclaim.reason == "process disappeared"
        assert reclaim.is_timeout is False
        assert reclaim.process_alive is False

    def test_held_worktree_path_is_excluded(self, tmp_path):
        active = _active(worktree_path=str(tmp_path), pid=111, started_at=None)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=0,
        )

        with (
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                autospec=True,
                return_value=False,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {},
                config,
                {active.worktree_path},
                now=2_000.0,
            )

        assert reclaims == []

    def test_zombie_and_timeout_disabled_returns_empty_immediately(self, tmp_path):
        active = _active()
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=False,
            task_timeout_seconds=0,
        )

        with patch(
            "orchestune.dispatch.execution_repair.is_process_alive", autospec=True
        ) as mock_is_alive:
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert reclaims == []
        mock_is_alive.assert_not_called()

    def test_subtask_id_resolved_from_tasks_by_issue(self, tmp_path):
        active = _active(started_at=1_000.0, pid=111)
        task = _task(status_labels=("status:in-progress",))
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
        )

        with patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            autospec=True,
            return_value=True,
        ):
            reclaims_with_task = _decide_zombie_or_timeout_reclaims(
                run_state, {active.issue_number: task}, config, None, now=2_000.0
            )
            reclaims_without_task = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert reclaims_with_task[0].subtask_id == task.subtask_id
        assert reclaims_without_task[0].subtask_id == ""

    def test_key_field_matches_active_worktrees_dict_key(self, tmp_path):
        active = _active(started_at=1_000.0, pid=111)
        run_state = RunState(active_worktrees={"custom-key": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
        )

        with patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            autospec=True,
            return_value=True,
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {280: _task(status_labels=("status:in-progress",))},
                config,
                None,
                now=2_000.0,
            )

        assert reclaims[0].key == "custom-key"
