"""dispatch_gc内のZombie・Timeout回収（collect/decide層）テスト。

`tests/test_dispatch_gc.py`の肥大化解消のため分割している（#345）。
完了ワークツリー処理は`test_dispatch_gc_completion.py`、gitプリミティブや
`dispatch_gc.py`自身のルール・エンドツーエンド統合テストは
`test_dispatch_gc.py`に残している。apply層（`_apply_zombie_or_timeout_reclaim`の
副作用検証）は再肥大化のため`test_dispatch_gc_zombies_apply.py`へ分割している（#829）。
"""

from unittest.mock import patch

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.state import RunState
from tests.dispatch_gc_test_support import _active, _task
from tests.dispatch_gc_test_support import (
    decide_gc_reclaims as _decide_zombie_or_timeout_reclaims,
)
from tests.dispatch_gc_test_support import (
    run_gc_reclaims as _collect_zombies_and_timeouts,
)


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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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
            parent_issue_number=1,
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


class TestInteractiveOwnershipGcExclusion:
    """#940: owner_kind=interactive をPID死亡・early-death・timeout強制回収から除外する。"""

    def test_interactive_active_is_excluded_from_gc_reclaims(
        self, tmp_path, fake_forge
    ):
        """owner_kind=interactive の active はプロセス死亡やタイムアウトでも回収候補から除外され、
        run_gc_reclaims で回収・削除されず除外診断イベントが記録される。"""
        active_interactive = _active(
            started_at=1_000.0,
            worktree_path=str(tmp_path / "interactive-worktree"),
            pid=111,
            owner_kind="interactive",
            claim_id="claim-interactive-1",
        )
        run_state = RunState(active_worktrees={"280": active_interactive})
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=60,
            forge=fake_forge,
        )

        with (
            patch("orchestune.dispatch.phase_gc.time.time", return_value=2_000.0),
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                autospec=True,
                return_value=False,
            ),
        ):
            # decide層: 回収候補から除外され、空リストになる
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {active_interactive.issue_number: task},
                config,
                None,
                now=2_000.0,
            )
            assert reclaims == []

            # collect/apply層: 回収実行されず、active_worktreesに残り、除外診断イベントが記録される
            events = _collect_zombies_and_timeouts(
                run_state, {active_interactive.issue_number: task}, config
            )

        assert "280" in run_state.active_worktrees
        assert run_state.active_worktrees["280"].owner_kind == "interactive"
        fake_forge.remove_label.assert_not_called()
        fake_forge.add_label.assert_not_called()
        assert len(events) == 1
        assert events[0]["action"] == "gc_reclaim_excluded_interactive"
        assert events[0]["issue_number"] == 280

    def test_dispatch_active_is_reclaimed_as_usual(self, tmp_path, fake_forge):
        """owner_kind=dispatch の active は従来どおりプロセス死亡で回収される（対で検証）。"""
        active_dispatch = _active(
            started_at=1_000.0,
            worktree_path=str(tmp_path / "dispatch-worktree"),
            pid=111,
            owner_kind="dispatch",
        )
        run_state = RunState(active_worktrees={"280": active_dispatch})
        task = _task(status_labels=("status:in-progress",))
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=60,
            forge=fake_forge,
        )

        with (
            patch("orchestune.dispatch.phase_gc.time.time", return_value=2_000.0),
            patch(
                "orchestune.dispatch.execution_repair.is_process_alive",
                autospec=True,
                return_value=False,
            ),
        ):
            reclaims = _decide_zombie_or_timeout_reclaims(
                run_state,
                {active_dispatch.issue_number: task},
                config,
                None,
                now=2_000.0,
            )
            assert len(reclaims) == 1
            assert reclaims[0].active.owner_kind == "dispatch"

            events = _collect_zombies_and_timeouts(
                run_state, {active_dispatch.issue_number: task}, config
            )

        assert run_state.active_worktrees == {}
        fake_forge.remove_label.assert_called_once_with(280, "status:in-progress")
        fake_forge.add_label.assert_called_once_with(280, "status:queued")
        assert len(events) == 1
        assert events[0]["action"] == "gc_reclaimed"

    def test_list_unattended_interactive_claims_helper(self):
        """list_unattended_interactive_claims は active 内の owner_kind=interactive をレポート向けに列挙する。"""
        from orchestune.dispatch.gc.zombies import list_unattended_interactive_claims

        active_interactive = _active(
            issue_number=280,
            owner_kind="interactive",
            claim_id="claim-123",
            reservation_kind="repository",
        )
        active_dispatch = _active(
            issue_number=281,
            owner_kind="dispatch",
        )
        run_state = RunState(
            active_worktrees={"280": active_interactive, "281": active_dispatch}
        )
        task = _task(subtask_id="interactive-task")

        unattended = list_unattended_interactive_claims(
            run_state, tasks_by_issue={280: task}
        )
        assert len(unattended) == 1
        item = unattended[0]
        assert item["issue_number"] == 280
        assert item["subtask_id"] == "interactive-task"
        assert item["claim_id"] == "claim-123"
        assert item["reservation_kind"] == "repository"
        assert "interactive" in item["reason"]

    def test_interactive_active_is_excluded_from_completion_and_retries(self, tmp_path):
        """owner_kind=interactive は _is_worktree_complete で未完了扱いとなり、
        early-death および review-timeout 再投入からも除外される。"""
        from orchestune.dispatch.gc.completion import (
            _apply_early_death_retry,
            _apply_review_timeout_retry,
            _is_worktree_complete,
        )

        active = _active(
            started_at=100.0,
            pid=111,
            owner_kind="interactive",
            claim_id="claim-test-1",
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            early_death_window_seconds=120,
            max_early_death_retries=2,
            max_review_timeout_retries=2,
        )
        run_state = RunState(active_worktrees={"280": active})

        with patch(
            "orchestune.dispatch.gc.completion.is_process_alive",
            autospec=True,
            return_value=False,
        ):
            # プロセス死亡でも _is_worktree_complete は False（作業中・生存扱い）
            assert _is_worktree_complete(active, config) is False

        # early-death 再投入からも除外される
        early_death = _apply_early_death_retry(
            active, _task(), config, run_state, now=110.0
        )
        assert early_death is None

        # review-timeout 再投入からも除外される
        review_timeout = _apply_review_timeout_retry(
            active, _task(), config, run_state, now=110.0
        )
        assert review_timeout is None

    def test_interactive_claim_process_exit_survives_across_cycles(
        self, tmp_path, fake_forge
    ):
        """短命なclaimプロセスが終了した直後の active（PID死亡）が、
        GCサイクルを複数回実行しても生存扱いとなり消えずに残る。"""
        from orchestune.dispatch.phase_gc import run_gc_phase

        worktree_path = tmp_path / "interactive-wt"
        worktree_path.mkdir(parents=True)
        active = _active(
            issue_number=280,
            started_at=1_000.0,
            worktree_path=str(worktree_path),
            pid=99999,  # 終了した短命claimプロセスのPID
            owner_kind="interactive",
            claim_id="claim-h2-test",
        )
        run_state = RunState(active_worktrees={"280": active})
        task = _task(
            issue_number=280,
            status_labels=("status:in-progress",),
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=str(tmp_path / "worktrees"),
            apply=True,
            zombie_gc=True,
            task_timeout_seconds=60,
            forge=fake_forge,
        )

        with patch(
            "orchestune.dispatch.execution_repair.is_process_alive",
            autospec=True,
            return_value=False,
        ):
            # 1サイクル目
            res1 = run_gc_phase(run_state, {280: task}, config, [], now=1_050.0)
            assert "280" in run_state.active_worktrees
            assert worktree_path.exists()
            assert any(
                e.get("action") == "gc_reclaim_excluded_interactive"
                for e in res1.completion_events
            )

            # 2サイクル目（さらに時間が経過しても消えず、worktreeも残る）
            res2 = run_gc_phase(run_state, {280: task}, config, [], now=1_200.0)
            assert "280" in run_state.active_worktrees
            assert worktree_path.exists()
            assert any(
                e.get("action") == "gc_reclaim_excluded_interactive"
                for e in res2.completion_events
            )

    def test_resolve_completion_guards_interactive_claims_before_routing(
        self, tmp_path, fake_forge
    ):
        """owner_kind=interactive は started_at=None や external_id の有無に関わらず、
        completion のあらゆるルーティング（クラウド判定・PR復元判定・ローカル判定）から除外され pending となる。
        """
        from orchestune.dispatch.gc import _resolve_completion
        from orchestune.dispatch.rules import _RuleExecutionContext
        from orchestune.models import PrRecord

        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=str(tmp_path / "worktrees"),
            forge=fake_forge,
        )
        from unittest.mock import MagicMock

        run_state = RunState(active_worktrees={})
        ctx = _RuleExecutionContext(
            run_state=run_state,
            queries=MagicMock(),
            config=config,
            prs=(),
        )

        # ケース1: external_id が付与されている（例: recovered PR）場合でも cloud completion に進まず pending
        active_with_external = _active(
            issue_number=301,
            started_at=None,
            external_id="recovered-pr:999",
            owner_kind="interactive",
            claim_id="claim-guard-1",
        )
        res1 = _resolve_completion(ctx, "301", active_with_external, None)
        assert res1.state == "pending"

        # ケース2: started_at=None, external_id=None で CLOSED PR が存在する場合でも
        # _resolve_recovered_completion に進んで abandoned 終了せず pending
        closed_pr = PrRecord(
            number=888,
            title="Old PR [skip ci]",
            body="fixes #302",
            state="CLOSED",
            head_ref="claude/issue-302-task",
            base_ref="main",
            changed_files=(),
        )
        fake_forge._prs = [closed_pr]
        active_recovered_pr = _active(
            issue_number=302,
            started_at=None,
            external_id=None,
            owner_kind="interactive",
            claim_id="claim-guard-2",
        )
        res2 = _resolve_completion(ctx, "302", active_recovered_pr, None)
        assert res2.state == "pending"
