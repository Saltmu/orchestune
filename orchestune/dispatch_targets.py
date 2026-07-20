"""#181/#215: タスクの実ディスパッチ先を切り替え可能にするStrategyクラス群。

`DispatchTarget`を実装するクラスを差し替えるだけで、ディスパッチャーが
「何に対してタスクを実行させるか」（ローカルsubprocess・Claude Codeクラウドルーチン等）
を変更できる。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from orchestune import github
from orchestune.github import PrRecord

if TYPE_CHECKING:
    from orchestune.dispatcher import Task

ROUTINE_ID_ENV_VAR = "ORCHESTUNE_ROUTINE_ID"
ROUTINE_TOKEN_ENV_VAR = "ORCHESTUNE_ROUTINE_TOKEN"
CODEX_CLOUD_ENV_VAR = "ORCHESTUNE_CODEX_CLOUD_ENV"

NONINTERACTIVE_DISPATCH_INSTRUCTION = (
    "これは非対話型のバックグラウンド自動実行であり、標準入力からの応答は得られません。"
    "planning_modeによるユーザー承認待ちで停止せず、"
    "実装プラン作成後は直ちに実装・検証・コミットまで完了させてください。"
)

CLAUDE_CLI_LOCAL_CMD_TEMPLATE = (
    'claude -p "GitHub Issue #{issue_number} を、'
    "必ず作業ブランチ `{branch_name}` で、"
    "標準開発ワークフローに従って実装してください。"
    f'{NONINTERACTIVE_DISPATCH_INSTRUCTION}" '
    "--permission-mode bypassPermissions"
)

AGY_CLI_LOCAL_CMD_TEMPLATE = (
    'agy -p "GitHub Issue #{issue_number} を、'
    "必ず作業ブランチ `{branch_name}` で、"
    "標準開発ワークフローに従って実装してください。"
    f'{NONINTERACTIVE_DISPATCH_INSTRUCTION}" '
    "--sandbox --dangerously-skip-permissions"
)

CODEX_CLI_LOCAL_CMD_TEMPLATE = (
    'codex exec "GitHub Issue #{issue_number} を、'
    "必ず作業ブランチ `{branch_name}` で、"
    "標準開発ワークフローに従って実装してください。"
    f'{NONINTERACTIVE_DISPATCH_INSTRUCTION}" '
    "--dangerously-bypass-approvals-and-sandbox"
)

LOCAL_CLI_CANDIDATES: tuple[str, ...] = ("claude", "agy", "codex")


def detect_installed_local_cli() -> str | None:
    """PATH上にインストールされているローカルCLIを検出する（`auto`モード用）。

    `claude`を優先し、無ければ`agy`、それも無ければ`codex`にフォールバックする。
    いずれも見つからない場合は`None`を返す。
    """
    for candidate in LOCAL_CLI_CANDIDATES:
        if shutil.which(candidate) is not None:
            return candidate
    return None


def resolve_default_dispatch_target_name(env: Mapping[str, str]) -> str:
    """`--dispatch-target`未指定時、実行環境から実ディスパッチ先を自動選択する。

    GitHub Actions実行環境（`GITHUB_ACTIONS=true`）ではクラウドルーチンへ、
    それ以外（ローカル/対話実行）では`auto`（PATH上のローカルCLI自動検出。
    `claude`優先、次点`agy`、`codex`）へディスパッチする。
    CLI未検出時・資格情報未設定時のフォールバックは`build_dispatch_target`側の
    既存ロジックに委ねる。
    """
    if env.get("GITHUB_ACTIONS") == "true":
        return "cloud-routine"
    return "auto"


@dataclass(frozen=True)
class DispatchHandle:
    """起動したエージェント実行を後から追跡するための不透明なハンドル。"""

    pid: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    branch_name: str | None = None
    issue_number: int | None = None


class DispatchTarget(ABC):
    """タスクを実際にどこへディスパッチするかを表す戦略インターフェース。"""

    @abstractmethod
    def launch(
        self, task: Task, branch_name: str, worktree_path: Path
    ) -> DispatchHandle:
        """タスクに対応するエージェントを起動し、追跡用ハンドルを返す。"""

    def is_complete(self, handle: DispatchHandle) -> bool:
        """`launch`で起動した実行が完了しているかどうかを判定する。"""
        return self.completion_status(handle) == "completed"

    def completion_status(
        self, handle: DispatchHandle
    ) -> Literal["pending", "completed", "abandoned"]:
        """Return a lifecycle status; local targets only expose pending/completed."""
        return "completed" if self.is_complete(handle) else "pending"


def classify_task_pr_completion_status(
    handle: DispatchHandle, prs: list[PrRecord]
) -> Literal["pending", "completed", "abandoned"]:
    """Classify matching task PRs without treating rejected PRs as done."""
    if handle.branch_name is None and handle.issue_number is None:
        return "pending"
    matching_prs = [
        pr
        for pr in prs
        if (handle.branch_name is not None and pr.head_ref == handle.branch_name)
        or (
            handle.issue_number is not None
            and handle.issue_number in pr.closes_issue_numbers
        )
    ]
    states = {pr.state.upper() for pr in matching_prs}
    if states & {"OPEN", "MERGED"}:
        return "completed"
    if "CLOSED" in states:
        return "abandoned"
    return "pending"


def _task_pr_completion_status(
    handle: DispatchHandle,
) -> Literal["pending", "completed", "abandoned"]:
    if handle.branch_name is None and handle.issue_number is None:
        return "pending"
    return classify_task_pr_completion_status(handle, github.list_prs(state="all"))


def default_dry_run_command_builder(task: Task, worktree_path: Path) -> list[str]:
    return ["true"]


def _is_pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class LocalProcessDispatchTarget(DispatchTarget):
    """ローカルの`git worktree`上でsubprocessを直接起動する（既定・後方互換の実装）。"""

    def __init__(
        self,
        command_builder: Callable[
            [Task, Path], list[str]
        ] = default_dry_run_command_builder,
        log_dir: str | Path = Path("logs"),
        local_cmd: str | None = None,
    ):
        self._command_builder = command_builder
        self._log_dir = Path(log_dir)
        self._local_cmd = local_cmd

    def launch(
        self, task: Task, branch_name: str, worktree_path: Path
    ) -> DispatchHandle:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        if self._local_cmd:
            formatted_cmd = self._local_cmd.format(
                issue_number=task.issue_number,
                subtask_id=task.subtask_id or "",
                branch_name=branch_name,
                worktree_path=str(worktree_path),
            )
            cmd = shlex.split(formatted_cmd)
        else:
            cmd = self._command_builder(task, worktree_path)

        slug = branch_name.replace("/", "-")
        log_path = self._log_dir / f"{slug}.log"
        with open(log_path, "ab") as log_fh:
            process = subprocess.Popen(
                cmd,
                cwd=str(worktree_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return DispatchHandle(pid=process.pid, branch_name=branch_name)

    def is_complete(self, handle: DispatchHandle) -> bool:
        return not _is_pid_alive(handle.pid)


class ClaudeCodeCloudRoutineDispatchTarget(DispatchTarget):
    """#181/#215: Claude Codeクラウドルーチンのfire APIへ実ディスパッチする。

    事前に https://claude.ai/code/routines でAPIトリガー付きルーチンを作成し、
    その`routine_id`と発行済みトークンを渡す必要がある
    （参考: https://code.claude.com/docs/en/routines.md ）。
    セッションの完了状態を問い合わせるポーリングAPIは現時点で公開されていないため、
    `is_complete`は対象ブランチにオープンなPRが立ったことをプロキシシグナルとして使う。
    """

    API_BASE = "https://api.anthropic.com/v1/claude_code/routines"
    BETA_HEADER = "experimental-cc-routine-2026-04-01"
    ANTHROPIC_VERSION = "2023-06-01"
    _RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        routine_id: str,
        routine_token: str,
        max_retries: int = 3,
        initial_delay: float = 1.0,
    ):
        self._routine_id = routine_id
        self._routine_token = routine_token
        self._max_retries = max_retries
        self._initial_delay = initial_delay

    def _build_text(self, task: Task, branch_name: str) -> str:
        footprint = ", ".join(task.footprint) if task.footprint else "(未指定)"
        return (
            f"GitHub Issue #{task.issue_number}"
            f"（サブタスク: {task.subtask_id or '不明'}）を"
            "標準開発ワークフローに従って実装してください。\n"
            f"作業ブランチ名は必ず `{branch_name}` としてください。\n"
            f"想定footprint: {footprint}\n"
            f"{NONINTERACTIVE_DISPATCH_INSTRUCTION}\n"
        )

    def _fire(self, text: str) -> dict[str, Any]:
        """任意のテキスト指示でルーチンをfireし、生のレスポンスペイロードを返す。"""
        body = json.dumps({"text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.API_BASE}/{self._routine_id}/fire",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._routine_token}",
                "anthropic-beta": self.BETA_HEADER,
                "anthropic-version": self.ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
        )
        return self._fire_with_retry(request)

    def launch(
        self, task: Task, branch_name: str, worktree_path: Path
    ) -> DispatchHandle:
        payload = self._fire(self._build_text(task, branch_name))
        return DispatchHandle(
            external_id=payload.get("claude_code_session_id"),
            external_url=payload.get("claude_code_session_url"),
            branch_name=branch_name,
        )

    def fire_text(self, text: str) -> DispatchHandle:
        """#186: タスク以外の任意指示（統合コーディネーターの意味的レビュー等）を
        dispatcherと同一のルーチンへ投げるための汎用fire。"""
        payload = self._fire(text)
        return DispatchHandle(
            external_id=payload.get("claude_code_session_id"),
            external_url=payload.get("claude_code_session_url"),
        )

    def _fire_with_retry(self, request: urllib.request.Request) -> dict[str, Any]:
        """#215: 最大`max_retries`回・指数バックオフでリトライする。

        4xx（認証・入力エラー等の非一時的エラー）はリトライ対象外として即座に送出する。
        """
        delay = self._initial_delay
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                    return result
            except urllib.error.HTTPError as exc:
                if exc.code not in self._RETRYABLE_STATUSES:
                    raise
                last_error = exc
            except urllib.error.URLError as exc:
                last_error = exc
            if attempt < self._max_retries:
                time.sleep(delay)
                delay *= 2
        assert last_error is not None
        raise last_error

    def completion_status(
        self, handle: DispatchHandle
    ) -> Literal["pending", "completed", "abandoned"]:
        """#239/#210: ブランチ名またはclosingIssuesReferencesでPR完了を判定する。"""
        return _task_pr_completion_status(handle)


