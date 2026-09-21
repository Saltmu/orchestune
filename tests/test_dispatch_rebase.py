"""dispatch_rebaseの通知処理（notify_recompute/notify_force_serial）の
ユニットテスト。

リベース判定ルール（decide層）は`test_dispatch_rebase_rules.py`、実際の
git rebase実行（apply層）は`test_dispatch_rebase_git.py`、依存関係スタッキング
のエンドツーエンド統合テストは`test_dispatch_rebase_stacking.py`へそれぞれ
分割している（#347、#943）。
"""

from dataclasses import fields
from inspect import signature
from unittest.mock import ANY, MagicMock, patch

from orchestune.dag.models import FootprintConflict
from orchestune.dispatch.rebase import notify_force_serial, notify_recompute


class TestRebaseContext:
    def test_context_carries_auto_rebase_dependencies(self):
        from orchestune.dispatch.rebase import RebaseContext

        assert {field.name for field in fields(RebaseContext)} == {
            "active",
            "active_task",
            "key",
            "run_state",
            "dependencies",
            "config",
        }

    def test_rebase_functions_accept_context_instead_of_many_arguments(self):
        from orchestune.dispatch.rebase import _apply_auto_rebase, _try_auto_rebase

        assert len(signature(_try_auto_rebase).parameters) <= 3
        assert len(signature(_apply_auto_rebase).parameters) <= 3


class TestNotifyRecompute:
    def test_dry_run_reports_without_calling_github(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_label,
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
            patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment,
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_label,
            patch("fake_forge_proxy.active_fake_forge.remove_label"),
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
            patch("fake_forge_proxy.active_fake_forge.add_comment"),
            patch("fake_forge_proxy.active_fake_forge.add_label") as mock_add_label,
            patch(
                "fake_forge_proxy.active_fake_forge.remove_label"
            ) as mock_remove_label,
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
            patch("fake_forge_proxy.active_fake_forge.add_comment"),
            patch(
                "fake_forge_proxy.active_fake_forge.add_label",
                side_effect=lambda issue, label: call_order.append(("add", label)),
            ),
            patch(
                "fake_forge_proxy.active_fake_forge.remove_label",
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
        with patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment:
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
        with patch("fake_forge_proxy.active_fake_forge.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_called_once_with(181, ANY)


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
