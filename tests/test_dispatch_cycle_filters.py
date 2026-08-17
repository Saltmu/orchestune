"""dispatch_cycle内のフットプリント逸脱フィルタリング（dispatch_filters.py関連）テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、逸脱によるブロック候補の
フィルタリングと、footprint逸脱検知→DAG再計算の配線テストを分割している
（#343）。
"""

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dag_models import FootprintConflict
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import (
    run_dispatch_cycle,
)
from orchestune.dispatch_filters import _filter_deviation_blocked_candidates
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    save_run_state,
)
from orchestune.issue_parsing import PARENT_MARKER
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


def _issue(number, labels=(), state="OPEN"):
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body="",
        labels=labels,
        created_at="2026-01-01T00:00:00+00:00",
        state=state,
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


class TestFilterDeviationBlockedCandidates:
    def test_excludes_candidates_blocked_by_recomputed_deviation(self):
        blocked = _task(issue_number=2, subtask_id="task-blocked")
        retained = _task(issue_number=3, subtask_id="task-retained")
        events = [
            {
                "action": "recomputed",
                "conflicts": [
                    {"blocked_subtask_id": "task-blocked"},
                    {"blocked_subtask_id": "unknown-task"},
                ],
            }
        ]

        result = _filter_deviation_blocked_candidates(
            [blocked, retained], events, {"task-blocked": 2}
        )

        assert result == [retained]

    def test_ignores_non_recomputed_and_missing_blocked_ids(self):
        candidates = [_task(issue_number=2, subtask_id="task-blocked")]
        events = [
            {
                "action": "blocked-human-review",
                "conflicts": [{"blocked_subtask_id": "task-blocked"}],
            },
            {"action": "recomputed", "conflicts": [{}]},
            {"action": "recomputed"},
        ]

        result = _filter_deviation_blocked_candidates(
            candidates, events, {"task-blocked": 2}
        )

        assert result is candidates

    def test_returns_original_candidates_when_there_are_no_events(self):
        candidates = [_task()]

        result = _filter_deviation_blocked_candidates(candidates, [], {})

        assert result is candidates


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

    def _epic_issue(self) -> IssueRecord:
        """#327: `parent_issue_number=181`は本物のEPICではない実在のバグIssue
        番号（このリポジトリの過去のIncidentに由来）であり、`ensure_parent_branch`
        呼び出し前の`is_epic_issue`検証が本物のGitHub `gh issue view 181`を
        呼ばないよう、EPIC形の`IssueRecord`を明示的にスタブする。"""
        return IssueRecord(
            number=181,
            title="[EPIC] Test plan",
            body=f"...\n{PARENT_MARKER}",
            labels=(),
            created_at="",
        )

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
        in_progress_issue = _full_issue(
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
            patch("orchestune.dispatch_phase_rebase.ensure_parent_branch"),
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.find_issues_by_parent_metadata",
                return_value=[],
            ),
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.dispatch_targets.subprocess.Popen"),
            _patch_gc_process_alive(return_value=True),
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.find_issues_by_parent_metadata",
                return_value=[],
            ),
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=True),
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
        in_progress_issue = _full_issue(
            1,
            labels=("status:in-progress",),
            subtask_id="task-a",
            footprint=("src/foo.py",),
        )
        other_queued_issue = _full_issue(
            2,
            labels=("status:queued",),
            subtask_id="task-b",
            footprint=("src/bar.py",),
        )

        def _launch_stub(ctx):
            save_run_state(ctx.run_state, ctx.config.run_state_path)
            return ctx.selected

        with (
            patch("orchestune.dispatch_phase_rebase.ensure_parent_branch"),
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.find_issues_by_parent_metadata",
                return_value=[],
            ),
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_phase_scheduling._launch_selected_tasks",
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
        in_progress_issue = _full_issue(
            1,
            labels=("status:in-progress", "status:force-serial"),
            subtask_id="task-a",
            footprint=("src/foo.py",),
        )
        conflicting_issue = _full_issue(
            2,
            labels=("status:queued",),
            subtask_id="task-b",
            footprint=("src/foo.py",),
        )
        dependent_issue = _full_issue(
            3,
            labels=("status:queued",),
            subtask_id="task-c",
            footprint=("src/baz.py",),
            depends_on=("task-a",),
        )
        independent_issue = _full_issue(
            4,
            labels=("status:queued",),
            subtask_id="task-d",
            footprint=("src/qux.py",),
        )

        def _launch_stub(ctx):
            save_run_state(ctx.run_state, ctx.config.run_state_path)
            return ctx.selected

        with (
            patch("orchestune.dispatch_phase_rebase.ensure_parent_branch"),
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.find_issues_by_parent_metadata",
                return_value=[],
            ),
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_phase_scheduling._launch_selected_tasks",
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.dispatch_phase_rebase.ensure_parent_branch"),
            patch("orchestune.forge.GitHubForge.list_sub_issues") as mock_list,
            patch(
                "orchestune.forge.GitHubForge.find_issues_by_parent_metadata",
                return_value=[],
            ),
            patch(
                "orchestune.forge.GitHubForge.get_issue",
                return_value=self._epic_issue(),
            ),
            patch(
                "orchestune.dispatch_phase_rebase.list_remote_branches", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=True),
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
