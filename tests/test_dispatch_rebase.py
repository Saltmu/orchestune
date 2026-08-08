"""dispatch_rebaseの通知処理（notify_recompute/notify_force_serial）、
およびブランチスタッキング・自動リベースのエンドツーエンド統合テスト。

リベース判定ルール（decide層）は`test_dispatch_rebase_rules.py`、実際の
git rebase実行（apply層）は`test_dispatch_rebase_git.py`へそれぞれ分割
している（#347）。
"""

import subprocess
from contextlib import ExitStack, contextmanager
from unittest.mock import ANY, MagicMock, patch

from orchestune.dag_models import FootprintConflict
from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_cycle import run_dispatch_cycle
from orchestune.dispatch_rebase import notify_force_serial, notify_recompute
from orchestune.dispatch_state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.models import PrRecord
from tests.conftest import make_issue


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
    """`tests/conftest.py`の`make_issue`に、このファイルの旧テスト群が前提と
    する`parent_number`（既定181）とtitleを合わせた薄いラッパー。"""
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


class TestNotifyRecompute:
    def test_dry_run_reports_without_calling_github(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
            patch("orchestune.forge.GitHubForge.add_label") as mock_label,
        ):
            bodies = notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=False,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        mock_comment.assert_not_called()
        mock_label.assert_not_called()
        assert len(bodies) >= 2

    def test_apply_posts_comments_and_labels_blocked_subtask(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.add_comment") as mock_comment,
            patch("orchestune.forge.GitHubForge.add_label") as mock_label,
            patch("orchestune.forge.GitHubForge.remove_label"),
        ):
            notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=True,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        assert mock_comment.call_count >= 3  # task-a issue, task-b issue, parent issue
        mock_label.assert_any_call(2, "status:blocked-recompute")

    def test_apply_removes_queued_and_adds_blocked_labels(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
        ):
            notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=True,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        mock_remove_label.assert_any_call(2, "status:queued")
        mock_add_label.assert_any_call(2, "status:blocked")
        mock_add_label.assert_any_call(2, "status:blocked-recompute")

    def test_adds_blocked_labels_before_removing_queued(self):
        # #381: 途中でクラッシュしてもIssueが必ずいずれかのstatus:*ラベルを
        # 持ち続けるよう、addがremoveより先に呼ばれなければならない。
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        call_order: list[tuple[str, str]] = []
        with (
            patch("orchestune.forge.GitHubForge.add_comment"),
            patch(
                "orchestune.forge.GitHubForge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "orchestune.forge.GitHubForge.remove_label",
                side_effect=lambda issue, label: call_order.append(("remove", label)),
            ),
        ):
            notify_recompute(
                conflict,
                "作業内容の要約",
                parent_issue_number=181,
                apply=True,
                issue_number_by_subtask_id={"task-a": 1, "task-b": 2},
            )
        assert call_order == [
            ("add", "status:blocked"),
            ("remove", "status:queued"),
            ("add", "status:blocked-recompute"),
        ]


