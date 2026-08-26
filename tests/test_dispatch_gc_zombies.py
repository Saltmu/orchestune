"""dispatch_gc内のZombie・Timeout回収（dispatch_gc_zombies）テスト。

`tests/test_dispatch_gc.py`の肥大化解消のため分割している（#345）。
完了ワークツリー処理は`test_dispatch_gc_completion.py`、gitプリミティブや
`dispatch_gc.py`自身のルール・エンドツーエンド統合テストは
`test_dispatch_gc.py`に残している。
"""

from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.gc.zombies import (
    ZombieOrTimeoutReclaim,
    _apply_zombie_or_timeout_reclaim,
    _collect_zombies_and_timeouts,
    _decide_zombie_or_timeout_reclaims,
)
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import ActiveWorktree, RunState


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
    def test_unknown_start_time_without_worktree_is_reclaimed_as_zombie(
        self, tmp_path, fake_forge
    ):
        """#383: 対応PR未検出のまま自己修復されたエントリ（pid=None,
        started_at=None, 物理worktree不在）は、タイムアウト判定は発火しない
        ものの、ゾンビ相当として回収されクオータを解放すること。"""
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
            patch("orchestune.dispatch.gc.zombies.time.time", return_value=2_000.0),
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
            patch("orchestune.dispatch.gc.zombies.time.time", return_value=2_000.0),
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
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch.gc.zombies.worktree_has_uncommitted_changes",
                return_value=True,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
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
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch.gc.zombies.worktree_has_uncommitted_changes",
                return_value=False,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert len(reclaims) == 1
        reclaim = reclaims[0]
        assert reclaim.reason == "process disappeared"
        assert reclaim.is_timeout is False
        assert reclaim.process_alive is False

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
            "orchestune.dispatch.gc.zombies.is_process_alive", return_value=True
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
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
            "orchestune.dispatch.gc.zombies.is_process_alive", return_value=True
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert reclaims == []

    def test_self_healed_entry_without_worktree_or_start_time_is_reclaimed(
        self, tmp_path
    ):
        """#383: run_state自己修復で対応PRが見つからず復元されたエントリ
        （pid=None, started_at=None, 物理worktree不在）は、通常のゾンビ判定
        （worktree実在+dirty）にもタイムアウト判定（started_at必須）にも
        永久に該当できずクオータを占有し続けていた。プロセス不在・worktree
        不在・開始時刻不明が揃った場合はゾンビ相当として回収されること。"""
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
            run_state, {}, config, None, now=2_000.0
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
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch.gc.zombies.worktree_has_uncommitted_changes",
                return_value=True,
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

        with patch("orchestune.dispatch.gc.zombies.is_process_alive") as mock_is_alive:
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert reclaims == []
        mock_is_alive.assert_not_called()

    def test_subtask_id_resolved_from_tasks_by_issue(self, tmp_path):
        active = _active(started_at=1_000.0, pid=111)
        task = _task()
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            task_timeout_seconds=60,
        )

        with patch(
            "orchestune.dispatch.gc.zombies.is_process_alive", return_value=True
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
            "orchestune.dispatch.gc.zombies.is_process_alive", return_value=True
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )

        assert reclaims[0].key == "custom-key"


