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

import ctypes
import os
import sys
from ctypes import wintypes

if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:
    _kernel32 = None  # type: ignore[assignment]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


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