class CodexCloudDispatchTarget(DispatchTarget):
    """Codex Cloud CLIへサブタスクを非対話で投入するターゲット。

    Codex Cloudはリモートブランチをチェックアウトするため、投入前にworktreeの
    タスクブランチをoriginへpushする。CLIプロセスの終了は投入完了だけを意味するため、
    完了判定はClaude Code Cloud Routineと同様にPR作成をシグナルとして用いる。
    """

    def __init__(self, environment_id: str, log_dir: str | Path = Path("logs")):
        self._environment_id = environment_id
        self._log_dir = Path(log_dir)

    def _build_prompt(self, task: Task, branch_name: str) -> str:
        footprint = ", ".join(task.footprint) if task.footprint else "(未指定)"
        return (
            f"GitHub Issue #{task.issue_number}"
            f"（サブタスク: {task.subtask_id or '不明'}）を"
            "標準開発ワークフローに従って実装してください。\n"
            f"作業ブランチ名は必ず `{branch_name}` としてください。\n"
            f"想定footprint: {footprint}\n"
            f"{NONINTERACTIVE_DISPATCH_INSTRUCTION}\n"
        )

    def launch(
        self, task: Task, branch_name: str, worktree_path: Path
    ) -> DispatchHandle:
        subprocess.run(
            ["git", "push", "--set-upstream", "origin", branch_name],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            check=True,
        )
        self._log_dir.mkdir(parents=True, exist_ok=True)
        slug = branch_name.replace("/", "-")
        log_path = self._log_dir / f"{slug}.log"
        command = [
            "codex",
            "cloud",
            "exec",
            "--env",
            self._environment_id,
            "--branch",
            branch_name,
            self._build_prompt(task, branch_name),
        ]
        with open(log_path, "ab") as log_fh:
            subprocess.run(
                command,
                cwd=str(worktree_path),
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                check=True,
            )
        return DispatchHandle(
            external_id=f"codex-cloud:{branch_name}",
            branch_name=branch_name,
        )

    def completion_status(
        self, handle: DispatchHandle
    ) -> Literal["pending", "completed", "abandoned"]:
        return _task_pr_completion_status(handle)


