import argparse
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from orchestune.dag import FootprintConflict
from orchestune.dispatch_result import PhaseResult, PhaseStatus
from orchestune.dispatch_targets import CodexCloudDispatchTarget
from orchestune.dispatcher import (
    ActiveWorktree,
    ClaudeCodeCloudRoutineDispatchTarget,
    CompletedWorktree,
    CycleReport,
    DispatcherConfig,
    LocalProcessDispatchTarget,
    RunState,
    Task,
    _decide_semantic_review_enabled,
    _is_worktree_complete,
    _poll_pending_not_needed_reviews,
    _process_parent_completion,
    _run_semantic_integrator,
    append_event_log,
    build_event_log_entry,
    load_run_state,
    main,
    run_dispatch_cycle,
    save_run_state,
)
from orchestune.forge import ForgeAuthError
from orchestune.github import IssueRecord, PrRecord


@pytest.fixture(autouse=True)
def _stub_forge_check_auth_by_default():
    """テスト環境において GitHubForge.check_auth() が実際の gh 認証エラーを
    投げないように、デフォルトで pass するようにスタブする。"""
    with patch("orchestune.forge.GitHubForge.check_auth") as mock_check:
        yield mock_check


@pytest.fixture(autouse=True)
def _stub_label_actor_permission_by_default():
    """#119で追加したactor権限検証ステップが、既存の大半のテストで実際の
    `gh api`呼び出しを行わないよう、デフォルトで許可された actor/permission を
    返すようスタブする。検証ロジック自体のテストは
    tests/test_dispatch_actor_verification.py に集約する。"""
    with (
        patch(
            "orchestune.dispatcher.github.get_label_actor",
            return_value="trusted-actor",
        ),
        patch(
            "orchestune.dispatcher.github.get_actor_permission",
            return_value="write",
        ),
    ):
        yield


def _issue(
    number,
    labels=("status:queued",),
    footprint=("src/foo.py",),
    symbols=("foo.Foo",),
    subtask_id="task-a",
    depends_on=(),
    created_at="2026-01-01T00:00:00+00:00",
    parent_number=181,
):
    footprint_lines = "\n".join(f"  - {f}" for f in footprint) if footprint else "  []"
    symbols_lines = "\n".join(f"  - {s}" for s in symbols) if symbols else "  []"
    depends_on_lines = (
        "\n".join(f"  - {d}" for d in depends_on) if depends_on else "  []"
    )
    body = (
        "## Footprint\n"
        "```yaml\n"
        f"subtask_id: {subtask_id}\n"
        "footprint:\n"
        f"{footprint_lines}\n"
        "symbols:\n"
        f"{symbols_lines}\n"
        "depends_on:\n"
        f"{depends_on_lines}\n"
        "```\n"
    )
    parent = {"number": parent_number} if parent_number is not None else None
    return IssueRecord(
        number=number,
        title="t",
        body=body,
        labels=labels,
        created_at=created_at,
        parent=parent,
    )


def _task(
    issue_number,
    priority="medium",
    risk=False,
    progress_partial=False,
    created_at="2023-01-01T00:00:00+00:00",
    footprint=("src/foo.py",),
    depends_on=(),
):
    return Task(
        issue_number=issue_number,
        subtask_id=f"task-{issue_number}",
        footprint=footprint,
        symbols=(),
        risk=risk,
        priority=priority,
        progress_partial=progress_partial,
        status_labels=("status:queued",),
        created_at=created_at,
        depends_on=depends_on,
    )


class TestAppendEventLog:
    def test_build_event_log_entry_includes_cycle_events(self):
        report = CycleReport(
            selected=[_task(1)],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[{"issue_number": 1, "action": "recomputed"}],
            completion_events=[{"issue_number": 2, "action": "completed"}],
            promotion_events=[{"issue_number": 3, "subtask_id": "task-c"}],
            applied=True,
        )
        entry = build_event_log_entry(report, now=1700000000.0)
        assert entry["timestamp"] == 1700000000.0
        assert entry["quota_slots_available"] == 0
        assert entry["selected"] == [{"issue_number": 1, "subtask_id": "task-1"}]
        assert entry["deviation_events"] == report.deviation_events
        assert entry["completion_events"] == report.completion_events
        assert entry["promotion_events"] == report.promotion_events

    def test_append_event_log_writes_jsonl(self, tmp_path):
        path = tmp_path / "events.jsonl"
        append_event_log({"timestamp": 1.0, "foo": "bar"}, path)
        append_event_log({"timestamp": 2.0, "foo": "baz"}, path)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"timestamp": 1.0, "foo": "bar"}
        assert json.loads(lines[1]) == {"timestamp": 2.0, "foo": "baz"}

    def test_append_event_log_creates_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "events.jsonl"
        append_event_log({"timestamp": 1.0}, path)
        assert path.exists()


