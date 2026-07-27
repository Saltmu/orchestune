from unittest.mock import MagicMock, patch

from orchestune.process_utils import is_process_alive


class TestIsProcessAlivePosix:
    """#193/#274: POSIX環境(_kernel32が存在しない)でのpid生存確認。"""

    def test_none_pid_is_not_alive(self):
        assert is_process_alive(None) is False

    def test_alive_pid_returns_true(self):
        with (
            patch("orchestune.process_utils._kernel32", None),
            patch("orchestune.process_utils.os.kill") as mock_kill,
        ):
            mock_kill.return_value = None
            assert is_process_alive(12345) is True

    def test_missing_pid_returns_false(self):
        with (
            patch("orchestune.process_utils._kernel32", None),
            patch("orchestune.process_utils.os.kill", side_effect=ProcessLookupError),
        ):
            assert is_process_alive(12345) is False

    def test_permission_error_is_treated_as_alive(self):
        with (
            patch("orchestune.process_utils._kernel32", None),
            patch("orchestune.process_utils.os.kill", side_effect=PermissionError),
        ):
            assert is_process_alive(1) is True

    def test_other_os_error_returns_false(self):
        with (
            patch("orchestune.process_utils._kernel32", None),
            patch("orchestune.process_utils.os.kill", side_effect=OSError),
        ):
            assert is_process_alive(1) is False


class TestIsProcessAliveWindows:
    """#274レビュー対応(P1): Windows環境(_kernel32あり)では非破壊的な
    OpenProcess+GetExitCodeProcessで確認し、os.killは一切呼ばない。"""

    def _fake_kernel32(self, *, exit_code=259, get_exit_code_succeeds=True):
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 4242

        def fake_get_exit_code(handle, ptr):
            if not get_exit_code_succeeds:
                return 0
            ptr.contents.value = exit_code
            return 1

        mock_kernel32.GetExitCodeProcess.side_effect = fake_get_exit_code
        return mock_kernel32

    def test_reproducer_does_not_call_os_kill_on_windows(self):
        """Reproducer: Windows経路ではos.kill(pid, 0)を絶対に呼ばない
        (呼ぶと対象プロセスがTerminateProcess()されてしまう回帰)。"""
        mock_kernel32 = self._fake_kernel32(exit_code=259)
        with (
            patch("orchestune.process_utils._kernel32", mock_kernel32),
            patch("orchestune.process_utils.os.kill") as mock_kill,
        ):
            assert is_process_alive(12345) is True
            mock_kill.assert_not_called()

    def test_returns_false_when_process_has_exited(self):
        mock_kernel32 = self._fake_kernel32(exit_code=0)
        with patch("orchestune.process_utils._kernel32", mock_kernel32):
            assert is_process_alive(12345) is False

    def test_returns_false_when_open_process_fails_with_unknown_error(self):
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 0
        with (
            patch("orchestune.process_utils._kernel32", mock_kernel32),
            patch(
                "orchestune.process_utils.ctypes.get_last_error",
                return_value=87,
                create=True,
            ),
        ):
            assert is_process_alive(12345) is False

    def test_treats_access_denied_as_alive(self):
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 0
        with (
            patch("orchestune.process_utils._kernel32", mock_kernel32),
            patch(
                "orchestune.process_utils.ctypes.get_last_error",
                return_value=5,
                create=True,
            ),
        ):
            assert is_process_alive(12345) is True

    def test_returns_false_when_get_exit_code_process_fails(self):
        mock_kernel32 = self._fake_kernel32(get_exit_code_succeeds=False)
        with patch("orchestune.process_utils._kernel32", mock_kernel32):
            assert is_process_alive(12345) is False

    def test_closes_handle_after_use(self):
        mock_kernel32 = self._fake_kernel32(exit_code=259)
        with patch("orchestune.process_utils._kernel32", mock_kernel32):
            is_process_alive(12345)
        mock_kernel32.CloseHandle.assert_called_once_with(4242)
