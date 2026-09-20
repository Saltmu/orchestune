"""#274レビュー対応(P1): PIDの生存確認を、POSIX/Windows双方で非破壊的に行う共有ヘルパー。

`os.kill(pid, 0)`はPOSIXでは「シグナル送信可能か」を確認するだけの非破壊的な
操作だが、Windows上のCPythonは`CTRL_C_EVENT`/`CTRL_BREAK_EVENT`以外のほぼ
全てのシグナル値（`0`を含む）に対して内部的に`TerminateProcess()`を呼び出す。
つまりWindows上では、生存確認のつもりの`os.kill(pid, 0)`が対象プロセスを
実際に終了させてしまう（次のdispatch cycleやmonitorがローカルで実行中の
エージェントセッションをポーリングするだけで、そのセッションを強制終了させる）。

このモジュールはWindows上でのみ、Win32 API（`OpenProcess` +
`GetExitCodeProcess`）による非破壊的な確認へ切り替える。`dispatch_worktree.py`
の`fcntl`/`msvcrt`と同じ「非対応プラットフォームでは`None`のままにしておき、
テストではモックで差し替える」パターンに合わせている。
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import threading
import time
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import IO, Any

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _kernel32 = None  # type: ignore[assignment]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


class _LockContention(Exception):
    """Internal signal for backend-specific lock contention."""


class FileLockContentionError(RuntimeError):
    """Raised when native file lock acquisition times out due to contention."""


class FileLock:
    """Cross-platform exclusive file lock with bounded non-blocking retries."""

    def __init__(
        self,
        lock_path: Path,
        *,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout < 0:
            raise ValueError("timeout must be greater than or equal to zero")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        self.lock_path = lock_path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._lock_fd: IO[str] | None = None
        self._backend: Any | None = None

    def _try_acquire(self, lock_fd: IO[str]) -> None:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            except BlockingIOError:
                raise _LockContention from None
            self._backend = fcntl
            return

        assert msvcrt is not None
        lock_fd.seek(0)
        try:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        except PermissionError:
            raise _LockContention from None
        self._backend = msvcrt

    def acquire(self) -> FileLock:
        """Acquire the lock, retrying contention until the configured timeout."""
        if self._lock_fd is not None:
            raise RuntimeError(f"File lock is already acquired ({self.lock_path})")
        if fcntl is None and msvcrt is None:
            raise RuntimeError(
                "Neither fcntl nor msvcrt is supported on this platform. "
                "File locking is required."
            )

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(self.lock_path, "w" if fcntl is not None else "a+")
        if fcntl is None:
            lock_fd.write(" ")
            lock_fd.flush()
            lock_fd.seek(0)
        deadline = time.monotonic() + self.timeout

        while True:
            try:
                self._try_acquire(lock_fd)
            except _LockContention:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    lock_fd.close()
                    raise FileLockContentionError(
                        "Another instance is already running "
                        f"(locked on {self.lock_path})"
                    ) from None
                time.sleep(min(self.poll_interval, remaining))
                continue
            except Exception:
                lock_fd.close()
                raise

            self._lock_fd = lock_fd
            return self

    def release(self) -> None:
        """Release the native lock and close its descriptor."""
        lock_fd = self._lock_fd
        backend = self._backend
        if lock_fd is None:
            return
        try:
            if backend is fcntl and fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            elif backend is not None:
                lock_fd.seek(0)
                backend.locking(lock_fd.fileno(), backend.LK_UNLCK, 1)
        except Exception:
            pass
        finally:
            lock_fd.close()
            self._lock_fd = None
            self._backend = None

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()


@contextlib.contextmanager
def file_lock(
    lock_path: Path,
    *,
    timeout: float = 0.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Hold an exclusive file lock for the duration of the context."""
    with FileLock(lock_path, timeout=timeout, poll_interval=poll_interval):
        yield


class _RunStateLockState:
    """Track in-process ownership and reentrancy count for a run_state lock path."""

    def __init__(self) -> None:
        self.owner_thread: int | None = None
        self.count: int = 0
        self.file_lock: FileLock | None = None
        self.cond = threading.Condition(threading.Lock())


_RUN_STATE_LOCK_STATES: dict[Path, _RunStateLockState] = {}
_RUN_STATE_REGISTRY_LOCK = threading.Lock()


def _get_run_state_lock_state(norm_path: Path) -> _RunStateLockState:
    """Get or create the lock state record for a canonical lock path."""
    with _RUN_STATE_REGISTRY_LOCK:
        if norm_path not in _RUN_STATE_LOCK_STATES:
            _RUN_STATE_LOCK_STATES[norm_path] = _RunStateLockState()
        return _RUN_STATE_LOCK_STATES[norm_path]


def format_run_state_lock_contention_message(lock_path: Path) -> str:
    """Format a consistent diagnostic message when run_state lock acquisition fails."""
    return (
        f"Could not acquire run_state lock on '{lock_path}': another process is currently holding the lock.\n"
        "A concurrent dispatch cycle or interactive claim session may be in progress.\n"
        "Wait for the active process to finish, or verify that no orphaned process is running, then retry."
    )