class TestRunDispatchCycle:
    def test_dry_run_makes_no_write_calls(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _issue(1)
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_subproc_run.assert_not_called()
        mock_popen.assert_not_called()
        assert report.applied is False
        assert len(report.selected) == 1
        assert not (tmp_path / "run_state.json").exists()

    def test_apply_launches_selected_task_and_persists_state(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1)
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label"),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            mock_subproc_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 555
            report = run_dispatch_cycle(config)

        assert report.applied is True
        assert len(report.selected) == 1
        mock_add_label.assert_any_call(1, "status:in-progress")
        assert (tmp_path / "run_state.json").exists()
        persisted = json.loads((tmp_path / "run_state.json").read_text())
        assert "1" in persisted["active_worktrees"]

    def test_apply_updates_last_reconciled_at(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        before = time.time()
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label", return_value=[]),
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
        ):
            run_dispatch_cycle(config)
        after = time.time()

        loaded = load_run_state(config.run_state_path)
        assert loaded.last_reconciled_at is not None
        assert before <= loaded.last_reconciled_at <= after

    def test_dry_run_does_not_update_last_reconciled_at(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label", return_value=[]),
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
        ):
            run_dispatch_cycle(config)

        assert not config.run_state_path.exists()

    def test_quota_exhausted_selects_nothing(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        save_run_state(
            RunState(
                active_worktrees={
                    "9": ActiveWorktree(9, "b", "w", 1, 1_699_999_000.0, ()),
                    "8": ActiveWorktree(8, "b2", "w2", 2, 1_699_999_000.0, ()),
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=5,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _issue(1)
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        assert report.quota_slots_available == 0

    def test_run_dispatch_cycle_filters_by_parent_issue_number(self, tmp_path):
        """#156: parent_issue_number指定時は、github.list_sub_issuesによる
        fast pathへ正しく配線され、その結果がそのまま使われることを確認する。
        『親を問わず返された候補から正しい親だけに絞る』という判定自体は、
        list_sub_issuesの実装（github.py）側の責務のためここでは検証しない。
        """
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            parent_issue_number=100,
            apply=False,
        )
        sub_issue_1 = _issue(
            1,
            labels=("status:queued",),
            subtask_id="task-a",
            parent_number=100,
        )

        with (
            patch("orchestune.dispatcher.github.list_sub_issues") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
        ):
            mock_list.return_value = [sub_issue_1]
            report = run_dispatch_cycle(config)

        mock_list.assert_called_once_with(100)
        assert [t.issue_number for t in report.selected] == [1]

    def test_run_dispatch_cycle_resolves_depends_on_from_blocked_by(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        done_issue = _issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=(),
        )
        blocked_issue = IssueRecord(
            number=blocked_issue.number,
            title=blocked_issue.title,
            body=blocked_issue.body,
            labels=blocked_issue.labels,
            created_at=blocked_issue.created_at,
            blocked_by=(1,),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:done":
                    return [done_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        # BがAの完了により昇格したことを確認
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]


class TestRunDispatchCycleBranchNormalization:
    """#194: リモートブランチ名のorigin/プレフィックス正規化。"""

    def test_does_not_self_lock_own_active_branch(self, tmp_path):
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
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _issue(2, footprint=("src/shared.py",))
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/claude/issue-1-task-a"],
            ),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatcher.github.branch_changed_files",
                return_value=["src/shared.py"],
            ),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.lock_changes["to_lock"] == []

    def test_excludes_branch_with_open_pr_multisegment_headref(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label", return_value=[]),
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/feature/foo"],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=1, head_ref="feature/foo", changed_files=("src/x.py",)
                    )
                ],
            ),
            patch(
                "orchestune.dispatcher.github.branch_changed_files"
            ) as mock_branch_files,
        ):
            run_dispatch_cycle(config)

        mock_branch_files.assert_not_called()

    def test_unrelated_external_branch_still_locks_overlapping_task(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _issue(1, footprint=("src/shared.py",))
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/someone-elses-branch"],
            ),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch(
                "orchestune.dispatcher.github.branch_changed_files",
                return_value=["src/shared.py"],
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert [t.issue_number for t in report.lock_changes["to_lock"]] == [1]


class TestRunDispatchCycleFootprintRecompute:
    """#192: footprint逸脱検知 → DAG再計算 → notify_recompute の配線。"""

    def _config(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
            parent_issue_number=181,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def test_significant_deviation_triggers_recompute_and_notify(self, tmp_path):
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
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _issue(
            1,
            labels=("status:in-progress",),
            footprint=("src/foo.py",),
            symbols=("foo.Foo",),
            subtask_id="task-a",
        )
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.dispatcher.github.list_sub_issues") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label"),
            patch("orchestune.dispatch_targets.subprocess.Popen"),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ) as mock_check_deviation,
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
            patch(
                "orchestune.dispatch_rebase.notify_recompute", return_value=["body"]
            ) as mock_notify,
        ):
            mock_list.return_value = [in_progress_issue]
            mock_recompute.return_value = (MagicMock(), [conflict])

            report = run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_check_deviation.assert_called_once()
        mock_recompute.assert_called_once()
        assert mock_recompute.call_args.args[1] == "task-a"
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["apply"] is True
        assert len(report.deviation_events) == 1
        event = report.deviation_events[0]
        assert event["issue_number"] == 1
        assert event["action"] == "recomputed"
        assert event["deviated_files"] == ["src/unexpected.py"]

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"]["1"]["recompute_count"] == 1

    def test_dry_run_recompute_does_not_persist_or_call_github(self, tmp_path):
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
        config = self._config(tmp_path, run_state_path, apply=False)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.dispatcher.github.list_sub_issues") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
            patch(
                "orchestune.dispatch_rebase.notify_recompute", return_value=["dry body"]
            ) as mock_notify,
        ):
            mock_list.return_value = [in_progress_issue]
            mock_recompute.return_value = (MagicMock(), [conflict])

            run_dispatch_cycle(config)

        mock_add_label.assert_not_called()
        mock_add_comment.assert_not_called()
        assert mock_notify.call_args.kwargs["apply"] is False

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"]["1"]["recompute_count"] == 0

    def test_retry_limit_exceeded_triggers_forced_serialization(self, tmp_path):
        """#200: リトライ上限超過時は再計算せず強制直列化にフォールバックする。"""
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
                        recompute_count=2,
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(
            tmp_path,
            run_state_path,
            max_recompute_retries=2,
            max_concurrent=2,
        )
        in_progress_issue = _issue(
            1,
            labels=("status:in-progress",),
            subtask_id="task-a",
            footprint=("src/foo.py",),
        )
        other_queued_issue = _issue(
            2,
            labels=("status:queued",),
            subtask_id="task-b",
            footprint=("src/bar.py",),
        )

        def _launch_stub(
            selected, _task_to_base_branch, _candidate_tasks, run_state, _now, config
        ):
            save_run_state(run_state, config.run_state_path)
            return selected

        with (
            patch("orchestune.dispatcher.github.list_sub_issues") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label"),
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_cycle._launch_selected_tasks",
                side_effect=_launch_stub,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
        ):
            mock_list.return_value = [other_queued_issue, in_progress_issue]

            report = run_dispatch_cycle(config)

        mock_recompute.assert_not_called()
        mock_add_label.assert_any_call(1, "status:force-serial")
        mock_add_comment.assert_called_once()
        assert [task.issue_number for task in report.selected] == [2]
        assert report.quota_slots_available == 1
        assert report.deviation_events[0]["action"] == "forced_serial"

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"]["1"]["forced_serial"] is True

    def test_forced_serial_filters_out_only_conflicting_candidates(self, tmp_path):
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
                        forced_serial=True,
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(tmp_path, run_state_path, max_concurrent=3)
        in_progress_issue = _issue(
            1,
            labels=("status:in-progress", "status:force-serial"),
            subtask_id="task-a",
            footprint=("src/foo.py",),
        )
        conflicting_issue = _issue(
            2,
            labels=("status:queued",),
            subtask_id="task-b",
            footprint=("src/foo.py",),
        )
        dependent_issue = _issue(
            3,
            labels=("status:queued",),
            subtask_id="task-c",
            footprint=("src/baz.py",),
            depends_on=("task-a",),
        )
        independent_issue = _issue(
            4,
            labels=("status:queued",),
            subtask_id="task-d",
            footprint=("src/qux.py",),
        )

        def _launch_stub(
            selected, _task_to_base_branch, _candidate_tasks, run_state, _now, config
        ):
            save_run_state(run_state, config.run_state_path)
            return selected

        with (
            patch("orchestune.dispatcher.github.list_sub_issues") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_cycle._launch_selected_tasks",
                side_effect=_launch_stub,
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=[],
            ),
        ):
            mock_list.return_value = [
                conflicting_issue,
                dependent_issue,
                independent_issue,
                in_progress_issue,
            ]
            report = run_dispatch_cycle(config)

        assert report.quota_slots_available == 2
        assert [task.issue_number for task in report.selected] == [4]

    def test_already_forced_serial_does_not_recompute_again(self, tmp_path):
        """一度強制直列化された後は、再度の再計算・通知でチャーンさせない。"""
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
                        recompute_count=2,
                        forced_serial=True,
                    )
                },
                launch_history=[],
            ),
            run_state_path,
        )
        config = self._config(tmp_path, run_state_path, max_recompute_retries=2)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatcher.github.list_sub_issues") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation",
                return_value=["src/unexpected.py"],
            ),
            patch(
                "orchestune.dispatch_rebase.recompute_dag_for_footprint_change"
            ) as mock_recompute,
        ):
            mock_list.return_value = [in_progress_issue]
            report = run_dispatch_cycle(config)

        mock_recompute.assert_not_called()
        mock_add_comment.assert_not_called()
        mock_add_label.assert_not_called()
        assert report.selected == []
        assert report.deviation_events[0]["action"] == "already_forced_serial"


class TestIsWorktreeComplete:
    """#239: external_id経由の完了判定に、issue_numberが正しく引き渡されること。"""

    def test_passes_issue_number_to_dispatch_target_handle(self, tmp_path):
        fake_target = MagicMock()
        fake_target.completion_status.return_value = "completed"
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=fake_target,
        )
        active = ActiveWorktree(
            issue_number=218,
            branch="claude/issue-218-review-history-backend-api",
            worktree_path=str(tmp_path / "w1"),
            pid=None,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="session_1",
            external_url="https://claude.ai/code/session_1",
        )

        result = _is_worktree_complete(active, config)

        assert result is True
        handle = fake_target.completion_status.call_args.args[0]
        assert handle.issue_number == 218
        assert handle.branch_name == "claude/issue-218-review-history-backend-api"

    def test_codex_cloud_active_worktree_waits_for_pr(self, tmp_path):
        target = CodexCloudDispatchTarget("env_123")
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            dispatch_target=target,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=4242,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
            external_id="codex-cloud:claude/issue-1-task-a",
        )

        with (
            patch("orchestune.dispatch_targets.github.list_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive") as mock_is_alive,
        ):
            assert _is_worktree_complete(active, config) is False

        mock_is_alive.assert_not_called()

    def test_recovered_local_active_worktree_waits_for_pid_reconciliation(
        self, tmp_path
    ):
        config = DispatcherConfig(run_state_path=tmp_path / "run_state.json")
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "missing-worktree"),
            pid=None,
            started_at=None,
            declared_footprint=(),
        )

        assert _is_worktree_complete(active, config) is False


class TestRecoveredActiveTask:
    def test_run_cycle_keeps_recovered_local_task_in_progress(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=60,
        )
        issue = _issue(1, labels=("status:in-progress",), subtask_id="task-a")

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch(
                "orchestune.dispatch_recovery.subprocess.run",
                return_value=MagicMock(stdout=""),
            ),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:in-progress" else []
            )

            report = run_dispatch_cycle(config)

        restored = load_run_state(config.run_state_path).active_worktrees["1"]
        assert restored.started_at is None
        assert report.completion_events == []
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()

    def test_next_cycle_completes_recovered_task_when_closing_pr_appears(
        self, tmp_path
    ):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        active = ActiveWorktree(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "worktrees" / "missing-worktree"),
            pid=None,
            started_at=None,
            declared_footprint=(),
        )
        save_run_state(RunState(active_worktrees={"1": active}), config.run_state_path)
        issue = _issue(1, labels=("status:in-progress",), subtask_id="task-a")
        pr = PrRecord(
            number=101,
            head_ref="agent/issue-1-task-a",
            changed_files=("src/foo.py",),
            closes_issue_numbers=(1,),
        )

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[pr]),
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.remote_branch_commit_sha_if_ahead",
                return_value="recovered-commit",
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue] if label == "status:in-progress" else []
            )

            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed"
        assert report.completion_events[0]["commit_sha"] == "recovered-commit"
        assert load_run_state(config.run_state_path).active_worktrees == {}
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_add_label.assert_any_call(1, "status:done")


