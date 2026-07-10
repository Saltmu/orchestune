from unittest.mock import ANY, call, patch

from orchestune.dag import FootprintConflict
from orchestune.dispatch_rebase import notify_force_serial, notify_recompute


class TestNotifyRecompute:
    def test_dry_run_reports_without_calling_github(self):
        conflict = FootprintConflict(
            subtask_id="task-a",
            other_subtask_id="task-b",
            similarity=0.5,
            blocked_subtask_id="task-b",
        )
        with (
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_label,
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
            patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment,
            patch("orchestune.dispatch_rebase.github.add_label") as mock_label,
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


class TestNotifyForceSerial:
    """#200: リトライ上限超過時の強制直列化フォールバック通知。"""

    def test_dry_run_does_not_call_github(self):
        with patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment:
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
        with patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=181,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_called_once_with(181, ANY)

    def test_apply_without_parent_issue_skips_comment(self):
        with patch("orchestune.dispatch_rebase.github.add_comment") as mock_comment:
            notify_force_serial(
                "task-a",
                issue_number=1,
                parent_issue_number=None,
                retry_count=2,
                apply=True,
            )
        mock_comment.assert_not_called()


class TestWaitForProcessTerminate:
    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_immediate_exit(self, mock_sleep, mock_kill):
        # 最初の os.kill(pid, 0) で ProcessLookupError が発生すれば即座に終了する
        mock_kill.side_effect = ProcessLookupError()

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        mock_kill.assert_called_once_with(12345, 0)
        mock_sleep.assert_not_called()

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_exit_after_polling(self, mock_sleep, mock_kill):
        # 1, 2回目は存在（例外なし）、3回目に ProcessLookupError で終了
        mock_kill.side_effect = [None, None, ProcessLookupError()]

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        assert mock_kill.call_count == 3
        mock_kill.assert_has_calls([call(12345, 0), call(12345, 0), call(12345, 0)])
        assert mock_sleep.call_count == 2
        mock_sleep.assert_has_calls([call(0.1), call(0.1)])

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_timeout(self, mock_sleep, mock_kill):
        # ずっとプロセスが存在している場合、タイムアウト時間経過で抜ける
        mock_kill.return_value = None

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        with patch("orchestune.dispatch_rebase.time.time") as mock_time:
            # startの取得時で 0.0、その後のループ条件評価で 0.0, 0.05, 0.11
            mock_time.side_effect = [0.0, 0.0, 0.05, 0.11]
            _wait_for_process_terminate(12345, timeout=0.1)

        assert mock_kill.call_count >= 1

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_permission_error_then_exit(self, mock_sleep, mock_kill):
        # 1回目は PermissionError (プロセスは存在している)
        # 2回目に ProcessLookupError で終了
        mock_kill.side_effect = [PermissionError(), ProcessLookupError()]

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        assert mock_kill.call_count == 2
        mock_sleep.assert_called_once_with(0.1)

    @patch("orchestune.dispatch_rebase.os.kill")
    @patch("orchestune.dispatch_rebase.time.sleep")
    def test_wait_os_error(self, mock_sleep, mock_kill):
        # 1回目に一般的な OSError が発生した場合は即座に終了
        mock_kill.side_effect = OSError()

        from orchestune.dispatch_rebase import _wait_for_process_terminate

        _wait_for_process_terminate(12345, timeout=1.0)

        mock_kill.assert_called_once_with(12345, 0)
        mock_sleep.assert_not_called()