class TestApplyZombieOrTimeoutReclaim:
    """#233: apply層はdecide層の判定結果に基づき副作用のみを担う。

    worktreeの存在有無は、ZombieOrTimeoutReclaimのフィールドとしては保持せず、
    apply実行の直前に毎回`os.path.exists`で再評価する（#236レビュー対応）。
    そのため各テストはreclaimに存在フラグを注入するのではなく、tmp_pathで
    実際のファイルシステム状態を用意して検証する。
    """

    def _reclaim(self, active, **overrides):
        defaults = dict(
            key="280",
            active=active,
            subtask_id="task-a",
            reason="process disappeared",
            is_timeout=False,
            process_alive=False,
        )
        defaults.update(overrides)
        return ZombieOrTimeoutReclaim(**defaults)

    def test_zombie_apply_removes_worktree_and_requeues(self, tmp_path, fake_forge):
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(active)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ) as mock_backup,
            patch("orchestune.dispatch.gc.zombies.os.kill") as mock_kill,
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_backup.assert_called_once_with(
            active.worktree_path, "WIP: backup by Orchestune GC (process disappeared)"
        )
        mock_kill.assert_not_called()
        mock_remove_worktree.assert_called_once_with(active.worktree_path)
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        fake_forge.add_comment.assert_called_once()
        assert run_state.active_worktrees == {}
        assert event == {
            "issue_number": 280,
            "subtask_id": "task-a",
            "action": "gc_reclaimed",
            "reason": "process disappeared",
            # #512: 今回を含む累計回収回数
            "reclaim_count": 1,
        }

    def test_reclaim_adds_queued_before_removing_in_progress(
        self, tmp_path, fake_forge
    ):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(active)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )
        call_order: list[tuple[str, str]] = []
        fake_forge.remove_label.side_effect = lambda issue, label: call_order.append(
            ("remove", label)
        )
        fake_forge.add_label.side_effect = lambda issue, label: call_order.append(
            ("add", label)
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill"),
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        assert call_order == [
            ("add", "status:queued"),
            ("remove", "status:in-progress"),
        ]

    def test_removes_stale_blocked_label_alongside_in_progress(
        self, tmp_path, fake_forge
    ):
        # #381レビュー対応(Codex P2): stacked launch中断で取り残された
        # status:blockedも併せて除去し、status:queuedへ確実に収束させる。
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active, status_labels=("status:blocked", "status:in-progress")
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill"),
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        fake_forge.remove_label.assert_any_call(280, "status:in-progress")
        fake_forge.remove_label.assert_any_call(280, "status:blocked")
        assert fake_forge.remove_label.call_count == 2

    def test_does_not_overwrite_terminal_escalation_label(self, tmp_path, fake_forge):
        # 中断した以前の遷移でstatus:manual-merge-requiredが既に付与されている
        # 場合、status:queuedへの書き換えは人間の確認要求を握りつぶしてしまう
        # ため、ラベルには一切触れてはならない。
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active,
            status_labels=("status:manual-merge-required", "status:in-progress"),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill"),
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        fake_forge.add_label.assert_not_called()
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_comment.assert_called_once()
        assert event is not None
        assert event["action"] == "gc_reclaimed"

    def test_timeout_apply_kills_alive_process(self, tmp_path, fake_forge):
        active = _active(pid=111, worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active, reason="timeout exceeded", is_timeout=True, process_alive=True
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_kill.assert_called_once_with(111, 9)

    def test_timeout_apply_kills_process_before_backing_up_wip(
        self, tmp_path, fake_forge
    ):
        """#385: タイムアウトかつプロセス生存中のケースでは、対象プロセスが
        まだworktreeへ書き込み中である可能性があるため、WIPバックアップより
        先にプロセスを停止（os.kill）しなければならない。順序が逆だと、
        書き込み途中の不整合なスナップショットやgit操作のロック競合を招く。"""
        active = _active(pid=111, worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active, reason="timeout exceeded", is_timeout=True, process_alive=True
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        call_order: list[str] = []

        def fake_kill(pid, sig):
            call_order.append("kill")

        def fake_backup(worktree_path, message):
            call_order.append("backup")
            return None

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit",
                side_effect=fake_backup,
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill", side_effect=fake_kill),
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        assert call_order == ["kill", "backup"]

    def test_timeout_apply_skips_kill_when_process_already_dead(
        self, tmp_path, fake_forge
    ):
        active = _active(pid=111, worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active, reason="timeout exceeded", is_timeout=True, process_alive=False
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_kill.assert_not_called()

    def test_timeout_apply_skips_kill_when_pid_is_none(self, tmp_path, fake_forge):
        active = _active(pid=None, worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active, reason="timeout exceeded", is_timeout=True, process_alive=True
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill") as mock_kill,
            patch("orchestune.dispatch.gc.zombies.remove_worktree"),
        ):
            _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_kill.assert_not_called()

    def test_kill_exception_is_swallowed(self, tmp_path, fake_forge):
        active = _active(pid=111, worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(
            active, reason="timeout exceeded", is_timeout=True, process_alive=True
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ),
            patch(
                "orchestune.dispatch.gc.zombies.os.kill", side_effect=Exception("boom")
            ),
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_remove_worktree.assert_called_once()
        fake_forge.remove_label.assert_called_once()
        fake_forge.add_label.assert_called_once()
        fake_forge.add_comment.assert_called_once()
        assert run_state.active_worktrees == {}
        assert event is not None

    def test_missing_worktree_skips_backup_and_remove_but_still_requeues(
        self, tmp_path, fake_forge
    ):
        active = _active(worktree_path=str(tmp_path / "missing"))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(active)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch("orchestune.dispatch.gc.zombies.backup_wip_commit") as mock_backup,
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_backup.assert_not_called()
        mock_remove_worktree.assert_not_called()
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        assert (
            "物理worktreeが見つからなかったため"
            in fake_forge.add_comment.call_args.args[1]
        )
        assert run_state.active_worktrees == {}
        assert event is not None

    def test_backup_failure_skips_reclaim_and_returns_none(self, tmp_path, fake_forge):
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(active)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit",
                return_value="git commit failed",
            ),
            patch("orchestune.dispatch.gc.zombies.os.kill") as mock_kill,
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        assert event is None
        fake_forge.add_comment.assert_called_once()
        assert "git commit failed" in fake_forge.add_comment.call_args.args[1]
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_label.assert_not_called()
        mock_kill.assert_not_called()
        mock_remove_worktree.assert_not_called()
        assert run_state.active_worktrees == {"280": active}

    def test_dry_run_returns_event_without_side_effects(self, tmp_path, fake_forge):
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(active)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=False,
            forge=fake_forge,
        )

        with (
            patch("orchestune.dispatch.gc.zombies.backup_wip_commit") as mock_backup,
            patch("orchestune.dispatch.gc.zombies.os.kill") as mock_kill,
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        mock_backup.assert_not_called()
        mock_kill.assert_not_called()
        mock_remove_worktree.assert_not_called()
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_label.assert_not_called()
        fake_forge.add_comment.assert_not_called()
        assert run_state.active_worktrees == {"280": active}
        assert event == {
            "issue_number": 280,
            "subtask_id": "task-a",
            "action": "gc_reclaimed",
            "reason": "process disappeared",
            # #512: 今回を含む累計回収回数
            "reclaim_count": 1,
        }

    def test_event_shape_omits_worktree_path(self, tmp_path):
        active = _active(worktree_path=str(tmp_path))
        run_state = RunState(active_worktrees={"280": active})
        reclaim = self._reclaim(active)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=False,
        )

        event = _apply_zombie_or_timeout_reclaim(run_state, reclaim, config)

        assert event is not None
        assert set(event.keys()) == {
            "issue_number",
            "subtask_id",
            "action",
            "reason",
            "reclaim_count",
        }

    def test_apply_backs_up_worktree_created_after_decide(self, tmp_path, fake_forge):
        """#236レビュー対応: decide時点では存在しなかったworktreeが、apply実行
        直前に作成された場合でも、apply側の再評価によりバックアップ・削除が
        正しく行われること（decide時点のスナップショットを固定して信用しない）。
        """
        worktree_dir = tmp_path / "wt"
        active = _active(worktree_path=str(worktree_dir), pid=None, started_at=1_000.0)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
            task_timeout_seconds=60,
        )

        with patch(
            "orchestune.dispatch.gc.zombies.is_process_alive", return_value=True
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )
        assert len(reclaims) == 1

        # decideからapply実行までの間にworktreeが作成されたことを模する。
        worktree_dir.mkdir()

        with (
            patch(
                "orchestune.dispatch.gc.zombies.backup_wip_commit", return_value=None
            ) as mock_backup,
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaims[0], config)

        mock_backup.assert_called_once_with(
            str(worktree_dir), "WIP: backup by Orchestune GC (timeout exceeded)"
        )
        mock_remove_worktree.assert_called_once_with(str(worktree_dir))
        assert event is not None

    def test_apply_skips_backup_when_worktree_removed_after_decide(
        self, tmp_path, fake_forge
    ):
        """#236レビュー対応: decide時点では存在したworktreeが、apply実行直前に
        削除された場合でも、apply側の再評価によりバックアップ・削除処理を安全に
        スキップし、orphan worktreeの参照や不要な操作を残さないこと。
        """
        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()
        active = _active(worktree_path=str(worktree_dir), pid=111, started_at=None)
        run_state = RunState(active_worktrees={"280": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            forge=fake_forge,
            zombie_gc=True,
            task_timeout_seconds=0,
        )

        with (
            patch(
                "orchestune.dispatch.gc.zombies.is_process_alive", return_value=False
            ),
            patch(
                "orchestune.dispatch.gc.zombies.worktree_has_uncommitted_changes",
                return_value=True,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state, {}, config, None, now=2_000.0
            )
        assert len(reclaims) == 1

        # decideからapply実行までの間にworktreeが削除されたことを模する。
        worktree_dir.rmdir()

        with (
            patch("orchestune.dispatch.gc.zombies.backup_wip_commit") as mock_backup,
            patch(
                "orchestune.dispatch.gc.zombies.remove_worktree"
            ) as mock_remove_worktree,
        ):
            event = _apply_zombie_or_timeout_reclaim(run_state, reclaims[0], config)

        mock_backup.assert_not_called()
        mock_remove_worktree.assert_not_called()
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        assert (
            "物理worktreeが見つからなかったため"
            in fake_forge.add_comment.call_args.args[1]
        )
        assert run_state.active_worktrees == {}
        assert event is not None