class TestRunDispatchCycleCompletion:
    """#193: プロセス終了検知→worktree削除→クオータ解放→status:doneラベル遷移。"""

    def _config(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            max_concurrent=1,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _seed_active(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
        )
        defaults.update(overrides)
        save_run_state(
            RunState(
                active_worktrees={"1": ActiveWorktree(**defaults)}, launch_history=[]
            ),
            run_state_path,
        )

    def test_completed_clean_worktree_is_removed_and_labeled_done(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_called_once_with(str(tmp_path / "w1"))
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_add_label.assert_any_call(1, "status:done")
        assert report.completion_events == [
            {
                "issue_number": 1,
                "worktree_path": str(tmp_path / "w1"),
                "action": "completed",
                "subtask_id": "task-a",
                "commit_sha": None,
            }
        ]

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"] == {}
        assert len(persisted["completed_worktrees"]) == 1
        completed = persisted["completed_worktrees"][0]
        assert completed["issue_number"] == 1
        assert completed["subtask_id"] == "task-a"
        assert completed["branch"] == "claude/issue-1-task-a"
        assert completed["started_at"] == 1_699_999_000.0
        assert completed["completed_at"] >= completed["started_at"]

        events_lines = config.events_log_path.read_text(encoding="utf-8").splitlines()
        assert len(events_lines) == 1
        logged_entry = json.loads(events_lines[0])
        assert logged_entry["completion_events"] == report.completion_events

    def test_cloud_completion_uses_remote_branch_commits(self, tmp_path):
        """#177: クラウド実行の結果は、起動時のローカルworktreeではなく
        fetch済みのリモートブランチで検証する。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path, external_id="session-1")
        dispatch_target = MagicMock()
        dispatch_target.completion_status.return_value = "completed"
        config = self._config(tmp_path, run_state_path, dispatch_target=dispatch_target)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label"),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=False,
            ) as mock_local_commits,
            patch(
                "orchestune.dispatch_gc.remote_branch_commit_sha_if_ahead",
                return_value="remote-commit",
            ) as mock_remote_commits,
            patch("orchestune.dispatch_gc.remove_worktree"),
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed"
        assert report.completion_events[0]["commit_sha"] == "remote-commit"
        mock_local_commits.assert_not_called()
        mock_remote_commits.assert_called_once_with(
            config.worktree_root.parent,
            "claude/issue-1-task-a",
            "origin/main",
        )
        mock_add_label.assert_any_call(1, "status:done")

    def test_cloud_completion_without_verified_sha_is_not_marked_done(self, tmp_path):
        """#177: SHAを取得できなければ、完了ラベルへ遷移しない。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path, external_id="session-1")
        dispatch_target = MagicMock()
        dispatch_target.completion_status.return_value = "completed"
        config = self._config(tmp_path, run_state_path, dispatch_target=dispatch_target)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label"),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.remote_branch_commit_sha_if_ahead",
                return_value=None,
            ),
            patch(
                "orchestune.dispatch_gc.apply_human_review_escalation"
            ) as mock_escalate,
            patch("orchestune.dispatch_gc.remove_worktree"),
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed_no_commits"
        mock_escalate.assert_called_once()
        assert all(
            call.args != (1, "status:done") for call in mock_add_label.call_args_list
        )

    def test_dirty_worktree_completion_is_skipped(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_not_called()
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert (
            report.completion_events[0]["action"] == "completion_skipped_dirty_worktree"
        )

        persisted = json.loads(run_state_path.read_text())
        assert "1" in persisted["active_worktrees"]

    def test_dry_run_completion_does_not_mutate_or_call_github(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path, apply=False)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue] if label == "status:in-progress" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_not_called()
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()
        assert report.completion_events[0]["action"] == "completed"
        assert not config.events_log_path.exists()

        persisted = json.loads(run_state_path.read_text())
        assert "1" in persisted["active_worktrees"]

    def test_no_commits_completion_frees_quota_without_promoting_dependents(
        self, tmp_path
    ):
        """#74: 空コミット完了はcompleted_subtask_idsに含めず依存先を昇格させないが、
        run_state側のクオータは解放する(worktree・ラベルはdispatch_gc側で片付け済みのため)。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [in_progress_issue]
                if label == "status:in-progress"
                else ([blocked_issue] if label == "status:blocked" else [])
            )
            report = run_dispatch_cycle(config)

        assert report.completion_events[0]["action"] == "completed_no_commits"
        mock_remove_worktree.assert_called_once_with(str(tmp_path / "w1"))
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        # #74の核心: 依存先(#2)は誤って昇格させない
        assert report.promotion_events == []
        assert all(
            call.args != (2, "status:queued") for call in mock_add_label.call_args_list
        )

        persisted = json.loads(run_state_path.read_text())
        assert "1" not in persisted["active_worktrees"]
        assert persisted["completed_worktrees"] == []

    def test_freed_quota_allows_new_task_to_launch_same_cycle(self, tmp_path):
        """#193の核心: 完了検知でクオータが解放され、同一サイクル内で
        新規タスクが選出・起動されることを検証する（恒久停止バグの回帰テスト）。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        queued_issue = _issue(2, footprint=("src/bar.py",), subtask_id="task-b")
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label"),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):

            def _list(label, **_):
                if label == "status:in-progress":
                    return [in_progress_issue]
                if label == "status:queued":
                    return [queued_issue]
                return []

            mock_list.side_effect = _list
            mock_subproc_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 999
            report = run_dispatch_cycle(config)

        assert [t.issue_number for t in report.selected] == [2]
        mock_add_label.assert_any_call(2, "status:in-progress")

        persisted = json.loads(run_state_path.read_text())
        assert "1" not in persisted["active_worktrees"]
        assert "2" in persisted["active_worktrees"]


class TestRunDispatchCycleNotNeeded:
    """#280: status:not-neededラベル検知による完全自動クローズ・依存解決。"""

    def _config(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            max_concurrent=1,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        defaults.update(overrides)
        return DispatcherConfig(**defaults)

    def _seed_active(self, tmp_path, run_state_path, **overrides):
        defaults = dict(
            issue_number=1,
            branch="claude/issue-1-task-a",
            worktree_path=str(tmp_path / "w1"),
            pid=111,
            started_at=1_699_999_000.0,
            declared_footprint=("src/foo.py",),
        )
        defaults.update(overrides)
        save_run_state(
            RunState(
                active_worktrees={"1": ActiveWorktree(**defaults)}, launch_history=[]
            ),
            run_state_path,
        )

    def test_not_needed_label_closes_issue_regardless_of_pr_or_process_state(
        self, tmp_path
    ):
        """セッションがコミット・PRを一切作らない対応不要ケースでも、
        PID/PR存在に依存せずラベル検知だけで完了・クローズできることを検証する
        （#250で観測された、永遠にstatus:in-progressのままスタックする問題の回帰テスト）。"""
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path)
        not_needed_issue = _issue(1, labels=("status:not-needed",), subtask_id="task-a")
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.close_issue") as mock_close_issue,
            # プロセスは生きたまま・PRも存在しない、という「対応不要」の典型状態
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [not_needed_issue] if label == "status:not-needed" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_called_once_with(str(tmp_path / "w1"))
        mock_remove_label.assert_any_call(1, "status:in-progress")
        mock_close_issue.assert_called_once()
        assert mock_close_issue.call_args.args[0] == 1
        assert mock_close_issue.call_args.args[1] == "not planned"
        assert report.completion_events == [
            {
                "issue_number": 1,
                "worktree_path": str(tmp_path / "w1"),
                "action": "not_needed",
                "subtask_id": "task-a",
            }
        ]

        persisted = json.loads(run_state_path.read_text())
        assert persisted["active_worktrees"] == {}

    def test_dry_run_not_needed_does_not_call_github_or_mutate(self, tmp_path):
        run_state_path = tmp_path / "run_state.json"
        self._seed_active(tmp_path, run_state_path)
        config = self._config(tmp_path, run_state_path, apply=False)
        not_needed_issue = _issue(1, labels=("status:not-needed",), subtask_id="task-a")
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.close_issue") as mock_close_issue,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_worktree,
        ):
            mock_list.side_effect = lambda label, **_: (
                [not_needed_issue] if label == "status:not-needed" else []
            )
            report = run_dispatch_cycle(config)

        mock_remove_worktree.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_close_issue.assert_not_called()
        assert report.completion_events[0]["action"] == "not_needed"

        persisted = json.loads(run_state_path.read_text())
        assert "1" in persisted["active_worktrees"]

    def test_blocked_task_promotes_when_dependency_is_not_needed(self, tmp_path):
        """対応不要と判定された依存先も、status:done同様に依存解決済みとして
        扱われ、後続のstatus:blockedタスクがstatus:queuedへ昇格すること。"""
        run_state_path = tmp_path / "run_state.json"
        config = self._config(tmp_path, run_state_path, max_concurrent=2)
        not_needed_issue = _issue(1, labels=("status:not-needed",), subtask_id="task-a")
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
        ):

            def _list(label, **_):
                if label == "status:not-needed":
                    return [not_needed_issue]
                if label == "status:blocked":
                    return [blocked_issue]
                return []

            mock_list.side_effect = _list
            report = run_dispatch_cycle(config)

        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:queued")
        assert report.promotion_events == [{"issue_number": 2, "subtask_id": "task-b"}]


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
        done_issue = _issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
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
        done_issue = _issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
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
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
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
        in_progress_issue = _issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc.worktree_has_new_commits",
                return_value=True,
            ),
            patch("orchestune.dispatch_gc.remove_worktree"),
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
        done_issue = _issue(1, labels=("status:done",), subtask_id="task-a")
        blocked_issue = _issue(
            2,
            labels=("status:blocked",),
            subtask_id="task-b",
            depends_on=("task-a",),
        )
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
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
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
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
        issue = _issue(1)
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
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