class TestNotifyForceSerial:
    """#200: リトライ上限超過時の強制直列化フォールバック通知。"""

    def test_dry_run_does_not_call_github(self):
        with patch("orchestune.forge.GitHubForge.add_comment") as mock_comment:
            body = notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=False,
            )
        mock_comment.assert_not_called()
        assert "task-a" in body

    def test_apply_posts_comment_to_parent_issue(self):
        with patch("orchestune.forge.GitHubForge.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_called_once_with(181, ANY)

    def test_apply_without_parent_issue_skips_comment(self):
        with patch("orchestune.forge.GitHubForge.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=None,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_not_called()


class TestNotifyForceSerialWithFakeForge:
    """#293: `mock.patch`によるグローバルなクラスメソッド差し替えではなく、
    `forge`引数への注入だけでテストが書けることを示す。"""

    def test_uses_injected_fake_forge_instead_of_patching(self):
        fake_forge = MagicMock()

        notify_force_serial(
            "task-a",
            issue_number=1,
            parent_issue_number=181,
            retry_count=2,
            apply=True,
            forge=fake_forge,
        )

        fake_forge.add_comment.assert_called_once()
        assert fake_forge.add_comment.call_args.args[0] == 181


class TestBranchStacking:
    def test_stacking_blocked_task_when_dependency_pr_ci_passes(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            _patch_gc_process_alive(return_value=True),
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
                dispatch_started_at=1_700_000_000.0,
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
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            _patch_gc_process_alive(return_value=True),
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
                dispatch_started_at=1_700_000_000.0,
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
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
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
        # rebase実行の引数チェック（#213: rebase前にWIP退避チェックのgit statusが
        # 挟まるため、"rebase"を含む呼び出しを探して検証する）
        rebase_call = next(c for c in mock_run.call_args_list if "rebase" in c.args[0])
        assert "claude/issue-1-task-1" in rebase_call.args[0]

        # 新しいPIDで状態が保存されていることを確認
        loaded = load_run_state(config.run_state_path)
        assert loaded.active_worktrees["2"].pid == 99999

    def test_stacking_blocked_when_multiple_dependencies_unmerged(self, tmp_path):
        config = DispatcherConfig(
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=[
                    "origin/claude/issue-1-task-1",
                    "origin/claude/issue-2-task-2",
                ],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            patch("orchestune.forge.GitHubForge.add_label"),
            patch("orchestune.forge.GitHubForge.remove_label"),
            _patch_gc_process_alive(return_value=True),
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
            events_log_path=tmp_path / "events.jsonl",
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
            patch("orchestune.forge.GitHubForge.list_issues_by_label") as mock_list,
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=[
                    "origin/claude/issue-1-task-1",
                    "origin/claude/issue-2-task-2",
                ],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            # #292: このシナリオのラベル遷移はdispatch_cycleの
            # _promote_blocked_tasks（Forge注入経由）が行うため、
            # dispatch_rebase.github経由ではなくGitHubForge側をパッチする。
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch(
                "orchestune.dispatch_launch.create_worktree_and_launch"
            ) as mock_launch,
            # タスクAの完了判定とGC処理のためのモック
            patch("orchestune.dispatch_gc._is_worktree_complete", return_value=True),
            # Completion now also consults the all-state PR list to rule out
            # an abandoned (closed-unmerged) PR before finalizing.
            patch("orchestune.forge.GitHubForge.list_prs", return_value=[]),
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
                dispatch_started_at=1_700_000_000.0,
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
            events_log_path=tmp_path / "events.jsonl",
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
                "orchestune.forge.GitHubForge.list_issues_by_label",
                side_effect=lambda label, **_: (
                    [issue_a, issue_b] if label == "status:in-progress" else []
                ),
            ),
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
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
            # 2. git status --porcelain (#213: rebase前のWIP退避チェック) -> clean
            # 3. git rebase -> 戻り値 128 (競合発生で失敗)
            # 4. git rebase --abort -> 戻り値 0
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
                subprocess.CalledProcessError(returncode=128, cmd="git rebase"),
                subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ]

            run_dispatch_cycle(config)

        # rebase abort が呼ばれたこと
        abort_call = mock_run.call_args_list[3]
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
            events_log_path=tmp_path / "events.jsonl",
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
                "orchestune.forge.GitHubForge.list_issues_by_label",
                side_effect=lambda label, **_: (
                    [issue_a, issue_b] if label == "status:in-progress" else []
                ),
            ),
            patch(
                "orchestune.dispatch_cycle.list_remote_branches",
                return_value=["origin/claude/issue-1-task-1"],
            ),
            patch(
                "orchestune.forge.GitHubForge.list_open_prs",
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
            _patch_gc_process_alive(return_value=True),
            patch(
                "orchestune.dispatch_rebase.check_footprint_deviation", return_value=[]
            ),
            # #292: CHANGES_REQUESTEDエスカレーションはdispatch_escalationの
            # apply_human_review_escalationがForge注入経由で呼ぶため、
            # dispatch_rebase.github経由ではなくGitHubForge側をパッチする。
            patch("orchestune.forge.GitHubForge.add_label") as mock_add_label,
            patch("orchestune.forge.GitHubForge.remove_label") as mock_remove_label,
            patch("orchestune.forge.GitHubForge.add_comment") as mock_add_comment,
            patch("orchestune.dispatch_rebase.os.kill") as mock_kill,
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
