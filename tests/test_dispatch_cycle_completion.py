"""dispatch_cycleのタスク完了・not-needed処理テスト。

`tests/test_dispatch_cycle.py`の肥大化解消のため、プロセス終了検知→worktree
削除→クオータ解放→status:doneラベル遷移（#193）と、status:not-needed
ラベル検知による完全自動クローズ（#280）を分割している（#343）。
"""

import json
import subprocess
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import (
    run_dispatch_cycle,
)
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    save_run_state,
)
from tests.conftest import make_issue


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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
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
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ) as mock_local_commits,
            patch(
                "orchestune.dispatch_gc_completion.remote_branch_commit_sha_if_ahead",
                return_value="remote-commit",
            ) as mock_remote_commits,
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remote_branch_commit_sha_if_ahead",
                return_value=None,
            ),
            patch(
                "orchestune.dispatch_gc_completion.apply_human_review_escalation"
            ) as mock_escalate,
            patch("orchestune.dispatch_gc_completion.remove_worktree"),
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            # Completion now also consults the all-state PR list to rule out an
            # abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=True,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
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
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
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
            # abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            _patch_gc_process_alive(return_value=False),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_new_commits",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
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
        in_progress_issue = _full_issue(
            1, labels=("status:in-progress",), subtask_id="task-a"
        )
        queued_issue = _full_issue(2, footprint=("src/bar.py",), subtask_id="task-b")
        with (
            patch("orchestune.dispatch_worktree._branch_exists", return_value=False),
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
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
        not_needed_issue = _full_issue(
            1, labels=("status:not-needed",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
            # プロセスは生きたまま・PRも存在しない、という「対応不要」の典型状態
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
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
        not_needed_issue = _full_issue(
            1, labels=("status:not-needed",), subtask_id="task-a"
        )
        with (
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch("orchestune.dispatch_cycle.list_remote_branches", return_value=[]),
            patch("orchestune.forge.GitHubForge.list_open_prs", return_value=[]),
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.close_issue") as mock_close_issue,
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_gc_completion.worktree_has_uncommitted_changes",
                return_value=False,
            ),
            patch(
                "orchestune.dispatch_gc_completion.remove_worktree"
            ) as mock_remove_worktree,
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
        not_needed_issue = _full_issue(
            1, labels=("status:not-needed",), subtask_id="task-a"
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
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
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