class TestDispatcherLocking:
    def test_run_dispatch_cycle_raises_runtime_error_if_locked(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            apply=False,
        )
        lock_path = Path(config.run_state_path).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        import fcntl

        with open(lock_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

            with pytest.raises(RuntimeError) as exc_info:
                with (
                    patch(
                        "orchestune.dispatcher.github.list_issues_by_label",
                        return_value=[],
                    ),
                    patch(
                        "orchestune.dispatcher.github.list_remote_branches",
                        return_value=[],
                    ),
                    patch(
                        "orchestune.dispatcher.github.list_open_prs", return_value=[]
                    ),
                ):
                    run_dispatch_cycle(config)
            assert "Another instance is already running" in str(exc_info.value)


class TestBranchStacking:
    def test_stacking_blocked_task_when_dependency_pr_ci_passes(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        blocked_issue = _issue(
            2, labels=("status:blocked",), subtask_id="task-2", depends_on=("task-1",)
        )
        parent_issue = _issue(1, labels=("status:in-progress",), subtask_id="task-1")

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=("src/a.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_list.side_effect = lambda label, **_: (
                [blocked_issue]
                if label == "status:blocked"
                else [parent_issue]
                if label == "status:in-progress"
                else []
            )
            mock_launch.return_value = MagicMock(
                launched=True,
                pid=123,
                branch="claude/issue-2-task-2",
                worktree_path="worktrees/claude-issue-2-task-2",
                error_message=None,
                external_id=None,
                external_url=None,
            )

            report = run_dispatch_cycle(config)

        mock_launch.assert_called_once_with(
            ANY,
            "claude/issue-2-task-2",
            ANY,
            ANY,
            apply=True,
            base_branch="claude/issue-1-task-1",
        )
        mock_remove_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:in-progress")
        assert len(report.selected) == 1

    def test_stacking_depth_limit_of_one(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=3,
            max_launches_per_window=3,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2, labels=("status:blocked",), subtask_id="task-2", depends_on=("task-1",)
        )
        issue_c = _issue(
            3, labels=("status:blocked",), subtask_id="task-3", depends_on=("task-2",)
        )

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=("src/a.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.dispatcher.github.add_label"),
            patch("orchestune.dispatcher.github.remove_label"),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_b, issue_c]
                if label == "status:blocked"
                else [issue_a]
                if label == "status:in-progress"
                else []
            )
            mock_launch.return_value = MagicMock(
                launched=True,
                pid=123,
                branch="claude/issue-2-task-2",
                worktree_path="worktrees/claude-issue-2-task-2",
                error_message=None,
                external_id=None,
                external_url=None,
            )

            run_dispatch_cycle(config)

        mock_launch.assert_called_once_with(
            ANY,
            "claude/issue-2-task-2",
            ANY,
            ANY,
            apply=True,
            base_branch="claude/issue-1-task-1",
        )

    def test_auto_rebase_success(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        # BはAに依存。AはPR状態、Bは実行中（active_worktrees）
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2,
            labels=("status:in-progress",),
            subtask_id="task-2",
            depends_on=("task-1",),
        )

        run_state = RunState(
            active_worktrees={
                "2": ActiveWorktree(
                    issue_number=2,
                    branch="claude/issue-2-task-2",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-2-task-2"),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.dispatcher.github.add_label"),
            patch("orchestune.dispatcher.github.remove_label"),
            # os.kill と Popen のモック（リブートプロセスのため）
            patch("orchestune.dispatch_rebase.os.kill") as mock_kill,
            patch("orchestune.dispatch_worktree.subprocess.Popen") as mock_popen,
            # git コマンド実行のモック
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="claude/issue-1-task-1",
            ),
        ):

            def list_issues_by_label_mock(label, **_):
                if label == "status:in-progress":
                    return [issue_a, issue_b]
                return []

            mock_list.side_effect = list_issues_by_label_mock

            def kill_mock(pid, sig):
                if sig == 0:
                    raise ProcessLookupError()
                return None

            mock_kill.side_effect = kill_mock

            # subprocess.runのモック動作
            def run_mock(args, **kwargs):
                if "merge-base" in args:
                    return subprocess.CompletedProcess(
                        args=args, returncode=1, stdout="", stderr=""
                    )
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="", stderr=""
                )

            mock_run.side_effect = run_mock
            mock_popen.return_value.pid = 99999

            run_dispatch_cycle(config)

        # プロセスがkillされ、rebaseされ、再起動されたことを確認
        mock_kill.assert_any_call(12345, 9)  # SIGKILL (or SIGTERM)
        # rebase実行の引数チェック
        rebase_call = mock_run.call_args_list[1]
        assert "rebase" in rebase_call.args[0]
        assert "claude/issue-1-task-1" in rebase_call.args[0]

        # 新しいPIDで状態が保存されていることを確認
        loaded = load_run_state(config.run_state_path)
        assert loaded.active_worktrees["2"].pid == 99999

    def test_stacking_blocked_when_multiple_dependencies_unmerged(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=3,
            max_launches_per_window=3,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(2, labels=("status:in-progress",), subtask_id="task-2")
        issue_c = _issue(
            3,
            labels=("status:blocked",),
            subtask_id="task-3",
            depends_on=("task-1", "task-2"),
        )

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=[
                    "origin/claude/issue-1-task-1",
                    "origin/claude/issue-2-task-2",
                ],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=("src/a.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    ),
                    PrRecord(
                        number=11,
                        head_ref="claude/issue-2-task-2",
                        changed_files=("src/b.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    ),
                ],
            ),
            patch("orchestune.dispatcher.github.add_label"),
            patch("orchestune.dispatcher.github.remove_label"),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_c]
                if label == "status:blocked"
                else [issue_a, issue_b]
                if label == "status:in-progress"
                else []
            )

            run_dispatch_cycle(config)

        mock_launch.assert_not_called()

    def test_stacking_blocked_task_when_dependency_completes_in_same_cycle(
        self, tmp_path
    ):
        config = DispatcherConfig(
            max_concurrent=3,
            max_launches_per_window=3,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        # タスクC(3) は タスクB(2) に依存、タスクB(2) は タスクA(1) に依存
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2, labels=("status:blocked",), subtask_id="task-2", depends_on=("task-1",)
        )
        issue_c = _issue(
            3, labels=("status:blocked",), subtask_id="task-3", depends_on=("task-2",)
        )

        # タスクA（issue 1）は active_worktrees に登録されており、このサイクルで完了する
        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-1-task-1"),
                    pid=123,
                    started_at=1700000000.0,
                    declared_footprint=(),
                    base_branch="origin/main",
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=[
                    "origin/claude/issue-1-task-1",
                    "origin/claude/issue-2-task-2",
                ],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=11,
                        head_ref="claude/issue-2-task-2",
                        changed_files=("src/b.py",),
                        review_decision="APPROVED",
                        is_ci_passing=True,  # 依存先BのPRはCI通過済み
                    )
                ],
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
            # タスクAの完了判定とGC処理のためのモック
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=True),
            patch(
                "orchestune.dispatch_gc._finalize_completed_worktree",
                return_value={
                    "action": "completed",
                    "issue_number": 1,
                    "subtask_id": "task-1",
                    "commit_sha": "abc1234",
                },
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_b, issue_c]
                if label == "status:blocked"
                else [issue_a]
                if label == "status:in-progress"
                else []
            )
            mock_launch.return_value = MagicMock(
                launched=True,
                pid=456,
                branch="claude/issue-3-task-3",
                worktree_path="worktrees/claude-issue-3-task-3",
                error_message=None,
                external_id=None,
                external_url=None,
            )

            report = run_dispatch_cycle(config)

        # 同一サイクル内で依存先Aが完了し、かつBのPRがCI通過済みのため、
        # タスクC（issue 3）がタスクBのブランチをベースにスタッキング起動される
        mock_launch.assert_called_once_with(
            ANY,
            "claude/issue-3-task-3",
            ANY,
            ANY,
            apply=True,
            base_branch="claude/issue-2-task-2",
        )
        mock_remove_label.assert_any_call(3, "status:blocked")
        mock_add_label.assert_any_call(3, "status:in-progress")
        assert len(report.selected) == 1

    def test_auto_rebase_conflict(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2,
            labels=("status:in-progress",),
            subtask_id="task-2",
            depends_on=("task-1",),
        )

        run_state = RunState(
            active_worktrees={
                "2": ActiveWorktree(
                    issue_number=2,
                    branch="claude/issue-2-task-2",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-2-task-2"),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch(
                "orchestune.dispatcher.github.list_issues_by_label",
                side_effect=lambda label, **_: (
                    [issue_a, issue_b] if label == "status:in-progress" else []
                ),
            ),
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="APPROVED",
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_rebase.os.kill") as mock_kill,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
            patch(
                "orchestune.dispatch_rebase.resolve_local_or_remote_branch",
                return_value="claude/issue-1-task-1",
            ),
        ):
            mock_kill.side_effect = lambda pid, sig: (
                ProcessLookupError() if sig == 0 else None
            )
            # 1. git merge-base -> 戻り値 1
            # 2. git rebase -> 戻り値 128 (競合発生で失敗)
            # 3. git rebase --abort -> 戻り値 0
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=""
                ),
                subprocess.CalledProcessError(returncode=128, cmd="git rebase"),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]

            run_dispatch_cycle(config)

        # rebase abort が呼ばれたこと
        abort_call = mock_run.call_args_list[2]
        assert "--abort" in abort_call.args[0]

        # 安全停止（ラベル遷移）が行われたこと
        mock_remove_label.assert_any_call(2, "status:in-progress")
        mock_add_label.assert_any_call(2, "status:manual-merge-required")
        mock_add_comment.assert_called_once()

        # active_worktrees から除外されたこと（worktree削除はしない）
        loaded = load_run_state(config.run_state_path)
        assert "2" not in loaded.active_worktrees

    def test_changes_requested_escalation(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
        )
        # BはAに依存。AはPR状態(CHANGES_REQUESTED)、Bは実行中（active_worktrees）
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        issue_b = _issue(
            2,
            labels=("status:in-progress",),
            subtask_id="task-2",
            depends_on=("task-1",),
        )

        run_state = RunState(
            active_worktrees={
                "2": ActiveWorktree(
                    issue_number=2,
                    branch="claude/issue-2-task-2",
                    worktree_path=str(tmp_path / "worktrees/claude-issue-2-task-2"),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch(
                "orchestune.dispatcher.github.list_issues_by_label",
                side_effect=lambda label, **_: (
                    [issue_a, issue_b] if label == "status:in-progress" else []
                ),
            ),
            patch(
                "orchestune.dispatcher.github.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="CHANGES_REQUESTED",  # ここがポイント
                        is_ci_passing=True,
                    )
                ],
            ),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatcher.os.kill") as mock_kill,
            patch("orchestune.dispatch_worktree.subprocess.run"),
        ):
            run_dispatch_cycle(config)

        # プロセスがkillされたこと
        mock_kill.assert_called_with(12345, 9)

        # エスカレーションラベル付与
        mock_remove_label.assert_any_call(2, "status:in-progress")
        mock_add_label.assert_any_call(2, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "一時停止" in mock_add_comment.call_args[0][1]

        # active_worktrees から除外されたこと
        loaded = load_run_state(config.run_state_path)
        assert "2" not in loaded.active_worktrees


class TestGC:
    def test_gc_reclaim_zombie(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=3600,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")

        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        git_calls = [call.args[0] for call in mock_run.call_args_list]
        assert any("add" in cmd for cmd in git_calls)
        assert any("commit" in cmd for cmd in git_calls)

        mock_remove_wt.assert_called_once_with(str(wt_path))
        mock_remove_label.assert_called_with(1, "status:in-progress")
        mock_add_label.assert_called_with(1, "status:queued")
        mock_add_comment.assert_called_once()

        loaded = load_run_state(config.run_state_path)
        assert "1" not in loaded.active_worktrees

    def test_gc_reclaim_zombie_only(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=0,
            zombie_gc=True,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_called_once_with(str(wt_path))
        mock_remove_label.assert_called_with(1, "status:in-progress")
        mock_add_label.assert_called_with(1, "status:queued")
        mock_add_comment.assert_called_once()

        loaded = load_run_state(config.run_state_path)
        assert "1" not in loaded.active_worktrees

    def test_gc_reclaim_zombie_disabled(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=0,
            zombie_gc=False,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_label.assert_not_called()
        mock_add_comment.assert_not_called()

        loaded = load_run_state(config.run_state_path)
        assert "1" in loaded.active_worktrees

    def test_gc_reclaim_timeout(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=600,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        import time

        old_time = time.time() - 1000

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=old_time,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=True),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=""
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_called_once_with(str(wt_path))
        mock_remove_label.assert_called_with(1, "status:in-progress")
        mock_add_label.assert_called_with(1, "status:queued")
        mock_add_comment.assert_called_once()

        loaded = load_run_state(config.run_state_path)
        assert "1" not in loaded.active_worktrees

    def test_gc_reclaim_backup_failure_skips_deletion(self, tmp_path):
        config = DispatcherConfig(
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=True,
            task_timeout_seconds=3600,
        )
        issue_a = _issue(1, labels=("status:in-progress",), subtask_id="task-1")
        wt_path = tmp_path / "worktrees/claude-issue-1-task-1"
        wt_path.mkdir(parents=True, exist_ok=True)

        run_state = RunState(
            active_worktrees={
                "1": ActiveWorktree(
                    issue_number=1,
                    branch="claude/issue-1-task-1",
                    worktree_path=str(wt_path),
                    pid=12345,
                    started_at=1700000000.0,
                    declared_footprint=(),
                )
            }
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatch_gc.is_process_alive", return_value=False),
            patch(
                "orchestune.dispatch_gc.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_gc.remove_worktree") as mock_remove_wt,
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_run,
        ):
            mock_list.side_effect = lambda label, **_: (
                [issue_a] if label == "status:in-progress" else []
            )
            mock_run.side_effect = subprocess.CalledProcessError(
                returncode=1,
                cmd="git commit",
                stderr="fatal: unable to write new index file",
            )

            run_dispatch_cycle(config)

        mock_remove_wt.assert_not_called()
        mock_remove_label.assert_not_called()
        mock_add_label.assert_not_called()
        mock_add_comment.assert_called_once()
        assert (
            "WIPバックアップコミットの作成に失敗しました"
            in mock_add_comment.call_args[0][1]
        )

        loaded = load_run_state(config.run_state_path)
        assert "1" in loaded.active_worktrees


class TestLaunchOrderingCrashSafety:
    """run_stateへの登録とGitHubラベル更新の順序を入れ替え、クラッシュ時に
    「GitHub側は確定・run_state側は空」という検出不能な非対称が起きないようにする。"""

    def test_run_state_is_persisted_before_label_transition_and_survives_crash(
        self, tmp_path
    ):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1)

        def remove_label_side_effect(issue_number, label):
            if label == "status:queued":
                raise RuntimeError("simulated crash during label transition")

        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch(
                "orchestune.dispatcher.github.remove_label",
                side_effect=remove_label_side_effect,
            ),
            patch("orchestune.dispatch_worktree.subprocess.run") as mock_subproc_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            mock_subproc_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            mock_popen.return_value.pid = 555

            with pytest.raises(RuntimeError, match="simulated crash"):
                run_dispatch_cycle(config)

        # ラベル遷移（status:in-progress付与）はクラッシュにより行われていない。
        mock_add_label.assert_not_called()

        # しかし、run_state.json にはactive_worktreeエントリが既に永続化されている
        # （ラベル更新より前にsave_run_stateが呼ばれる順序になっているため）。
        assert (tmp_path / "run_state.json").exists()
        persisted = json.loads((tmp_path / "run_state.json").read_text())
        assert "1" in persisted["active_worktrees"]


class TestStaleActiveEntryReconciliation:
    """run_stateにエントリが残っているが、GitHub側のラベルが実際には
    status:in-progressになっていない（起動直後のクラッシュ等による）場合、
    run_state側を破棄してGitHubラベルを正とする（ゾンビGCの拡張）。"""

    def test_stale_entry_without_in_progress_label_is_discarded(self, tmp_path):
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
        config = DispatcherConfig(
            max_concurrent=0,
            max_launches_per_window=0,
            window_seconds=3600,
            run_state_path=run_state_path,
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        # 起動処理自体はcreate_worktree_and_launch成功後の何らかの時点で
        # クラッシュしており、GitHub側のラベルは "status:queued" のまま
        # （status:in-progressへの遷移は未完了）という状況を再現する。
        queued_issue = _issue(1, labels=("status:queued",), subtask_id="task-1")

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        # GC/完了検知としてはラベルに一切触らない（GitHub側は既にqueuedのまま
        # で正しいため、ここでラベル操作をしてはいけない）。
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()

        assert any(
            event.get("action") == "stale_active_entry_discarded"
            for event in report.completion_events
        )

        loaded = load_run_state(run_state_path)
        assert "1" not in loaded.active_worktrees


class TestPreventDuplicateSessions:
    @patch("orchestune.dispatcher.github.list_issues_by_label")
    @patch("orchestune.dispatcher.github.list_remote_branches", return_value=[])
    @patch("orchestune.dispatcher.github.list_open_prs")
    @patch("orchestune.dispatcher.github.remove_label")
    @patch("orchestune.dispatcher.github.add_label")
    @patch("orchestune.dispatcher.github.add_comment")
    @patch("orchestune.dispatch_worktree.subprocess.run")
    @patch("orchestune.dispatch_targets.subprocess.Popen")
    def test_run_dispatch_cycle_skips_launch_if_open_pr_exists(
        self,
        mock_popen,
        mock_subproc_run,
        mock_add_comment,
        mock_add_label,
        mock_remove_label,
        mock_list_prs,
        mock_list_branches,
        mock_list_issues,
        tmp_path,
    ):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        mock_list_issues.side_effect = lambda label, **_: (
            [queued_issue] if label == "status:queued" else []
        )

        # open PR with expected head_ref branch name exists
        mock_list_prs.return_value = [
            PrRecord(
                number=10,
                head_ref="claude/issue-1-task-1",
                changed_files=(),
                closes_issue_numbers=(),
                review_decision="",
                is_ci_passing=True,
            )
        ]

        report = run_dispatch_cycle(config)

        # 起動がスキップされていること
        assert len(report.selected) == 0
        assert mock_popen.call_count == 0

        # ラベル遷移とコメント追加が行われていること
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "重複起動防止" in mock_add_comment.call_args[0][1]

    @patch("orchestune.dispatcher.github.list_issues_by_label")
    @patch("orchestune.dispatcher.github.list_remote_branches", return_value=[])
    @patch("orchestune.dispatcher.github.list_open_prs")
    @patch("orchestune.dispatcher.github.remove_label")
    @patch("orchestune.dispatcher.github.add_label")
    @patch("orchestune.dispatcher.github.add_comment")
    @patch("orchestune.dispatch_worktree.subprocess.run")
    @patch("orchestune.dispatch_targets.subprocess.Popen")
    def test_run_dispatch_cycle_ignores_unrelated_closes_issue_pr(
        self,
        mock_popen,
        mock_subproc_run,
        mock_add_comment,
        mock_add_label,
        mock_remove_label,
        mock_list_prs,
        mock_list_branches,
        mock_list_issues,
        tmp_path,
    ):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        mock_list_issues.side_effect = lambda label, **_: (
            [queued_issue] if label == "status:queued" else []
        )

        # unrelated open PR closes issue 1, but head_ref does not follow Orchestune launch branch naming
        mock_list_prs.return_value = [
            PrRecord(
                number=10,
                head_ref="some-other-branch-name",
                changed_files=(),
                closes_issue_numbers=(1,),
                review_decision="",
                is_ci_passing=True,
            )
        ]
        mock_popen.return_value.pid = 12345

        report = run_dispatch_cycle(config)

        # 無関係なPRは重複扱いせず、起動候補として残ること
        assert len(report.selected) == 1
        mock_popen.assert_called_once()
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:in-progress")
        mock_add_comment.assert_not_called()

    def test_ls_remote_uses_existing_pr_head_ref_for_closes_issue_match(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        save_run_state(
            RunState(
                completed_worktrees=[
                    CompletedWorktree(
                        issue_number=1,
                        subtask_id="task-1",
                        branch="claude/issue-1-task-1",
                        started_at=100.0,
                        completed_at=200.0,
                        commit_sha="old-sha",
                    )
                ]
            ),
            config.run_state_path,
        )

        def ls_remote_result(command, **_kwargs):
            stdout = (
                "updated-sha\trefs/heads/claude/issue-1-human-authored\n"
                if command
                == [
                    "git",
                    "ls-remote",
                    "origin",
                    "refs/heads/claude/issue-1-human-authored",
                ]
                else ""
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=101,
                        head_ref="claude/issue-1-human-authored",
                        changed_files=(),
                        review_decision="",
                        is_ci_passing=False,
                        closes_issue_numbers=(1,),
                    )
                ],
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch(
                "orchestune.dispatch_worktree.subprocess.run",
                side_effect=ls_remote_result,
            ) as mock_subprocess_run,
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_popen.return_value.pid = 12345
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_popen.assert_not_called()
        mock_subprocess_run.assert_called_once_with(
            [
                "git",
                "ls-remote",
                "origin",
                "refs/heads/claude/issue-1-human-authored",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()

    def test_ls_remote_failure_transitions_to_blocked_human_review(self, tmp_path):
        """ls-remoteが例外等で失敗した場合は、安全のため重複とみなして起動をスキップする。"""
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        # すでに過去の完了履歴があり、かつオープンなPRがあるタスク
        queued_issue = _issue(1, subtask_id="task-1")
        run_state = RunState(
            completed_worktrees=[
                CompletedWorktree(
                    issue_number=1,
                    subtask_id="task-1",
                    branch="claude/issue-1-task-1",
                    started_at=100.0,
                    completed_at=200.0,
                    commit_sha="old-sha",
                )
            ]
        )
        save_run_state(run_state, config.run_state_path)

        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch(
                "orchestune.dispatcher.github.list_open_prs",
                return_value=[
                    PrRecord(
                        number=101,
                        head_ref="claude/issue-1-task-1",
                        changed_files=(),
                        review_decision="",
                        is_ci_passing=False,
                        closes_issue_numbers=(1,),
                    )
                ],
            ),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch(
                "orchestune.dispatch_worktree.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    returncode=128, cmd="git ls-remote"
                ),
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        # 起動はスキップされ、status:blocked-human-review ラベルが付与されている
        assert report.selected == []
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "重複起動防止" in mock_add_comment.call_args[0][1]


class TestSyncExternalLocks:
    @patch("orchestune.dispatcher.github.list_remote_branches")
    @patch("orchestune.dispatcher.github.remove_label")
    @patch("orchestune.dispatcher.github.add_label")
    def test_sync_external_locks_unlocks_without_requeue_for_done_tasks(
        self, mock_add_label, mock_remove_label, mock_list_branches
    ):
        from orchestune.dispatcher import _sync_external_locks

        mock_list_branches.return_value = []

        done_task = Task(
            issue_number=1,
            subtask_id="task-1",
            footprint=("src/shared.py",),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=("status:done", "status:external-lock"),
            created_at="2026-01-01T00:00:00+00:00",
        )

        run_state = RunState(active_worktrees={})
        config = DispatcherConfig(apply=True)

        res = _sync_external_locks(
            tasks_by_issue={1: done_task},
            prs=[],
            run_state=run_state,
            config=config,
        )

        assert res.to_lock == []
        assert [t.issue_number for t in res.to_unlock] == [1]

        mock_remove_label.assert_called_once_with(1, "status:external-lock")
        assert mock_add_label.call_count == 0

    def test_write_github_step_summary(self, tmp_path):
        from orchestune.dispatcher import write_github_step_summary

        summary_file = tmp_path / "step_summary.md"

        task_selected = Task(
            issue_number=10,
            subtask_id="task-launch-10",
            footprint=(),
            symbols=(),
            risk=False,
            priority="high",
            progress_partial=False,
            status_labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )
        task_lock = Task(
            issue_number=20,
            subtask_id="task-lock-20",
            footprint=(),
            symbols=(),
            risk=False,
            priority="medium",
            progress_partial=False,
            status_labels=(),
            created_at="2026-01-01T00:00:00+00:00",
        )

        cycle_report = CycleReport(
            selected=[task_selected],
            quota_slots_available=1,
            lock_changes={
                "to_lock": [task_lock],
                "to_unlock": [],
            },
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=True,
        )

        integrator_report = {
            "status": "partial_success",
            "merged": ["task-merged-1"],
            "failed": ["task-failed-2"],
            "failed_reasons": {
                "task-failed-2": "CI verification failed\nDetailed error message here"
            },
        }

        write_github_step_summary(
            cycle_report=cycle_report,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        assert summary_file.exists()
        content = summary_file.read_text(encoding="utf-8")
        assert "## 🤖 Orchestune Dispatch Summary" in content
        assert "### 🔍 仮マージ検証（Integrator）結果" in content
        assert "全体ステータス: **partial_success**" in content
        assert "| `task-merged-1` | ✅ 成功 |" in content
        assert "| `task-failed-2` | ❌ 失敗 | CI verification failed |" in content
        assert "### 🚀 新規起動タスク" in content
        assert "| `task-launch-10` | #10 | high |" in content
        assert "### 🔒 外部ロック（External Lock）変更" in content
        assert (
            "| `task-lock-20` | #20 | 🔒 ロック付与 (`status:external-lock`) |"
            in content
        )

    def test_write_github_step_summary_includes_integration_pr_link(
        self, tmp_path, monkeypatch
    ):
        from orchestune.dispatcher import write_github_step_summary

        monkeypatch.setenv("GITHUB_REPOSITORY", "Saltmu/manuscriptune")
        summary_file = tmp_path / "step_summary.md"

        integrator_report = {
            "status": "success",
            "merged": ["task-merged-1"],
            "failed": [],
            "integration_pr_number": 315,
        }

        write_github_step_summary(
            cycle_report=None,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "統合PR #315" in content
        assert "https://github.com/Saltmu/manuscriptune/pull/315" in content

    def test_write_github_step_summary_without_repository_env_omits_link(
        self, tmp_path, monkeypatch
    ):
        from orchestune.dispatcher import write_github_step_summary

        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        summary_file = tmp_path / "step_summary.md"

        integrator_report = {
            "status": "success",
            "merged": ["task-merged-1"],
            "failed": [],
            "integration_pr_number": 315,
        }

        write_github_step_summary(
            cycle_report=None,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "統合PR #315" in content
        assert "https://github.com/" not in content

    def test_write_github_step_summary_no_pr_number_omits_pr_line(self, tmp_path):
        from orchestune.dispatcher import write_github_step_summary

        summary_file = tmp_path / "step_summary.md"

        integrator_report = {
            "status": "success",
            "merged": ["task-merged-1"],
            "failed": [],
            "integration_pr_number": None,
        }

        write_github_step_summary(
            cycle_report=None,
            integrator_report=integrator_report,
            summary_path=str(summary_file),
        )

        content = summary_file.read_text(encoding="utf-8")
        assert "統合PR" not in content


class TestBuildArgParser:
    """#328: dispatch-cycleの既定挙動をapplyに変更（--no-applyでdry-run）。"""

    def test_apply_defaults_to_true(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.apply is True

    def test_no_apply_flag_disables_apply(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--no-apply"])
        assert args.apply is False

    def test_explicit_apply_flag_still_works(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--apply"])
        assert args.apply is True

    def test_dispatch_target_defaults_to_none_when_unspecified(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.dispatch_target is None

    def test_dispatch_target_explicit_local_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "local"])
        assert args.dispatch_target == "local"

    def test_dispatch_target_explicit_auto_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "auto"])
        assert args.dispatch_target == "auto"

    def test_dispatch_target_explicit_codex_cli_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "codex-cli"])
        assert args.dispatch_target == "codex-cli"

    def test_dispatch_target_explicit_codex_cloud_is_preserved(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dispatch-target", "codex-cloud"])
        assert args.dispatch_target == "codex-cloud"

    def test_codex_cloud_env_option_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--codex-cloud-env", "env_123"])
        assert args.codex_cloud_env == "env_123"

    def test_task_timeout_seconds_defaults_to_zero(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.task_timeout_seconds == 0

    def test_task_timeout_seconds_arg_is_parsed(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--task-timeout-seconds", "3600"])
        assert args.task_timeout_seconds == 3600

    def test_zombie_gc_defaults_to_true(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args([])
        assert args.zombie_gc is True

    def test_no_zombie_gc_disables_zombie_gc(self):
        from orchestune.dispatcher import _build_arg_parser

        args = _build_arg_parser().parse_args(["--no-zombie-gc"])
        assert args.zombie_gc is False


class TestConfigDefaults:
    def test_config_defaults_load(self):
        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        config_data = {
            "task-timeout-seconds": 1200,
            "zombie-gc": False,
        }
        defaults = _config_defaults(parser, config_data)
        assert defaults["task_timeout_seconds"] == 1200
        assert defaults["zombie_gc"] is False

    def test_config_defaults_validation_error(self):
        import pytest

        from orchestune.dispatcher import _build_arg_parser, _config_defaults

        parser = _build_arg_parser()
        with pytest.raises(SystemExit):
            _config_defaults(parser, {"task-timeout-seconds": -1})

        with pytest.raises(SystemExit):
            _config_defaults(parser, {"zombie-gc": "invalid"})


class TestMainDispatchTargetAutoDetection:
    """#121: --dispatch-target未指定時、mainが実行環境に応じた実ディスパッチ先を
    build_dispatch_targetへ渡すことを検証する（ダミー動作への誤フォールバック防止）。"""

    def _empty_report(self):
        return CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )

    def test_defaults_to_auto_outside_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply", "--run-state-path", str(tmp_path / "rs.json")])

        assert mock_build.call_args.args[0] == "auto"

    def test_defaults_to_cloud_routine_in_github_actions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply", "--run-state-path", str(tmp_path / "rs.json")])

        assert mock_build.call_args.args[0] == "cloud-routine"

    def test_explicit_local_wins_even_inside_github_actions(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(
                [
                    "--no-apply",
                    "--dispatch-target",
                    "local",
                    "--run-state-path",
                    str(tmp_path / "rs.json"),
                ]
            )

        assert mock_build.call_args.args[0] == "local"


class TestDecideSemanticReviewEnabled:
    """#150: ORCHESTUNE_SEMANTIC_REVIEW環境変数によるON/OFF判定（副作用なし）。"""

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ORCHESTUNE_SEMANTIC_REVIEW", raising=False)
        assert _decide_semantic_review_enabled() is True

    def test_disabled_when_env_var_is_zero(self, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "0")
        assert _decide_semantic_review_enabled() is False

    def test_enabled_when_env_var_is_nonzero(self, monkeypatch):
        monkeypatch.setenv("ORCHESTUNE_SEMANTIC_REVIEW", "1")
        assert _decide_semantic_review_enabled() is True


class TestPollPendingNotNeededReviews:
    """#150/#282: 保留中のstatus:not-needed検証レビューのポーリング（ベストエフォート）。"""

    def test_returns_report_on_success(self, tmp_path):
        args = argparse.Namespace(not_needed_review_state_path=tmp_path / "s.json")
        with patch(
            "orchestune.integration_coordinator.process_pending_not_needed_reviews",
            return_value={"processed": 1},
        ) as mock_poll:
            result = _poll_pending_not_needed_reviews(args)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {"processed": 1}
        mock_poll.assert_called_once_with(args.not_needed_review_state_path)

    def test_returns_none_and_warns_on_failure(self, tmp_path, capsys):
        args = argparse.Namespace(not_needed_review_state_path=tmp_path / "s.json")
        with patch(
            "orchestune.integration_coordinator.process_pending_not_needed_reviews",
            side_effect=RuntimeError("boom"),
        ):
            result = _poll_pending_not_needed_reviews(args)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert "boom" in result.error_message
        assert "boom" in capsys.readouterr().err

    def test_returns_fatal_failure_on_forge_auth_error(self, tmp_path, capsys):
        args = argparse.Namespace(not_needed_review_state_path=tmp_path / "s.json")
        result = _poll_pending_not_needed_reviews(
            args, auth_error=ForgeAuthError("auth-failed")
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err


class TestRunSemanticIntegrator:
    """#150: Integrator実行と、クラウドルーチン利用時のみ意味的レビューを
    有効化する分岐（ベストエフォート）。"""

    def test_enables_semantic_review_for_cloud_routine_target(self):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"ok": True}
        with (
            patch(
                "orchestune.integrator.Integrator", return_value=mock_instance
            ) as mock_integrator_cls,
            patch(
                "orchestune.integration_coordinator.IntegrationCoordinator"
            ) as mock_coordinator_cls,
        ):
            result = _run_semantic_integrator(config, semantic_review_enabled=True)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {"ok": True}
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is True
        mock_coordinator_cls.assert_called_once_with(config.dispatch_target)

    def test_disables_semantic_review_when_flag_off(self):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            dispatch_target=ClaudeCodeCloudRoutineDispatchTarget("rid", "rtok"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"ok": True}
        with patch(
            "orchestune.integrator.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is False

    def test_disables_semantic_review_for_non_cloud_routine_target(self, tmp_path):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            dispatch_target=LocalProcessDispatchTarget(log_dir=tmp_path / "logs"),
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"ok": True}
        with patch(
            "orchestune.integrator.Integrator", return_value=mock_instance
        ) as mock_integrator_cls:
            result = _run_semantic_integrator(config, semantic_review_enabled=True)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        integrator_config = mock_integrator_cls.call_args.args[0]
        assert integrator_config.enable_semantic_review is False

    def test_returns_none_and_warns_on_failure(self, capsys):
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )
        with patch(
            "orchestune.integrator.Integrator", side_effect=RuntimeError("boom")
        ):
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert "boom" in result.error_message
        assert "boom" in capsys.readouterr().err

    def test_returns_retryable_failure_when_report_has_failed_tasks(self):
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )
        mock_instance = MagicMock()
        mock_instance.run.return_value = {"status": "failure", "failed": ["task-1"]}
        with patch("orchestune.integrator.Integrator", return_value=mock_instance):
            result = _run_semantic_integrator(config, semantic_review_enabled=False)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert result.report == {"status": "failure", "failed": ["task-1"]}

    def test_returns_fatal_failure_on_forge_auth_error(self, capsys):
        config = DispatcherConfig(
            run_state_path="dummy.json", worktree_root="worktrees"
        )
        result = _run_semantic_integrator(
            config,
            semantic_review_enabled=False,
            auth_error=ForgeAuthError("auth-failed"),
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err


class TestProcessParentCompletion:
    """#170: 親Issue完了検知（best-effort）の配線を確認する。"""

    def test_returns_report_on_success(self):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            parent_issue_number=100,
            apply=True,
        )
        with patch(
            "orchestune.parent_completion.process_parent_completion",
            return_value={"status": "waiting_on_children", "open_children": [101]},
        ) as mock_process:
            result = _process_parent_completion(config)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.SUCCESS
        assert result.report == {
            "status": "waiting_on_children",
            "open_children": [101],
        }
        mock_process.assert_called_once_with(100, True)

    def test_returns_none_and_warns_on_failure(self, capsys):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            parent_issue_number=100,
            apply=True,
        )
        with patch(
            "orchestune.parent_completion.process_parent_completion",
            side_effect=RuntimeError("boom"),
        ):
            result = _process_parent_completion(config)

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.RETRYABLE_FAILURE
        assert result.retryable is True
        assert "boom" in result.error_message
        assert "boom" in capsys.readouterr().err

    def test_returns_fatal_failure_on_forge_auth_error(self, capsys):
        config = DispatcherConfig(
            run_state_path="dummy.json",
            worktree_root="worktrees",
            parent_issue_number=100,
            apply=True,
        )
        result = _process_parent_completion(
            config, auth_error=ForgeAuthError("auth-failed")
        )

        assert isinstance(result, PhaseResult)
        assert result.status == PhaseStatus.FATAL_FAILURE
        assert result.retryable is False
        assert "auth-failed" in result.error_message
        assert "auth-failed" in capsys.readouterr().err


class TestRunDispatchCycleActorVerification:
    """#119: status:queuedラベルを付与したactorの権限が不足している場合、
    run_dispatch_cycle経由でも起動をスキップしエスカレーションすることを確認する。"""

    def test_unauthorized_actor_skips_launch_and_escalates(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            events_log_path=tmp_path / "events.jsonl",
            apply=True,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch("orchestune.dispatcher.github.add_comment") as mock_add_comment,
            patch(
                "orchestune.dispatcher.github.get_label_actor", return_value="mallory"
            ),
            patch(
                "orchestune.dispatcher.github.get_actor_permission",
                return_value="read",
            ),
            patch("orchestune.dispatch_worktree.subprocess.run"),
            patch("orchestune.dispatch_targets.subprocess.Popen") as mock_popen,
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_popen.assert_not_called()
        mock_remove_label.assert_any_call(1, "status:queued")
        mock_add_label.assert_any_call(1, "status:blocked-human-review")
        mock_add_comment.assert_called_once()
        assert "mallory" in mock_add_comment.call_args[0][1]

    def test_dry_run_excludes_unauthorized_actor_without_writes(self, tmp_path):
        config = DispatcherConfig(
            max_concurrent=2,
            max_launches_per_window=2,
            window_seconds=3600,
            run_state_path=tmp_path / "run_state.json",
            worktree_root=tmp_path / "worktrees",
            log_dir=tmp_path / "logs",
            apply=False,
        )
        queued_issue = _issue(1, subtask_id="task-1")
        with (
            patch("orchestune.dispatcher.github.list_issues_by_label") as mock_list,
            patch("orchestune.dispatcher.github.list_remote_branches", return_value=[]),
            patch("orchestune.dispatcher.github.list_open_prs", return_value=[]),
            patch("orchestune.dispatcher.github.add_label") as mock_add_label,
            patch("orchestune.dispatcher.github.remove_label") as mock_remove_label,
            patch(
                "orchestune.dispatcher.github.get_label_actor", return_value="mallory"
            ),
            patch(
                "orchestune.dispatcher.github.get_actor_permission",
                return_value="none",
            ),
        ):
            mock_list.side_effect = lambda label, **_: (
                [queued_issue] if label == "status:queued" else []
            )
            report = run_dispatch_cycle(config)

        assert report.selected == []
        mock_add_label.assert_not_called()
        mock_remove_label.assert_not_called()


class TestDispatcherConfigLoading:
    def _empty_report(self):
        return CycleReport(
            selected=[],
            quota_slots_available=0,
            lock_changes={"to_lock": [], "to_unlock": []},
            deviation_events=[],
            completion_events=[],
            promotion_events=[],
            applied=False,
        )

    def test_load_config_from_orchestune_toml(self, tmp_path):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n"
            "dispatch-target = 'local'\n"
            "run-state-path = 'custom_state.json'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "local"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 5
        assert config_arg.run_state_path == Path("custom_state.json")

    def test_load_config_from_pyproject_toml(self, tmp_path):
        config_path = tmp_path / "pyproject.toml"
        config_path.write_text(
            "[tool.orchestune]\n"
            "max-concurrent = 7\n"
            "dispatch-target = 'claude-cli'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(["--no-apply"], cwd=tmp_path)

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 7

    def test_cli_arg_overrides_config_file(self, tmp_path):
        config_path = tmp_path / "orchestune.toml"
        config_path.write_text(
            "max-concurrent = 5\n" "dispatch-target = 'local'\n",
            encoding="utf-8",
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ) as mock_run,
        ):
            main(
                [
                    "--no-apply",
                    "--max-concurrent",
                    "3",
                    "--dispatch-target",
                    "claude-cli",
                ],
                cwd=tmp_path,
            )

        mock_build.assert_called_once()
        assert mock_build.call_args.args[0] == "claude-cli"
        assert mock_run.called
        config_arg = mock_run.call_args.args[0]
        assert config_arg.max_concurrent == 3

    @pytest.mark.parametrize(
        ("config", "expected_error"),
        [
            ("dispatch-target = 'claude-clii'\n", "dispatch-target"),
            ("max-concurent = 5\n", "unknown key"),
            ('max-concurrent = "5"\n', "must be an integer"),
            ('apply = "false"\n', "must be a boolean"),
            ("max-concurrent = -1\n", "greater than or equal to 0"),
            ("window-seconds = 0\n", "greater than or equal to 1"),
            ("parent-issue = 0\n", "greater than or equal to 1"),
            ("run-state-path = 1\n", "must be a string path"),
        ],
    )
    def test_rejects_invalid_config_values(
        self, tmp_path, config, expected_error, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(config, encoding="utf-8")

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert expected_error in capsys.readouterr().err

    def test_rejects_invalid_toml_without_falling_back_to_pyproject(
        self, tmp_path, capsys
    ):
        (tmp_path / "orchestune.toml").write_text(
            "max-concurrent = [\n", encoding="utf-8"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[tool.orchestune]\nmax-concurrent = 5\n", encoding="utf-8"
        )

        with pytest.raises(SystemExit) as error:
            main(["--no-apply"], cwd=tmp_path)

        assert error.value.code == 2
        assert "failed to load" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "dispatch_target",
        [
            "local",
            "cloud-routine",
            "codex-cloud",
            "claude-cli",
            "agy-cli",
            "codex-cli",
            "auto",
        ],
    )
    def test_accepts_each_dispatch_target(self, tmp_path, dispatch_target):
        (tmp_path / "orchestune.toml").write_text(
            f"dispatch-target = '{dispatch_target}'\n", encoding="utf-8"
        )

        with (
            patch("orchestune.dispatcher.build_dispatch_target") as mock_build,
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
        ):
            main(["--no-apply"], cwd=tmp_path)

        assert mock_build.call_args.args[0] == dispatch_target

    def test_post_cycle_failures_in_main(self, tmp_path, capsys):
        r1 = PhaseResult("poll_pending_not_needed_reviews", PhaseStatus.SUCCESS)
        r2 = PhaseResult("run_semantic_integrator", PhaseStatus.SUCCESS)
        r2_retryable = PhaseResult(
            "run_semantic_integrator",
            PhaseStatus.RETRYABLE_FAILURE,
            error_message="retryable-error",
            retryable=True,
        )
        r2_fatal = PhaseResult(
            "run_semantic_integrator",
            PhaseStatus.FATAL_FAILURE,
            error_message="fatal-error",
        )
        r3 = PhaseResult("process_parent_completion", PhaseStatus.SUCCESS)

        # ケース1: すべて成功
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch("orchestune.dispatcher._run_semantic_integrator", return_value=r2),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 0
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 3
            assert out["post_cycle_results"][0]["status"] == "success"

        # ケース2: RETRYABLE_FAILURE
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator",
                return_value=r2_retryable,
            ),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 2
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "retryable_failure"
            assert out["post_cycle_results"][1]["error_message"] == "retryable-error"

        # ケース3: FATAL_FAILURE
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.dispatcher._poll_pending_not_needed_reviews",
                return_value=r1,
            ),
            patch(
                "orchestune.dispatcher._run_semantic_integrator", return_value=r2_fatal
            ),
            patch("orchestune.dispatcher._process_parent_completion", return_value=r3),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert out["post_cycle_results"][1]["status"] == "fatal_failure"

        # ケース4: main()のcheck_auth()自体がForgeAuthErrorを投げる場合
        with (
            patch(
                "orchestune.dispatcher.run_dispatch_cycle",
                return_value=self._empty_report(),
            ),
            patch(
                "orchestune.forge.GitHubForge.check_auth",
                side_effect=ForgeAuthError("main-auth-failed"),
            ),
        ):
            code = main(["--apply", "--parent-issue", "100"], cwd=tmp_path)
            assert code == 1
            out = json.loads(capsys.readouterr().out)
            assert "post_cycle_results" in out
            assert len(out["post_cycle_results"]) == 3
            for res in out["post_cycle_results"]:
                assert res["status"] == "fatal_failure"
                assert "main-auth-failed" in res["error_message"]
