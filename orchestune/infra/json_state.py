"""JSONベースの状態ファイル永続化の共通ヘルパー。

`run_state.json` / `not_needed_review_state.json` など、複数の状態ファイルが
共通して必要とする「atomic write」と「破損ファイルからの復旧」をここに集約する。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | Path, data: Any) -> None:
    """同一ディレクトリに一時ファイルを書き、flush/fsync後にos.replace()でatomic renameする。

    プロセス停止・ディスク枯渇・同時書き込みが途中で発生しても、読み取り側からは
    書き込み前の完全な旧内容か書き込み後の完全な新内容のいずれかしか観測されない
    （os.replace()は同一ファイルシステム上でアトミック）。

    一時ファイル名は`tempfile.mkstemp`（内部でO_EXCLを使用）により、同一ディレクトリ
    内で毎回一意なものを生成する。PIDのみに基づく命名だと、同一プロセス内で同じ
    パスへ並行書き込みが発生した場合に一時ファイルが衝突し、一方の`os.replace()`後に
    他方が`FileNotFoundError`になったり内容が競合したりし得るため。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.tmp.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt == max_attempts - 1:
                    raise
                time.sleep(0.01 * (attempt + 1))
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

    quarantine対象は`JSONDecodeError`/`UnicodeDecodeError`（＝ファイル内容そのものが
    破損している場合）に限定する。一時的なI/O障害や権限エラーなどの`OSError`は
    ファイル内容の破損を意味しないため、ここでは捕捉せずそのまま呼び出し元へ伝播
    させる（正常な状態ファイルを誤って空状態として扱い、後続の保存で状態を失う
    ことを防ぐため）。同様の理由で、quarantineへの退避自体（`os.replace`）が失敗した
    場合もNoneへフォールバックせず例外を伝播させる。
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantine_path = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
        os.replace(path, quarantine_path)
        print(
            f"Warning: {label} at '{path}' is corrupted ({exc}). "
            f"Quarantined to '{quarantine_path}' for diagnostics; "
            "falling back to default state.",
            file=sys.stderr,
        )
        return None
