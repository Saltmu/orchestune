"""JSONベースの状態ファイル永続化の共通ヘルパー。

`run_state.json` / `not_needed_review_state.json` など、複数の状態ファイルが
共通して必要とする「atomic write」と「破損ファイルからの復旧」をここに集約する。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | Path, data: Any) -> None:
    """同一ディレクトリに一時ファイルを書き、flush/fsync後にos.replace()でatomic renameする。

    プロセス停止・ディスク枯渇・同時書き込みが途中で発生しても、読み取り側からは
    書き込み前の完全な旧内容か書き込み後の完全な新内容のいずれかしか観測されない
    （os.replace()は同一ファイルシステム上でアトミック）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def read_json_with_recovery(path: str | Path, *, label: str) -> Any | None:
    """状態ファイルを読み込む。存在しなければNoneを返す。

    JSONのdecodeに失敗した場合（部分書き込みや破損）は、診断用に破損ファイルを
    別名へ退避（quarantine）した上でNoneを返す。呼び出し側はNoneをファイル未存在時
    と同様にデフォルト状態へのフォールバックとして扱うことで、既存の
    self-healing（GitHubからのreconciliation）に接続できる。
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        quarantine_path = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        try:
            os.replace(path, quarantine_path)
            print(
                f"Warning: {label} at '{path}' is corrupted ({exc}). "
                f"Quarantined to '{quarantine_path}' for diagnostics; "
                "falling back to default state.",
                file=sys.stderr,
            )
        except OSError as move_exc:
            print(
                f"Warning: {label} at '{path}' is corrupted ({exc}) and could not "
                f"be quarantined ({move_exc}); falling back to default state.",
                file=sys.stderr,
            )
        return None