def build_dispatch_target(
    dispatch_target_name: str,
    routine_id: str | None,
    routine_token: str | None,
    log_dir: str | Path,
    local_cmd: str | None = None,
    codex_cloud_env: str | None = None,
) -> DispatchTarget:
    """#215: CLI引数・環境変数からディスパッチターゲットを組み立てる。

    `cloud-routine`が指定されていても`routine_id`/`routine_token`が
    環境変数・引数のいずれからも解決できない場合は、警告を出した上で
    ローカルのダミー動作（`LocalProcessDispatchTarget`）へ自動フォールバックする。
    `auto`が指定された場合はPATH上のローカルCLI（`claude`優先、次点`agy`、
    `codex`）を検出し、見つかったCLIを指定した場合と同じ挙動にフォールスルーする。
    いずれも見つからない場合も同様に警告を出し、ダミー動作へフォールバックする。
    """
    if dispatch_target_name == "cloud-routine":
        resolved_id = routine_id or os.environ.get(ROUTINE_ID_ENV_VAR)
        resolved_token = routine_token or os.environ.get(ROUTINE_TOKEN_ENV_VAR)
        if resolved_id and resolved_token:
            return ClaudeCodeCloudRoutineDispatchTarget(resolved_id, resolved_token)
        print(
            f"警告: {ROUTINE_ID_ENV_VAR}/{ROUTINE_TOKEN_ENV_VAR}"
            "が未設定のため、クラウドルーチンへのディスパッチはできません。"
            "ローカルのダミー起動にフォールバックします。",
            file=sys.stderr,
        )
    if dispatch_target_name == "codex-cloud":
        resolved_env = codex_cloud_env or os.environ.get(CODEX_CLOUD_ENV_VAR)
        if resolved_env:
            return CodexCloudDispatchTarget(resolved_env, log_dir=log_dir)
        print(
            f"警告: {CODEX_CLOUD_ENV_VAR}が未設定のため、Codex Cloudへの"
            "ディスパッチはできません。ローカルのダミー起動にフォールバックします。",
            file=sys.stderr,
        )
    if dispatch_target_name == "auto":
        detected = detect_installed_local_cli()
        if detected is not None:
            dispatch_target_name = f"{detected}-cli"
        else:
            print(
                "警告: PATH上にclaude/agy/codexのいずれのCLIも見つかりませんでした。"
                "ローカルのダミー起動にフォールバックします。",
                file=sys.stderr,
            )
    if dispatch_target_name == "claude-cli":
        return LocalProcessDispatchTarget(
            log_dir=log_dir, local_cmd=local_cmd or CLAUDE_CLI_LOCAL_CMD_TEMPLATE
        )
    if dispatch_target_name == "agy-cli":
        return LocalProcessDispatchTarget(
            log_dir=log_dir, local_cmd=local_cmd or AGY_CLI_LOCAL_CMD_TEMPLATE
        )
    if dispatch_target_name == "codex-cli":
        return LocalProcessDispatchTarget(
            log_dir=log_dir, local_cmd=local_cmd or CODEX_CLI_LOCAL_CMD_TEMPLATE
        )
    return LocalProcessDispatchTarget(log_dir=log_dir, local_cmd=local_cmd)
