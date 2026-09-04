"""dispatch_cycle内のアクティブworktree処理（_process_active_worktrees）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため分割している（#343）。
各ruleの中身(decide/act)は対応するactモジュール（dispatch_gc/
dispatch_escalation/dispatch_rebase）にあるため、patch対象はそれらを指す。
"""

import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle import run_dispatch_cycle
from orchestune.dispatch.cycle_context import _group_by_status
from orchestune.dispatch.locks import ExternalLockScanResult
from orchestune.dispatch.phase_reconciliation import _process_active_worktrees
from orchestune.dispatch.rules import CycleContext
from orchestune.dispatch.scoring import Task
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
)
from orchestune.models import PrRecord


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
        config=DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
        ),
    )
    defaults.update(overrides)
    return CycleContext(**defaults)


tmp_path = Path(tempfile.mkdtemp(prefix="orchestune-test-state-"))


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
                "orchestune.dispatch.gc._is_worktree_complete",
                return_value=False,
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch.rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
                return_value=["b.py"],
            ),
            patch(
                "orchestune.dispatch.rebase._handle_footprint_deviation",
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

    def test_dirty_worktree_skips_completion_and_does_not_fall_through(
        self, fake_forge
    ):
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

        fake_forge.list_prs.reset_mock(side_effect=True)
        fake_forge.list_prs.return_value = []
        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                return_value=True,
            ),
            # Completion now also consults the all-state PR list to rule out
            # an abandoned (closed-unmerged) PR before finalizing.
            patch(
                "orchestune.dispatch.gc._finalize_completed_worktree",
                return_value={"action": "completion_skipped_dirty_worktree"},
            ),
            patch(
                "orchestune.dispatch.rebase._try_auto_rebase",
                side_effect=AssertionError("Should not call auto rebase"),
            ),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
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

    def test_dirty_worktree_hold_survives_same_cycle_zombie_gc(
        self, tmp_path, fake_forge
    ):
        """#212: 完了判定の保留を同一サイクルのゾンビGCが上書きしない。"""
        worktree_path = tmp_path / "w1"
        worktree_path.mkdir()
        active = _active(worktree_path=str(worktree_path), pid=None)
        task = _task()
        run_state = RunState(active_worktrees={"1": active})
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
            zombie_gc=True,
        )
        ctx = _ctx(
            run_state=run_state,
            tasks_by_issue={1: task},
            config=config,
        )

        fake_forge.list_prs.reset_mock(side_effect=True)
        fake_forge.list_prs.return_value = []
        with (
            patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
            patch(
                "orchestune.dispatch.cycle._fetch_issues",
                return_value=_group_by_status([]),
            ),
            patch("orchestune.dispatch.cycle._build_cycle_context", return_value=ctx),
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True),
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            # Completion now also consults the all-state PR list to rule out
            # an abandoned (closed-unmerged) PR before finalizing.
            patch(
                "orchestune.dispatch.cycle._run_status_repair_boundary",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.phase_reconciliation._handle_blocked_recompute_recovery",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.cycle._sync_external_locks",
                return_value=ExternalLockScanResult(to_lock=[], to_unlock=[]),
            ),
            patch(
                "orchestune.dispatch.phase_scheduling._determine_candidate_tasks",
                return_value=([], {}, []),
            ),
            patch(
                "orchestune.dispatch.phase_scheduling._finalize_launch", return_value=[]
            ),
        ):
            report = run_dispatch_cycle(config)

        assert [event["action"] for event in report.completion_events] == [
            "completion_skipped_dirty_worktree"
        ]
        assert run_state.active_worktrees == {"1": active}

    def _run_dead_local_completion_cycle(
        self,
        tmp_path,
        fake_forge,
        *,
        active_overrides=None,
        config_overrides=None,
        create_worktree=True,
    ):
        """完了検知後の同一サイクルGCまでを、実際のフェーズ順で実行する。"""
        worktree_path = tmp_path / "w1"
        if create_worktree:
            worktree_path.mkdir()
        active_values = {"worktree_path": str(worktree_path), "pid": 123}
        active_values.update(active_overrides or {})
        active = _active(**active_values)
        task = _task()
        run_state = RunState(active_worktrees={"1": active})
        config_values = dict(
            events_log_path=tmp_path / "events.jsonl",
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            apply=False,
            zombie_gc=True,
        )
        config_values.update(config_overrides or {})
        config = DispatcherConfig(**config_values)
        ctx = _ctx(run_state=run_state, tasks_by_issue={1: task}, config=config)

        with (
            patch("orchestune.dispatch.cycle.load_run_state", return_value=run_state),
            patch(
                "orchestune.dispatch.cycle._fetch_issues",
                return_value=_group_by_status([]),
            ),
            patch("orchestune.dispatch.cycle._build_cycle_context", return_value=ctx),
            patch("orchestune.dispatch.gc._is_worktree_complete", return_value=True),
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.gc.completion.worktree_has_new_commits",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch.cycle._run_status_repair_boundary",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.phase_reconciliation._handle_blocked_recompute_recovery",
                return_value=[],
            ),
            patch(
                "orchestune.dispatch.cycle._sync_external_locks",
                return_value=ExternalLockScanResult(to_lock=[], to_unlock=[]),
            ),
            patch(
                "orchestune.dispatch.phase_scheduling._determine_candidate_tasks",
                return_value=([], {}, []),
            ),
            patch(
                "orchestune.dispatch.phase_scheduling._finalize_launch", return_value=[]
            ),
        ):
            report = run_dispatch_cycle(config)
        return report, run_state, active

    def test_forge_outcome_error_holds_dead_process_from_same_cycle_gc(
        self, tmp_path, fake_forge
    ):
        """Forgeの一時障害で完了確定できないworktreeを同一サイクルで回収しない。"""
        fake_forge.list_prs.return_value = [
            PrRecord(
                number=10,
                head_ref="claude/issue-1-task-a",
                changed_files=("src/foo.py",),
                closes_issue_numbers=(1,),
                state="MERGED",
            )
        ]
        fake_forge.list_comments.side_effect = RuntimeError("temporary forge error")

        report, run_state, active = self._run_dead_local_completion_cycle(
            tmp_path, fake_forge
        )

        assert [event["action"] for event in report.completion_events] == [
            "completion_skipped_forge_error"
        ]
        assert run_state.active_worktrees == {"1": active}

    def test_unknown_local_pr_status_holds_dead_process_from_same_cycle_gc(
        self, tmp_path, fake_forge
    ):
        """PR状態を取得できない場合もfail-closedで次サイクルまで保留する。"""
        fake_forge.list_prs.side_effect = RuntimeError("temporary forge error")

        report, run_state, active = self._run_dead_local_completion_cycle(
            tmp_path, fake_forge
        )

        assert [event["action"] for event in report.completion_events] == [
            "completion_skipped_forge_error"
        ]
        assert run_state.active_worktrees == {"1": active}

    def test_unknown_cloud_status_holds_timed_out_session_from_same_cycle_gc(
        self, tmp_path, fake_forge
    ):
        """クラウド状態照会の一時障害もタイムアウトGCより優先して保留する。"""
        dispatch_target = MagicMock()
        dispatch_target.completion_status.return_value = "unknown"

        report, run_state, active = self._run_dead_local_completion_cycle(
            tmp_path,
            fake_forge,
            active_overrides={"pid": None, "external_id": "session-1"},
            config_overrides={
                "dispatch_target": dispatch_target,
                "task_timeout_seconds": 1,
            },
        )

        assert [event["action"] for event in report.completion_events] == [
            "completion_skipped_forge_error"
        ]
        assert run_state.active_worktrees == {"1": active}

    def test_unknown_recovery_pr_status_holds_orphan_from_same_cycle_gc(
        self, tmp_path, fake_forge
    ):
        """自己修復エントリのPR照会失敗を、PR不在と誤認して回収しない。"""
        fake_forge.list_prs.side_effect = RuntimeError("temporary forge error")

        report, run_state, active = self._run_dead_local_completion_cycle(
            tmp_path,
            fake_forge,
            active_overrides={"pid": None, "started_at": None},
            create_worktree=False,
        )

        assert [event["action"] for event in report.completion_events] == [
            "completion_skipped_forge_error"
        ]
        assert run_state.active_worktrees == {"1": active}

    def test_not_needed_label_takes_precedence_over_stale_entry(self, tmp_path):
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
                events_log_path=tmp_path / "events.jsonl",
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch.gc._finalize_not_needed_worktree",
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
        assert "1" not in ctx.run_state.active_worktrees

    def test_auto_rebase_failure_discards_active_entry(self, tmp_path, fake_forge):
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
                events_log_path=tmp_path / "events.jsonl",
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        def mock_subprocess_run(args, **kwargs):
            if "rebase" in args:
                raise subprocess.CalledProcessError(1, args)
            # #213: rebase前のWIP退避チェック(`git status --porcelain`)がcleanと
            # 判定されるよう、空のstdoutを返す。
            return subprocess.CompletedProcess(args, 0, stdout="")

        fake_forge.remove_label.reset_mock(side_effect=True)
        mock_remove = fake_forge.remove_label
        fake_forge.add_label.reset_mock(side_effect=True)
        mock_add = fake_forge.add_label
        fake_forge.add_comment.reset_mock(side_effect=True)
        mock_comment = fake_forge.add_comment
        with (
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                return_value=False,
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch.rebase._decide_rebase_needed",
                return_value=True,
            ),
            patch(
                "orchestune.infra.git_cli.subprocess.run",
                side_effect=mock_subprocess_run,
            ),
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

    def test_forced_serial_persists_with_early_termination_rules(self, tmp_path):
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
                events_log_path=tmp_path / "events.jsonl",
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch.gc._finalize_not_needed_worktree",
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
                events_log_path=tmp_path / "events.jsonl",
                run_state_path=Path("dummy.json"),
                worktree_root=Path("worktrees"),
                apply=True,
            ),
        )

        with (
            patch(
                "orchestune.dispatch.gc._finalize_not_needed_worktree",
                return_value={"action": "not_needed"},
            ),
            patch(
                "orchestune.dispatch.gc._is_worktree_complete",
                return_value=False,
            ),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch.rebase._decide_rebase_needed",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch.rebase.check_footprint_deviation",
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
