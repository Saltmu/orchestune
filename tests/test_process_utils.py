from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.process_utils import FileLock, file_lock, is_process_alive


class TestFileLock:
    @pytest.mark.uses_real_file_lock
    def test_lock_is_exclusive_and_released_after_context_exit(self, tmp_path: Path):
        lock_path = tmp_path / "shared.lock"

        with file_lock(lock_path):
            with pytest.raises(RuntimeError, match="Another instance"):
                with file_lock(lock_path):
                    pass

        with file_lock(lock_path):
            pass

    def test_retries_contention_until_lock_is_acquired(self, tmp_path: Path):
        mock_fcntl = MagicMock()
        mock_fcntl.LOCK_EX = 1
        mock_fcntl.LOCK_NB = 2
        mock_fcntl.LOCK_UN = 4
        mock_fcntl.flock.side_effect = [BlockingIOError, None, None]

        with (
            patch("orchestune.process_utils.fcntl", mock_fcntl),
            patch("orchestune.process_utils.msvcrt", None),
            patch(
                "orchestune.process_utils.time.monotonic",
                side_effect=[10.0, 10.0],
            ),
            patch("orchestune.process_utils.time.sleep") as mock_sleep,
        ):
            with file_lock(tmp_path / "retry.lock", timeout=1.0, poll_interval=0.2):
                pass

        mock_sleep.assert_called_once_with(0.2)
        assert mock_fcntl.flock.call_count == 3

    def test_timeout_closes_file_and_raises_runtime_error(self, tmp_path: Path):
        mock_fcntl = MagicMock()
        mock_fcntl.LOCK_EX = 1
        mock_fcntl.LOCK_NB = 2
        mock_fcntl.flock.side_effect = BlockingIOError

        with (
            patch("orchestune.process_utils.fcntl", mock_fcntl),
            patch("orchestune.process_utils.msvcrt", None),
            patch(
                "orchestune.process_utils.time.monotonic",
                side_effect=[10.0, 10.0, 10.5],
            ),
            patch("orchestune.process_utils.time.sleep") as mock_sleep,
        ):
            lock = FileLock(tmp_path / "timeout.lock", timeout=0.5, poll_interval=0.2)
            with pytest.raises(RuntimeError, match="Another instance"):
                lock.acquire()

        assert lock._lock_fd is None
        mock_sleep.assert_called_once_with(0.2)

    def test_context_body_exception_is_propagated_and_lock_released(
        self, tmp_path: Path
    ):
        lock_path = tmp_path / "body-error.lock"

        with pytest.raises(ValueError, match="body failed"):
            with file_lock(lock_path):
                raise ValueError("body failed")

        with file_lock(lock_path):
            pass

    def test_msvcrt_backend_locks_and_unlocks_one_byte(self, tmp_path: Path):
        mock_msvcrt = MagicMock()

        with (
            patch("orchestune.process_utils.fcntl", None),
            patch("orchestune.process_utils.msvcrt", mock_msvcrt),
        ):
            with file_lock(tmp_path / "windows.lock"):
                pass

        assert mock_msvcrt.locking.call_count == 2
        assert mock_msvcrt.locking.call_args_list[0].args[1:] == (
            mock_msvcrt.LK_NBLCK,
            1,
        )
        assert mock_msvcrt.locking.call_args_list[1].args[1:] == (
            mock_msvcrt.LK_UNLCK,
            1,
        )

    def test_unsupported_platform_raises_runtime_error(self, tmp_path: Path):
        with (
            patch("orchestune.process_utils.fcntl", None),
            patch("orchestune.process_utils.msvcrt", None),
            pytest.raises(RuntimeError, match="Neither fcntl nor msvcrt is supported"),
        ):
            with file_lock(tmp_path / "unsupported.lock"):
                pass

    @pytest.mark.parametrize(
        ("timeout", "poll_interval"),
        [(-0.1, 0.1), (0.1, 0.0), (0.1, -0.1)],
    )
    def test_invalid_timing_configuration_is_rejected(
        self, tmp_path: Path, timeout: float, poll_interval: float
    ):
        with pytest.raises(ValueError):
            FileLock(
                tmp_path / "invalid.lock",
                timeout=timeout,
                poll_interval=poll_interval,
            )


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
