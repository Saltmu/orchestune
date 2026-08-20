"""ローカルgit実行の単一の実行口。

`git`の`subprocess`呼び出しをこのモジュールに集約し、`text=True`・`encoding="utf-8"`・
`errors="replace"`統一・`cwd=`パラメータ統一（`-C <path>`引数との混在を解消）・一貫した
エラーハンドリング（`CalledProcessError`/`OSError`は握り潰さずそのまま
伝播させ、握り潰す/文字列化する判断は呼び出し側に委ねる）を提供する。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# git CLIのadapter境界でrefを検証し、不正な値をsubprocessへ渡さない。
_REF_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")

# Git hooksや外部環境からリークして意図しないリポジトリを操作する危険な環境変数一覧 (Issue #507)
DANGEROUS_GIT_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
    "GIT_GRAFT_FILE",
    "GIT_SUPER_PREFIX",
)


def get_clean_git_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of os.environ with all dangerous GIT_* environment variables removed."""
    env = {k: v for k, v in os.environ.items() if k not in DANGEROUS_GIT_ENV_VARS}
    if extra_env:
        env.update(extra_env)
    return env


def _validate_ref_name(ref: str) -> str:
    if (
        not ref
        or not _REF_NAME_PATTERN.match(ref)
        or ref.startswith("-")
        or ".." in ref
    ):
        raise ValueError(f"ブランチ名が不正です: {ref!r}")
    return ref


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def run_git(
    args: list[str],
    *,
    cwd: str | Path | None,
    check: bool = True,
    env: dict[str, str] | None = None,
    isolate_git_env: bool = True,
) -> GitResult:
    """`git`を呼び出す単一の実行口。

    `check=True`（既定）の場合、非ゼロ終了は`subprocess.CalledProcessError`
    としてそのまま伝播する。実行自体ができなかった場合は`OSError`が伝播する。
    握り潰す/文字列化するかどうかの判断は呼び出し側の責務とする。

    #531: 出力デコードには`encoding="utf-8"`および`errors="replace"`を使用する。
    リポジトリ内の不正なバイト列を含むファイル名等があってもプロセスを落とさず、
    置換文字(`\\ufffd`)へ縮退させて安全に処理を継続する。
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": check,
    }

    if env is not None:
        if isolate_git_env and any(var in env for var in DANGEROUS_GIT_ENV_VARS):
            kwargs["env"] = {
                k: v for k, v in env.items() if k not in DANGEROUS_GIT_ENV_VARS
            }
        else:
            kwargs["env"] = env
    elif isolate_git_env and any(var in os.environ for var in DANGEROUS_GIT_ENV_VARS):
        kwargs["env"] = get_clean_git_env()

    result = subprocess.run(["git", *args], **kwargs)
    return GitResult(
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
    )


def list_remote_branches() -> list[str]:
    """#183: 単一ブランチcheckout等でローカルにremote-tracking refが存在しない
    ブランチも見落とさないよう、列挙前にoriginの全ブランチ参照を明示refspecで
    fetchして同期する。コマンドラインで渡すrefspecは`remote.origin.fetch`の
    制限（単一ブランチ設定等）より優先されるため、これで確実に同期できる。
    fetchが失敗した場合は警告を出し、それまでのローカル参照のみで続行する。"""
    try:
        run_git(
            ["fetch", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"],
            cwd=None,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        print(
            f"Warning: failed to fetch remote branches before listing: {detail}",
            file=sys.stderr,
        )
    result = run_git(["branch", "-r", "--format=%(refname:short)"], cwd=None)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def branch_changed_files(branch: str, base: str = "origin/main") -> list[str] | None:
    """`base`に対する`branch`の変更ファイル一覧を返す。

    #245: 「変更なし」と「差分取得不能」を戻り値の型で区別する。差分を取得
    できなかった場合は`None`を返し、呼び出し側（外部ロック同期）がfail closed
    （既存lock維持・新規taskを競合なしと判定しない）に振る舞えるようにする。
    失敗タイプ別の方針:

    - 不正なref名(ValueError): `[]`。バリデーション上diff自体を永遠に実行
      できず、`None`を返すとそのブランチが存在する限りディスパッチが恒久的に
      凍結されるため、従来通りロック対象外として扱う。
    - no merge base(#232): `[]`。`base`と共通の祖先を持たない(orphanな)
      ブランチとの3点diffは`fatal: no merge base`でexit 128になるが、
      共有履歴がなくfootprint比較対象として意味を持たないため差分なしと扱う。
    - その他のgit失敗(ref欠落・repository異常等のCalledProcessError): `None`。
    - git実行障害(OSError): `None`。
    """
    try:
        _validate_ref_name(branch)
        _validate_ref_name(base)
        result = run_git(["diff", "--name-only", f"{base}...{branch}"], cwd=None)
    except ValueError as exc:
        print(
            f"Warning: invalid ref name for branch_changed_files: {exc}",
            file=sys.stderr,
        )
        return []
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else str(exc)
        print(
            f"Warning: failed to diff changed files for {branch!r} against "
            f"{base!r}: {detail}",
            file=sys.stderr,
        )
        if "no merge base" in detail:
            return []
        return None
    except OSError as exc:
        print(
            f"Warning: unable to inspect changed files for {branch!r} against "
            f"{base!r}: {exc}",
            file=sys.stderr,
        )
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def ensure_parent_branch(parent_issue_number: int) -> None:
    parent_branch = f"parent/issue-{parent_issue_number}"
    try:
        result = run_git(
            ["ls-remote", "origin", f"refs/heads/{parent_branch}"], cwd=None
        )
        remote_exists = bool(result.stdout.strip())
    except Exception:
        remote_exists = False

    if remote_exists:
        try:
            # すでにリモートに親ブランチが存在する場合、そのリモート追跡ブランチ参照を確実にローカルにフェッチする
            run_git(
                [
                    "fetch",
                    "origin",
                    f"+refs/heads/{parent_branch}:refs/remotes/origin/{parent_branch}",
                ],
                cwd=None,
            )
        except Exception as e:
            print(
                f"Warning: Failed to fetch existing parent branch '{parent_branch}': {e}",
                file=sys.stderr,
            )
        return

    print(f"Creating parent branch '{parent_branch}' from main...", file=sys.stderr)
    try:
        # 競合やローカル変更を避けるため、checkoutを行わずにリモートmainの最新状態をfetchし、
        # FETCH_HEADを指定して直接リモートに親ブランチをプッシュして作成する。
        run_git(["fetch", "origin", "main"], cwd=None)
        run_git(
            ["push", "origin", f"FETCH_HEAD:refs/heads/{parent_branch}"],
            cwd=None,
        )
        # プッシュ完了後、リモート追跡ブランチ参照を確実にローカルにフェッチする
        run_git(
            [
                "fetch",
                "origin",
                f"+refs/heads/{parent_branch}:refs/remotes/origin/{parent_branch}",
            ],
            cwd=None,
        )
    except Exception as e:
        print(
            f"Warning: Failed to auto-create parent branch '{parent_branch}': {e}",
            file=sys.stderr,
        )


def resolve_local_or_remote_branch(
    worktree_path: str | Path, branch: str, *, prefer_remote: bool = False
) -> str:
    """指定されたブランチ名を解決して返す。
    prefer_remote=True の場合はリモート追跡ブランチを最優先し、
    False の場合はローカルブランチを最優先する。"""
    _validate_ref_name(branch)
    worktree_path = Path(worktree_path)

    def check_remote() -> str | None:
        try:
            result = run_git(
                ["show-ref", "--verify", f"refs/remotes/origin/{branch}"],
                cwd=worktree_path,
                check=False,
            )
        except OSError:
            # worktree_pathが存在しない等でgitを起動できなかった場合も、
            # 「参照が見つからなかった」と同様にNoneへフォールバックする
            # （元の`git -C <path>`実装では、この失敗はgit自身の非ゼロ終了として
            # 現れ例外にならなかったため、同じ振る舞いを維持する）。
            return None
        if result.returncode == 0:
            return f"origin/{branch}"
        return None

    def check_local() -> str | None:
        try:
            result = run_git(
                ["show-ref", "--verify", f"refs/heads/{branch}"],
                cwd=worktree_path,
                check=False,
            )
        except OSError:
            return None
        if result.returncode == 0:
            return branch
        return None

    if prefer_remote:
        resolved = check_remote() or check_local()
    else:
        resolved = check_local() or check_remote()

    return resolved or branch


def fetch_remote_branch(repository_root: str | Path, branch: str) -> str:
    """指定ブランチのリモート追跡参照を最新化して、その参照名を返す。"""
    _validate_ref_name(branch)
    run_git(
        ["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
        cwd=repository_root,
    )
    return f"origin/{branch}"


def normalize_remote_branch_name(branch: str) -> str:
    """`origin/` 接頭辞の有無を吸収し、リモートのブランチ名を返す。"""
    if branch.startswith("origin/"):
        branch = branch.removeprefix("origin/")
    return _validate_ref_name(branch)