def _acquire_run_state_lock_in_process(
    state: _RunStateLockState,
    norm_path: Path,
    current_thread: int,
    deadline: float,
    poll_interval: float,
) -> bool:
    """Acquire the in-process run_state lock slot, returning True if nested."""
    with state.cond:
        while state.owner_thread is not None and state.owner_thread != current_thread:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(format_run_state_lock_contention_message(norm_path))
            state.cond.wait(timeout=min(poll_interval, remaining))

        if state.owner_thread == current_thread:
            state.count += 1
            return True
        state.owner_thread = current_thread
        state.count = 1
        return False


def _release_run_state_lock_in_process(
    state: _RunStateLockState, *, release_outermost: bool
) -> FileLock | None:
    """Release one recursion level of the in-process run_state lock."""
    with state.cond:
        state.count -= 1
        if release_outermost or state.count <= 0:
            state.count = 0
            active_obj = state.file_lock
            state.file_lock = None
            state.owner_thread = None
            state.cond.notify_all()
            return active_obj
        return None


@contextlib.contextmanager
def run_state_lock(
    lock_path: Path,
    *,
    timeout: float = 0.0,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Hold a reentrant exclusive file lock for run_state operations.

    Nested acquisitions on the same path within the same thread increment
    a recursion counter without deadlocking, releasing the underlying
    FileLock only when the outermost context exits. Concurrent calls from
    different threads or processes block or raise on contention.
    """
    norm_path = Path(lock_path).resolve()
    state = _get_run_state_lock_state(norm_path)
    deadline = time.monotonic() + timeout

    is_nested = _acquire_run_state_lock_in_process(
        state, norm_path, threading.get_ident(), deadline, poll_interval
    )
    if is_nested:
        try:
            yield
        finally:
            _release_run_state_lock_in_process(state, release_outermost=False)
        return

    remaining_timeout = max(0.0, deadline - time.monotonic())
    lock_obj = FileLock(
        norm_path, timeout=remaining_timeout, poll_interval=poll_interval
    )
    try:
        lock_obj.acquire()
    except FileLockContentionError as exc:
        _release_run_state_lock_in_process(state, release_outermost=True)
        raise RuntimeError(format_run_state_lock_contention_message(norm_path)) from exc
    except BaseException:
        _release_run_state_lock_in_process(state, release_outermost=True)
        raise

    with state.cond:
        state.file_lock = lock_obj

    try:
        yield
    finally:
        active_obj = _release_run_state_lock_in_process(state, release_outermost=True)
        if active_obj is not None:
            active_obj.release()


def is_run_state_lock_held(lock_path: Path) -> bool:
    """Check if the current thread holds the run_state lock for the given path."""
    norm_path = Path(lock_path).resolve()
    with _RUN_STATE_REGISTRY_LOCK:
        state = _RUN_STATE_LOCK_STATES.get(norm_path)
    if state is None:
        return False
    with state.cond:
        return state.owner_thread == threading.get_ident() and state.count > 0


def assert_run_state_lock_held(lock_path: Path) -> None:
    """Assert that the current thread holds the run_state lock for the given path."""
    if not is_run_state_lock_held(lock_path):
        norm_path = Path(lock_path).resolve()
        raise RuntimeError(f"run_state lock must be held: {norm_path}")


def default_ci_command() -> list[str]:
    """Return the repository-local CI command appropriate for this platform."""
    if sys.platform == "win32":
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/local-ci.ps1",
        ]
    return ["./scripts/local-ci.sh"]


def _is_process_alive_windows(pid: int) -> bool:
    assert _kernel32 is not None
    # PROCESS_QUERY_LIMITED_INFORMATIONは終了コード取得に必要な最小限の
    # 権限のみを要求する非破壊的なアクセス権であり、プロセスの制御
    # （終了・一時停止等）を一切要求しない。
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ハンドル取得自体に失敗した場合、アクセス拒否（プロセスは存在する
        # が権限がない）はPOSIX側のPermissionError→生存扱いと同じ安全側の
        # 判定にする。それ以外（該当PIDが存在しない等）は非生存として扱う。
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        ptr = ctypes.pointer(exit_code)
        if not _kernel32.GetExitCodeProcess(handle, ptr):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def is_process_alive(pid: int | None) -> bool:
    """記録済みpidのプロセス生存確認。

    シグナル送信・プロセスハンドル取得権限がない場合（別ユーザー所有の
    PID再利用等）は、安全側に倒し「生存している」とみなす。
    Windows上では`OpenProcess`+`GetExitCodeProcess`による非破壊的な確認を
    使う（`os.kill(pid, 0)`はWindows上で対象プロセスを実際に終了させて
    しまうため使用しない）。
    """
    if pid is None:
        return False
    if _kernel32 is not None:
        return _is_process_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
