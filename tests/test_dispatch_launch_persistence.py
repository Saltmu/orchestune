import tempfile
from pathlib import Path

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_rules import CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import CompletedWorktree, RunState
from orchestune.models import PrRecord

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


class TestApplyTaskLaunchesPersistsLaunchHistoryToParentIssue:
    """#514: run_state.json消失時に`max_launches_per_window`を復元できるよう、
    起動タイムスタンプを親Issue本文へも永続化する。

    `--parent-issue`未指定（フラットモード）は対象外（Issue #514のスコープ決定）。
    """

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

    def _run(self, tmp_path, run_state, now, *, parent_issue_number, forge):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=parent_issue_number,
            forge=forge,
        )
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(plans, run_state, now, config)

    def test_writes_the_launch_timestamp_into_the_parent_issue_body(self, tmp_path):
        from unittest.mock import MagicMock

        from orchestune.issue_parsing import launch_history_from_body

        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(body="EPIC body")
        now = 5_000_000.0

        self._run(
            tmp_path,
            RunState(active_worktrees={}),
            now,
            parent_issue_number=100,
            forge=forge,
        )

        forge.update_issue_body.assert_called_once()
        issue_number, written_body = forge.update_issue_body.call_args.args
        assert issue_number == 100
        assert launch_history_from_body(written_body) == [now]
        assert "EPIC body" in written_body

    def test_does_not_touch_the_parent_issue_in_flat_mode(self, tmp_path):
        from unittest.mock import MagicMock

        forge = MagicMock()

        self._run(
            tmp_path,
            RunState(active_worktrees={}),
            5_000_000.0,
            parent_issue_number=None,
            forge=forge,
        )

        forge.get_issue.assert_not_called()
        forge.update_issue_body.assert_not_called()

    def test_persists_only_in_window_timestamps(self, tmp_path):
        """ウィンドウ外の古い起動は書き込まない（本文の単調肥大化を防ぐ）。"""
        from unittest.mock import MagicMock

        from orchestune.issue_parsing import launch_history_from_body

        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(body="EPIC body")
        now = 5_000_000.0
        stale = now - 90_000.0  # 既定24時間ウィンドウの外

        self._run(
            tmp_path,
            RunState(active_worktrees={}, launch_history=[stale]),
            now,
            parent_issue_number=100,
            forge=forge,
        )

        _, written_body = forge.update_issue_body.call_args.args
        assert launch_history_from_body(written_body) == [now]


class TestApplyTaskLaunchesLaunchHistoryCrashSafety:
    """#519レビュー指摘(P1/P2): 親Issueへのリモート書き込みが、既存の
    クラッシュ安全順序（ローカル確定 → GitHub反映）を迂回してはならない。
    また親ごとのストアへ、他の親の起動履歴を混ぜてはならない。
    """

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

    def _run(self, tmp_path, run_state, now, forge, *, run_state_path=None):
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=run_state_path or (tmp_path / "run_state.json"),
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=100,
            forge=forge,
        )
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(plans, run_state, now, config)

    def test_reserves_the_slot_before_launching(self, tmp_path):
        """#519レビュー2巡目(P1): レート制限の永続化は「使う前に予約する」
        順序でなければならない。起動後に書いて失敗を握り潰すと、ステートレス
        ランナーでは「エージェントは起動済みだが親Issueにも消えたrun_stateにも
        記録が無い」状態になり、次サイクルが同じ窓の中でもう1件起動できる——
        この永続化がdurableにしようとしている当の上限が破れる。
        """
        from unittest.mock import MagicMock, patch

        import orchestune.dispatch_worktree as dw
        from orchestune.dispatch_launch import _apply_task_launches

        calls: list[str] = []
        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(body="EPIC body")
        forge.update_issue_body.side_effect = lambda *a, **k: calls.append("persist")

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=100,
            forge=forge,
        )
        real_launch = dw.create_worktree_and_launch

        def _record_launch(*args, **kwargs):
            calls.append("launch")
            return real_launch(*args, **kwargs)

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch",
                side_effect=_record_launch,
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            _apply_task_launches(
                plans, RunState(active_worktrees={}), 5_000_000.0, config
            )

        assert calls[:2] == ["persist", "launch"]

    def test_fails_closed_and_does_not_launch_when_the_reservation_fails(
        self, tmp_path
    ):
        """予約に失敗したら起動しない（fail-closed）。記録できない起動を
        作らないので、上限が緩む方向へは壊れない。タスクはqueuedのまま
        次サイクルで再試行される。"""
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches

        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(body="EPIC body")
        forge.update_issue_body.side_effect = RuntimeError("transient GitHub error")

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=100,
            forge=forge,
        )
        run_state = RunState(active_worktrees={})

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_popen.return_value.pid = 1234
            selected = _apply_task_launches(plans, run_state, 5_000_000.0, config)

        mock_launch.assert_not_called()
        assert selected == []
        assert run_state.active_worktrees == {}
        assert run_state.launch_history == []
        assert not any(
            call.args[1] == "status:in-progress"
            for call in forge.add_label.call_args_list
        )

    def test_releases_the_reservation_when_launch_fails_preflight(self, tmp_path):
        """#519レビュー4巡目(P2): 予約が守っているのは**エージェントのクオータ**。
        起動前の決定論的な失敗（不正なブランチ名・worktree作成失敗など）は
        クオータを消費していないので、予約を解放しなければならない。
        さもないと既定(max_launches_per_window=1)では、1件の失敗が同じ親配下の
        全タスクを1時間ブロックしてしまう。
        """
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches
        from orchestune.dispatch_worktree import LaunchResult
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])

        def _capture(_n, body):
            bodies["current"] = body

        forge.update_issue_body.side_effect = _capture

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=100,
            forge=forge,
        )

        with patch(
            "orchestune.dispatch_launch.create_worktree_and_launch",
            return_value=LaunchResult(
                issue_number=1,
                branch="claude/issue-1-task-1",
                worktree_path=str(tmp_path / "worktrees" / "w1"),
                pid=None,
                launched=False,
                error_message="worktree creation failed",
            ),
        ):
            selected = _apply_task_launches(
                plans, RunState(active_worktrees={}), now, config
            )

        assert selected == []
        assert launch_history_from_body(bodies["current"]) == []

    def test_releases_the_reservation_before_reporting_to_the_forge(self, tmp_path):
        """#519レビュー7巡目(P2): 解放をGitHubへの報告より**後**に置くと、
        `transition_status_label`/`add_comment`が一時的なforgeエラーで送出した
        場合に解放へ到達せず、起動していないエージェントの予約が漏れる。
        既定(max_launches_per_window=1)では、その1件が同じ親配下の全タスクを
        1ウィンドウぶんブロックしてしまう。
        """
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches
        from orchestune.dispatch_worktree import LaunchResult
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = lambda _n, body: bodies.update(
            current=body
        )
        # 起動失敗の報告そのものが失敗する（forge側の一時障害）
        forge.add_label.side_effect = RuntimeError("forge is unavailable")

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=100,
            forge=forge,
        )

        with patch(
            "orchestune.dispatch_launch.create_worktree_and_launch",
            return_value=LaunchResult(
                issue_number=1,
                branch="claude/issue-1-task-1",
                worktree_path=str(tmp_path / "worktrees" / "w1"),
                pid=None,
                launched=False,
                error_message="worktree creation failed",
            ),
        ):
            with pytest.raises(RuntimeError):
                _apply_task_launches(plans, RunState(active_worktrees={}), now, config)

        # 報告が失敗してもクオータは解放済み（次サイクルで再試行できる）
        assert launch_history_from_body(bodies["current"]) == []

    def test_releases_the_reservation_when_launch_raises_unexpected_exception(
        self, tmp_path
    ):
        """#568: create_worktree_and_launch 自体が予期せぬ例外（OSErrorやクラッシュ等）
        を送出した場合でも、try/finally (コンテキストマネージャ) により確実に予約が解放される。
        """
        from unittest.mock import MagicMock, patch

        from orchestune.dispatch_launch import _apply_task_launches
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = lambda _n, body: bodies.update(
            current=body
        )

        plans, dispatch_target = self._launch_plan(tmp_path)
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=dispatch_target,
            parent_issue_number=100,
            forge=forge,
        )

        with patch(
            "orchestune.dispatch_launch.create_worktree_and_launch",
            side_effect=RuntimeError("unexpected crash during worktree creation"),
        ):
            with pytest.raises(
                RuntimeError, match="unexpected crash during worktree creation"
            ):
                _apply_task_launches(plans, RunState(active_worktrees={}), now, config)

        # 予期せぬ例外で脱出しても、finally でクオータは解放される
        assert launch_history_from_body(bodies["current"]) == []

    def test_keeps_the_reservation_when_launch_succeeds(self, tmp_path):
        """成功時は当然、予約を残す（解放処理が誤って消さないこと）。"""
        from unittest.mock import MagicMock

        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = lambda _n, body: bodies.update(
            current=body
        )

        self._run(tmp_path, RunState(active_worktrees={}), now, forge)

        assert launch_history_from_body(bodies["current"]) == [now]

    def test_does_not_copy_other_parents_launches_into_this_parent(self, tmp_path):
        """#519レビュー指摘(P2): run_state.launch_historyは複数の親をまたいで
        共有される。親Bのサイクルで、親Aの起動タイムスタンプを親Bの本文へ
        書き込んではならない（永続化される上限は親ごとと文書化しているため）。"""
        from unittest.mock import MagicMock

        from orchestune.issue_parsing import launch_history_from_body

        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(body="EPIC body")
        now = 5_000_000.0
        other_parents_launch = now - 60.0

        self._run(
            tmp_path,
            RunState(active_worktrees={}, launch_history=[other_parents_launch]),
            now,
            forge,
        )

        _, written_body = forge.update_issue_body.call_args.args
        assert launch_history_from_body(written_body) == [now]

    def test_appends_to_the_parents_own_persisted_history(self, tmp_path):
        """親自身の既存履歴には追記する（多重集合として重複も保つ）。"""
        from unittest.mock import MagicMock

        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        prior = now - 30.0
        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(
            body=(
                "EPIC\n\n<!-- orchestune:launch-history -->\n"
                f"```yaml\nlaunch_history:\n- {prior}\n```\n"
            )
        )

        self._run(tmp_path, RunState(active_worktrees={}), now, forge)

        _, written_body = forge.update_issue_body.call_args.args
        assert launch_history_from_body(written_body) == [prior, now]


class TestLaunchReservationContextManager:
    """#568: _launch_reservation コンテキストマネージャ単体の構造的保証を検証する。"""

    def test_launch_reservation_preserves_slot_when_committed(self, tmp_path):
        from unittest.mock import MagicMock

        from orchestune.dispatch_launch import _launch_reservation
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = lambda _n, body: bodies.update(
            current=body
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            parent_issue_number=100,
            forge=forge,
        )

        with _launch_reservation(now, config) as commit:
            assert launch_history_from_body(bodies["current"]) == [now]
            commit()

        assert launch_history_from_body(bodies["current"]) == [now]

    def test_launch_reservation_releases_slot_when_not_committed(self, tmp_path):
        from unittest.mock import MagicMock

        from orchestune.dispatch_launch import _launch_reservation
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = lambda _n, body: bodies.update(
            current=body
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            parent_issue_number=100,
            forge=forge,
        )

        with _launch_reservation(now, config):
            assert launch_history_from_body(bodies["current"]) == [now]
            # commit is not called

        assert launch_history_from_body(bodies["current"]) == []

    def test_launch_reservation_releases_slot_on_exception(self, tmp_path):
        from unittest.mock import MagicMock

        from orchestune.dispatch_launch import _launch_reservation
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.side_effect = lambda _n: MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = lambda _n, body: bodies.update(
            current=body
        )
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            parent_issue_number=100,
            forge=forge,
        )

        with pytest.raises(ValueError, match="simulated failure"):
            with _launch_reservation(now, config):
                assert launch_history_from_body(bodies["current"]) == [now]
                raise ValueError("simulated failure")

        assert launch_history_from_body(bodies["current"]) == []

    def test_launch_reservation_yields_none_when_persist_fails(self, tmp_path):
        from unittest.mock import MagicMock

        from orchestune.dispatch_launch import _launch_reservation
        from orchestune.issue_parsing import launch_history_from_body

        now = 5_000_000.0
        bodies = {"current": "EPIC body"}
        forge = MagicMock()
        forge.get_issue.return_value = MagicMock(body=bodies["current"])
        forge.update_issue_body.side_effect = RuntimeError("transient GitHub error")
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            dispatch_target=MagicMock(),
            parent_issue_number=100,
            forge=forge,
        )

        with _launch_reservation(now, config, issue_number=1) as commit:
            assert commit is None

        assert launch_history_from_body(bodies["current"]) == []
