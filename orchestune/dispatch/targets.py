"""#181/#215: タスクの実ディスパッチ先を切り替え可能にするStrategyクラス群。

`DispatchTarget`を実装するクラスを差し替えるだけで、ディスパッチャーが
「何に対してタスクを実行させるか」（ローカルsubprocess・Claude Codeクラウドルーチン等）
を変更できる。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from orchestune.dispatch.reviewer import (
    ReviewerBot,
    ReviewerBotSetting,
    resolve_reviewer_bot,
)
from orchestune.forge import Forge, GitHubForge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import is_process_alive
from orchestune.models import Usage
from orchestune.outcome_record import (
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    parse_from_comments,
)

if TYPE_CHECKING:
    from orchestune.dispatch.execution_profiles import ExecutionSelection
    from orchestune.models import PrRecord, Task

logger = logging.getLogger(__name__)


ROUTINE_ID_ENV_VAR = "ORCHESTUNE_ROUTINE_ID"
ROUTINE_TOKEN_ENV_VAR = "ORCHESTUNE_ROUTINE_TOKEN"
CODEX_CLOUD_ENV_VAR = "ORCHESTUNE_CODEX_CLOUD_ENV"

NONINTERACTIVE_DISPATCH_INSTRUCTION = (
    "これは非対話型のバックグラウンド自動実行であり、標準入力からの応答は得られません。"
    "planning_modeによるユーザー承認待ちで停止せず、"
    "実装プラン作成後は直ちに実装・検証・コミットまで完了させてください。"
)


def _noninteractive_instruction(reviewer_bot: ReviewerBot | None) -> str:
    if reviewer_bot is None:
        return NONINTERACTIVE_DISPATCH_INSTRUCTION
    return (
        NONINTERACTIVE_DISPATCH_INSTRUCTION
        + f"PR作成後のレビュー担当には必ず `{reviewer_bot}` を指定し、"
        "レビュー完了と指摘解消まで自律的に進めてください。"
    )


def _resolve_base_branch_val(base_branch: str | None) -> str:
    """PR作成時のベースブランチ名を正規化する（未指定時は'main'）。"""
    return (base_branch.removeprefix("origin/") if base_branch else "") or "main"


_CLAUDE_CLI_LOCAL_CMD_BASE = (
    'claude -p "GitHub Issue #{issue_number} を、'
    "必ず作業ブランチ `{branch_name}` で、"
    "標準開発ワークフローに従って実装してください。"
    "PR作成時は必ずベースブランチに `{base_branch}` を指定してください（`gh pr create --base {base_branch}`）。"
    f'{NONINTERACTIVE_DISPATCH_INSTRUCTION}" '
    "--permission-mode bypassPermissions "
    "--output-format stream-json "
    "--verbose"
)

_AGY_CLI_LOCAL_CMD_BASE = (
    'agy -p "GitHub Issue #{issue_number} を、'
    "必ず作業ブランチ `{branch_name}` で、"
    "標準開発ワークフローに従って実装してください。"
    "PR作成時は必ずベースブランチに `{base_branch}` を指定してください（`gh pr create --base {base_branch}`）。"
    f'{NONINTERACTIVE_DISPATCH_INSTRUCTION}" '
    "--add-dir . --print-timeout 60m --dangerously-skip-permissions"
)

_CODEX_CLI_LOCAL_CMD_BASE = (
    'codex exec "GitHub Issue #{issue_number} を、'
    "必ず作業ブランチ `{branch_name}` で、"
    "標準開発ワークフローに従って実装してください。"
    "PR作成時は必ずベースブランチに `{base_branch}` を指定してください（`gh pr create --base {base_branch}`）。"
    f'{NONINTERACTIVE_DISPATCH_INSTRUCTION}" '
    "--dangerously-bypass-approvals-and-sandbox"
)

_LOCAL_CMD_BASE_BY_TARGET = {
    "claude-cli": _CLAUDE_CLI_LOCAL_CMD_BASE,
    "agy-cli": _AGY_CLI_LOCAL_CMD_BASE,
    "codex-cli": _CODEX_CLI_LOCAL_CMD_BASE,
}


def _default_local_cmd_template(
    target_name: str, reviewer_bot: ReviewerBot | None
) -> str:
    return _LOCAL_CMD_BASE_BY_TARGET[target_name].replace(
        NONINTERACTIVE_DISPATCH_INSTRUCTION,
        _noninteractive_instruction(reviewer_bot),
    )


CLAUDE_CLI_LOCAL_CMD_TEMPLATE = _default_local_cmd_template(
    "claude-cli", resolve_reviewer_bot("auto", "claude-cli")
)
AGY_CLI_LOCAL_CMD_TEMPLATE = _default_local_cmd_template(
    "agy-cli", resolve_reviewer_bot("auto", "agy-cli")
)
CODEX_CLI_LOCAL_CMD_TEMPLATE = _default_local_cmd_template(
    "codex-cli", resolve_reviewer_bot("auto", "codex-cli")
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
    started_at: float | None = None


class DispatchTarget(ABC):
    """タスクを実際にどこへディスパッチするかを表す戦略インターフェース。"""

    @abstractmethod
    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        """タスクに対応するエージェントを起動し、追跡用ハンドルを返す。

        #384: `force_push=True`は、自動リベース後の再launch（ローカルで書き
        換え済みの履歴を再pushする必要がある場合）を呼び出し元が明示するため
        のフラグ。pushを行わない実装では無視してよい。

        #711: `base_branch`はタスクPR作成先のベースブランチ（親Issueモード時は
        `parent/issue-{N}`、通常時は`main`）。未指定時は`None`（各実装側で
        `main`へフォールバック）。
        """

    @abstractmethod
    def is_complete(self, handle: DispatchHandle, forge: Forge | None = None) -> bool:
        """`launch`で起動した実行が完了しているかどうかを判定する。"""

    def completion_status(
        self, handle: DispatchHandle, forge: Forge | None = None
    ) -> Literal["pending", "completed", "abandoned"]:
        """Return a lifecycle status; local targets only expose pending/completed.

        #315レビュー対応: `is_complete`の旧シグネチャ（`forge`引数なし）を実装した
        サブクラスが残っていても、TypeErrorにせず引数無しで再試行して互換性を保つ。
        """
        try:
            complete = self.is_complete(handle, forge=forge)
        except TypeError:
            complete = self.is_complete(handle)
        return "completed" if complete else "pending"

    def collect_usage(self, handle: DispatchHandle) -> Usage | None:
        """#438: 完了した実行の消費量および動作モデル名を返す。取得できない場合は None。"""
        return None


def _parse_github_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_stale_pr_for_handle(pr: PrRecord, handle: DispatchHandle) -> bool:
    """#246: session開始（`handle.started_at`）より前に作成されたPRは、状態
    （OPEN/MERGED/CLOSED）に関係なく現在のsessionの成果物ではない。同名branchの
    古いMERGED PR等で再キュー後の新sessionが即completed扱いされないよう除外する。

    `created_at`を取得・解釈できないPRも現世代の証拠とはみなさない（fail
    closed）。`started_at`を持たないhandle（復元経路等）は従来通り除外しない。
    CLOSED PRの`closed_at < started_at`によるstale判定（#210）は、
    `closed_at >= created_at`であるため`created_at`判定に包含される。

    #262レビュー対応: GitHubの`created_at`は秒精度で切り捨てられる一方、
    `handle.started_at`は小数秒を含む（各task起動直前に`time.time()`で
    取得）。直接比較すると、実際にはsession開始「後」に作成された正規PRでも
    `created_at < started_at`が真になり誤ってstale扱いされうる
    （例: created_at=X.000, started_at=X.900）。比較はGitHub側の精度に
    合わせて`started_at`を秒単位に切り捨ててから行い、同じ秒に作成された
    PRはstaleとしない。"""
    if handle.started_at is None:
        return False
    created_at = _parse_github_timestamp(pr.created_at)
    return created_at is None or created_at < math.floor(handle.started_at)


def _check_open_prs_outcome(
    open_prs: list[PrRecord],
    handle: DispatchHandle,
    forge: Forge,
) -> Literal["completed", "unknown", "pending"]:
    all_comments: list[Mapping[str, Any]] = []
    had_error = False
    if handle.issue_number is not None:
        try:
            all_comments.extend(forge.list_comments(handle.issue_number))
        except Exception:
            had_error = True
    pr_numbers = {pr.number for pr in open_prs}
    for pr_num in pr_numbers:
        if handle.issue_number is None or pr_num != handle.issue_number:
            try:
                all_comments.extend(forge.list_comments(pr_num))
            except Exception:
                had_error = True
    outcome = parse_from_comments(all_comments, since=handle.started_at)
    if outcome is not None and outcome.result in (
        RESULT_DONE,
        RESULT_NOT_NEEDED,
        RESULT_BLOCKED,
    ):
        return "completed"
    if had_error and outcome is None:
        return "unknown"
    return "pending"


def _task_pr_completion_status(
    handle: DispatchHandle,
    forge: Forge | None = None,
) -> Literal["pending", "completed", "abandoned", "unknown"]:
    if handle.branch_name is None and handle.issue_number is None:
        return "pending"
    forge = forge or GitHubForge()
    try:
        prs = forge.list_prs(state="all")
    except Exception:
        return "unknown"
    matching_prs = [
        pr
        for pr in prs
        if (
            (handle.branch_name is not None and pr.head_ref == handle.branch_name)
            or (
                handle.issue_number is not None
                and handle.issue_number in pr.closes_issue_numbers
            )
        )
        and not _is_stale_pr_for_handle(pr, handle)
    ]
    if any(pr.state == "MERGED" for pr in matching_prs):
        return "completed"

    open_prs = [pr for pr in matching_prs if pr.state == "OPEN"]
    if open_prs:
        return _check_open_prs_outcome(open_prs, handle, forge)

    if any(pr.state == "CLOSED" for pr in matching_prs):
        return "abandoned"
    return "pending"


def default_dry_run_command_builder(task: Task, worktree_path: Path) -> list[str]:
    return ["true"]


class BranchReachabilityError(RuntimeError):
    """#244レビュー対応: `_push_branch_and_verify`の到達性検証失敗専用の例外。

    `create_worktree_and_launch`側で汎用`RuntimeError`を捕捉すると、
    このチェック以外の実装バグまで通常の起動失敗として握り潰してしまうため、
    この専用型だけを捕捉させる。
    """


def _push_branch_and_verify(
    branch_name: str, worktree_path: Path, *, force: bool = False
) -> None:
    """#244: stacked/parent base付きで作成されたローカルtask branchを、リモート
    セッションがその内容ごとcheckoutできるようoriginへpushし、到達性を検証する。

    push後に`git ls-remote`でリモートbranchのSHAをローカルHEADと照合し、
    確認できない場合は`BranchReachabilityError`を送出する
    （呼び出し側はfireせずfail closed）。

    #384: `force=True`の場合は`--force-with-lease`を付与する。自動リベースは
    ローカルで既存の履歴を書き換えるため、force無しの通常pushは常に
    non-fast-forwardで拒否される（初回起動時の新規ブランチpushには影響しない）。
    """
    push_args = ["push", "--set-upstream", "origin", branch_name]
    if force:
        push_args.insert(1, "--force-with-lease")
    run_git(push_args, cwd=worktree_path, check=True)
    local_sha = run_git(
        ["rev-parse", "HEAD"], cwd=worktree_path, check=True
    ).stdout.strip()
    ls_remote_output = run_git(
        ["ls-remote", "origin", f"refs/heads/{branch_name}"],
        cwd=worktree_path,
        check=True,
    ).stdout.strip()
    remote_sha = ls_remote_output.split()[0] if ls_remote_output else ""
    if not remote_sha or remote_sha != local_sha:
        raise BranchReachabilityError(
            f"リモートブランチ '{branch_name}' の到達性を検証できませんでした "
            f"(local={local_sha or '不明'}, remote={remote_sha or '不在'})。"
            "baseの変更を含まないセッション起動を防ぐため、fireを中止します。"
        )


def _is_pid_alive(pid: int | None) -> bool:
    return is_process_alive(pid)


def _local_cli_name(command: list[str]) -> str | None:
    """Return the supported CLI executable without inspecting prompt text."""
    if not command:
        return None
    executable = Path(command[0]).name.lower().removesuffix(".exe")
    if executable in LOCAL_CLI_CANDIDATES:
        return executable
    return None


def _format_local_cmd(
    local_cmd: str,
    task: Task,
    branch_name: str,
    worktree_path: Path,
    model: str | None,
    reasoning_effort: str | None,
    profile_name: str,
    reviewer_bot: ReviewerBot | None = None,
    base_branch: str | None = None,
) -> list[str]:
    base_branch_val = _resolve_base_branch_val(base_branch)
    formatted_cmd = local_cmd.format(
        issue_number=task.issue_number,
        subtask_id=task.subtask_id or "",
        branch_name=branch_name,
        worktree_path=str(worktree_path).replace("\\", "\\\\"),
        model=model or "",
        reasoning_effort=reasoning_effort or "",
        profile=profile_name,
        reviewer_bot=reviewer_bot or "",
        base_branch=base_branch_val,
    )
    cmd = shlex.split(formatted_cmd)
    cli_name = _local_cli_name(cmd)

    if "{model}" not in local_cmd and model:
        if cli_name is not None:
            cmd.extend(["--model", model])

    if "{reasoning_effort}" not in local_cmd and reasoning_effort:
        if cli_name == "codex":
            cmd.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
        elif cli_name in ("claude", "agy"):
            logger.warning(
                "Target %r does not support reasoning_effort %r; skipping setting",
                f"{cli_name}-cli",
                reasoning_effort,
            )
    return cmd


class LocalProcessDispatchTarget(DispatchTarget):
    """ローカルマシン上のサブプロセスとしてエージェントを起動する戦略。

    デフォルト（dry runモード）では何も実行しない`default_dry_run_command_builder`を使う。
    `local_cmd`が指定された場合は、テンプレート文字列からコマンドを生成して実行する。
    """

    def __init__(
        self,
        command_builder: Callable[
            [Task, Path], list[str]
        ] = default_dry_run_command_builder,
        log_dir: str | Path = Path("logs"),
        local_cmd: str | None = None,
        reviewer_bot: ReviewerBot | None = None,
    ):
        self._command_builder = command_builder
        self._log_dir = Path(log_dir)
        self._local_cmd = local_cmd
        self._reviewer_bot = reviewer_bot

    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        model = execution_selection.model if execution_selection else None
        reasoning_effort = (
            execution_selection.reasoning_effort if execution_selection else None
        )
        profile_name = (
            execution_selection.profile
            if execution_selection
            else (task.execution_profile or "")
        )

        if self._local_cmd:
            cmd = _format_local_cmd(
                self._local_cmd,
                task,
                branch_name,
                worktree_path,
                model,
                reasoning_effort,
                profile_name,
                self._reviewer_bot,
                base_branch=base_branch,
            )
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

    def is_complete(self, handle: DispatchHandle, forge: Forge | None = None) -> bool:
        return not _is_pid_alive(handle.pid)

    def collect_usage(self, handle: DispatchHandle) -> Usage | None:
        """#438: ログファイルから usage / model 情報を抽出して返す。"""
        if not handle.branch_name:
            return None
        slug = handle.branch_name.replace("/", "-")
        log_path = self._log_dir / f"{slug}.log"
        return _parse_usage_from_log(log_path)


def _extract_usage_from_dict(data: Any) -> Usage | None:
    if not isinstance(data, dict):
        return None
    usage_data = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if not isinstance(usage_data, dict):
        return None
    if "input_tokens" not in usage_data and "output_tokens" not in usage_data:
        return None
    try:
        input_tokens = int(usage_data.get("input_tokens", 0))
        output_tokens = int(usage_data.get("output_tokens", 0))
        total_tokens = int(usage_data.get("total_tokens", input_tokens + output_tokens))
        model = (
            str(data["model"])
            if data.get("model") is not None
            else (
                str(usage_data["model"])
                if usage_data.get("model") is not None
                else None
            )
        )
        cost_val = data.get("total_cost_combined")
        if cost_val is None:
            cost_val = data.get("cost_usd")
        if cost_val is None and usage_data is not data:
            cost_val = usage_data.get("total_cost_combined")
        if cost_val is None and usage_data is not data:
            cost_val = usage_data.get("cost_usd")
        cost_usd = float(cost_val) if cost_val is not None else None
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            model=model,
            cost_usd=cost_usd,
        )
    except (TypeError, ValueError):
        return None


def _parse_usage_from_log(log_path: Path) -> Usage | None:
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                usage = _extract_usage_from_dict(data)
                if usage is not None:
                    return usage
            except Exception:
                pass
    try:
        data = json.loads(content.strip())
        usage = _extract_usage_from_dict(data)
        if usage is not None:
            return usage
    except Exception:
        pass
    return None


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
        reviewer_bot: ReviewerBot | None = None,
    ):
        self._routine_id = routine_id
        self._routine_token = routine_token
        self._max_retries = max_retries
        self._initial_delay = initial_delay
        self._reviewer_bot = reviewer_bot

    def _build_text(
        self, task: Task, branch_name: str, base_branch: str | None = None
    ) -> str:
        footprint = ", ".join(task.footprint) if task.footprint else "(未指定)"
        base_branch_val = _resolve_base_branch_val(base_branch)
        return (
            f"GitHub Issue #{task.issue_number}"
            f"（サブタスク: {task.subtask_id or '不明'}）を"
            "標準開発ワークフローに従って実装してください。\n"
            f"作業ブランチ名は必ず `{branch_name}` としてください。\n"
            # #244: stacked/parent baseの変更はpush済みbranchにしか含まれない。
            # default branch基点で同名branchを新規作成すると成果物からbaseの
            # 変更が欠落するため、必ずorigin上のbranchを起点にさせる。
            f"作業ブランチ `{branch_name}` は、依存先・親ブランチ（base）の内容を"
            "含む状態でoriginへpush済みです。ブランチを新規作成せず、必ず"
            f"originから `{branch_name}` をfetchしてcheckoutし、その内容を"
            "起点に作業してください。\n"
            f"想定footprint: {footprint}\n"
            f"PR作成時は必ずベースブランチに `{base_branch_val}` を指定してください（`gh pr create --base {base_branch_val}`）。\n"
            f"{_noninteractive_instruction(self._reviewer_bot)}\n"
        )

    def _fire(self, text: str, model: str | None = None) -> dict[str, Any]:
        """任意のテキスト指示でルーチンをfireし、生のレスポンスペイロードを返す。"""
        payload_dict: dict[str, Any] = {"text": text}
        if model is not None:
            payload_dict["model"] = model
        body = json.dumps(payload_dict).encode("utf-8")
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
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        # #244: fireより先にpush・到達性検証を行い、確認できなければfireしない。
        _push_branch_and_verify(branch_name, worktree_path, force=force_push)
        model = execution_selection.model if execution_selection else None
        reasoning_effort = (
            execution_selection.reasoning_effort if execution_selection else None
        )
        if reasoning_effort is not None:
            logger.warning(
                "ClaudeCodeCloudRoutineDispatchTarget does not support reasoning_effort %r; skipping setting",
                reasoning_effort,
            )
        payload = self._fire(
            self._build_text(task, branch_name, base_branch=base_branch),
            model=model,
        )
        return DispatchHandle(
            external_id=payload.get("claude_code_session_id"),
            external_url=payload.get("claude_code_session_url"),
            branch_name=branch_name,
        )

    def fire_text(self, text: str, model: str | None = None) -> DispatchHandle:
        """#186: タスク以外の任意指示（統合コーディネーターの意味的レビュー等）を
        dispatcherと同一のルーチンへ投げるための汎用fire。"""
        payload = self._fire(text, model=model)
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
        self, handle: DispatchHandle, forge: Forge | None = None
    ) -> Literal["pending", "completed", "abandoned"]:
        """#239/#210: ブランチ名またはclosingIssuesReferencesでPR完了を判定する。"""
        status = _task_pr_completion_status(handle, forge=forge)
        return "pending" if status == "unknown" else status

    def is_complete(self, handle: DispatchHandle, forge: Forge | None = None) -> bool:
        """#239: ブランチ名一致を優先判定としつつ、AIセッションが指示された
        ブランチ名に従わなかった場合に備え、PRの`closingIssuesReferences`
        （`Closes #N`等から解決されるIssue参照）によるフォールバック判定も行う。"""
        return self.completion_status(handle, forge=forge) == "completed"


_CODEX_TASK_URL_RE = re.compile(r"https?://[^\s]+/tasks/([a-zA-Z0-9_-]+)")
_CODEX_TASK_ID_RE = re.compile(r"\b(task_[a-zA-Z0-9_-]+)\b")
_CODEX_CLOUD_TERMINAL_FAILED_STATUSES: frozenset[str] = frozenset(
    {"failed", "cancelled", "canceled", "error"}
)


def _parse_codex_cloud_exec_output(output: str) -> tuple[str | None, str | None]:
    """codex cloud exec の出力から実タスク ID / URL を抽出する。"""
    url_match = _CODEX_TASK_URL_RE.search(output)
    if url_match:
        return url_match.group(1), url_match.group(0)
    id_match = _CODEX_TASK_ID_RE.search(output)
    if id_match:
        return id_match.group(1), None
    return None, None


def _fetch_codex_cloud_page(
    environment_id: str, cursor: str | None
) -> tuple[list[Any], str | None, bool]:
    cmd = ["codex", "cloud", "list", "--env", environment_id, "--json"]
    if cursor:
        cmd.extend(["--cursor", cursor])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            return [], None, False
        data = json.loads(proc.stdout)
        items: list[Any] = []
        next_cursor: str | None = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            raw_items = data.get("items") or data.get("tasks")
            if isinstance(raw_items, list):
                items = raw_items
            raw_cursor = data.get("cursor")
            if isinstance(raw_cursor, str) and raw_cursor.strip():
                next_cursor = raw_cursor.strip()
        return items, next_cursor, True
    except Exception:
        return [], None, False


def _fetch_codex_cloud_task_status(
    environment_id: str,
    task_id: str,
) -> str | None:
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        if cursor is not None and cursor in seen_cursors:
            break
        if cursor is not None:
            seen_cursors.add(cursor)

        items, next_cursor, success = _fetch_codex_cloud_page(environment_id, cursor)
        if not success:
            return None

        for item in items:
            if isinstance(item, dict) and item.get("id") == task_id:
                status = item.get("status")
                return str(status).lower() if status is not None else None

        if not next_cursor:
            break
        cursor = next_cursor
    return None


def _run_codex_cloud_exec(
    command: list[str],
    worktree_path: Path,
    log_path: Path,
) -> str:
    proc = subprocess.run(
        command,
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    combined_output = f"{proc.stdout or ''}{proc.stderr or ''}"
    with open(log_path, "a", encoding="utf-8") as log_fh:
        log_fh.write(combined_output)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode,
            command,
            output=proc.stdout,
            stderr=proc.stderr,
        )
    return combined_output


class CodexCloudDispatchTarget(DispatchTarget):
    """Codex Cloud CLIへサブタスクを非対話で投入するターゲット。

    Codex Cloudはリモートブランチをチェックアウトするため、投入前にworktreeの
    タスクブランチをoriginへpushする。投入時に実タスクID/URLを抽出し、
    Cloud実タスク状態照合とPR状態を組み合わせて完了判定を行う。
    """

    def __init__(
        self,
        environment_id: str,
        log_dir: str | Path = Path("logs"),
        reviewer_bot: ReviewerBot | None = None,
    ):
        self._environment_id = environment_id
        self._log_dir = Path(log_dir)
        self._reviewer_bot = reviewer_bot

    def _build_prompt(
        self, task: Task, branch_name: str, base_branch: str | None = None
    ) -> str:
        footprint = ", ".join(task.footprint) if task.footprint else "(未指定)"
        base_branch_val = _resolve_base_branch_val(base_branch)
        return (
            f"GitHub Issue #{task.issue_number}"
            f"（サブタスク: {task.subtask_id or '不明'}）を"
            "標準開発ワークフローに従って実装してください。\n"
            f"作業ブランチ名は必ず `{branch_name}` としてください。\n"
            f"想定footprint: {footprint}\n"
            f"PR作成時は必ずベースブランチに `{base_branch_val}` を指定してください（`gh pr create --base {base_branch_val}`）。\n"
            f"{_noninteractive_instruction(self._reviewer_bot)}\n"
        )

    def _fetch_task_status(self, task_id: str) -> str | None:
        """テスト容易性のための内部委任フック。"""
        return _fetch_codex_cloud_task_status(self._environment_id, task_id)

    def launch(
        self,
        task: Task,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        push_args = ["push", "--set-upstream", "origin", branch_name]
        if force_push:
            push_args.insert(1, "--force-with-lease")
        run_git(push_args, cwd=worktree_path, check=True)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        slug = branch_name.replace("/", "-")
        log_path = self._log_dir / f"{slug}.log"

        model = execution_selection.model if execution_selection else None
        reasoning_effort = (
            execution_selection.reasoning_effort if execution_selection else None
        )

        command = [
            "codex",
            "cloud",
            "exec",
            "--env",
            self._environment_id,
            "--branch",
            branch_name,
        ]
        if model:
            command.extend(["--model", model])
        if reasoning_effort:
            command.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
        command.append(self._build_prompt(task, branch_name, base_branch=base_branch))

        combined_output = _run_codex_cloud_exec(command, worktree_path, log_path)
        task_id, task_url = _parse_codex_cloud_exec_output(combined_output)
        external_id = task_id or f"codex-cloud:{branch_name}"
        return DispatchHandle(
            external_id=external_id,
            external_url=task_url,
            branch_name=branch_name,
            issue_number=task.issue_number,
        )

    def completion_status(
        self, handle: DispatchHandle, forge: Forge | None = None
    ) -> Literal["pending", "completed", "abandoned"]:
        pr_status = _task_pr_completion_status(handle, forge=forge)
        if pr_status == "unknown":
            # PRの取得失敗時はCloud障害を誤ってabandoned扱いにしない
            return "pending"
        if pr_status != "pending":
            return pr_status
        if handle.external_id is not None and not handle.external_id.startswith(
            "codex-cloud:"
        ):
            cloud_status = self._fetch_task_status(handle.external_id)
            if cloud_status in _CODEX_CLOUD_TERMINAL_FAILED_STATUSES:
                return "abandoned"
        return "pending"

    def is_complete(self, handle: DispatchHandle, forge: Forge | None = None) -> bool:
        return self.completion_status(handle, forge=forge) == "completed"


@dataclass(frozen=True)
class TargetBuildConfig:
    """#476: `build_dispatch_target`の入力を集約するDTO。"""

    dispatch_target_name: str
    routine_id: str | None
    routine_token: str | None
    log_dir: str | Path
    local_cmd: str | None = None
    codex_cloud_env: str | None = None
    allow_unsafe_agent_execution: bool = False
    reviewer_bot: ReviewerBotSetting = "auto"


def _resolve_target_name(dispatch_target_name: str, allow_unsafe: bool) -> str:
    is_unsafe = dispatch_target_name in {"claude-cli", "agy-cli", "codex-cli"}
    if dispatch_target_name == "auto":
        detected = detect_installed_local_cli()
        if detected is not None:
            dispatch_target_name = f"{detected}-cli"
            is_unsafe = True
        else:
            print(
                "警告: PATH上にclaude/agy/codexのいずれのCLIも見つかりませんでした。"
                "ローカルのダミー起動にフォールバックします。",
                file=sys.stderr,
            )
            dispatch_target_name = "local"

    if is_unsafe and not allow_unsafe:
        raise ValueError(
            f"設定エラー: `{dispatch_target_name}` によるローカル無人実行は、承認やサンドボックスのバイパスを伴う完全権限実行となります。\n"
            "この実行を許可するには、信頼できる実行環境であることを確認の上、明示的に `--allow-unsafe-agent-execution` オプションを指定するか、"
            "設定ファイル（orchestune.toml 等）で `allow_unsafe_agent_execution = true` を設定してください。"
        )
    return dispatch_target_name


def _build_cloud_routine_target(
    routine_id: str | None,
    routine_token: str | None,
    reviewer_bot: ReviewerBot | None,
) -> ClaudeCodeCloudRoutineDispatchTarget | None:
    resolved_id = routine_id or os.environ.get(ROUTINE_ID_ENV_VAR)
    resolved_token = routine_token or os.environ.get(ROUTINE_TOKEN_ENV_VAR)
    if resolved_id and resolved_token:
        return ClaudeCodeCloudRoutineDispatchTarget(
            resolved_id, resolved_token, reviewer_bot=reviewer_bot
        )
    print(
        f"警告: {ROUTINE_ID_ENV_VAR}/{ROUTINE_TOKEN_ENV_VAR}"
        "が未設定のため、クラウドルーチンへのディスパッチはできません。"
        "ローカルのダミー起動にフォールバックします。",
        file=sys.stderr,
    )
    return None


def _build_codex_cloud_target(
    codex_cloud_env: str | None,
    log_dir: str | Path,
    reviewer_bot: ReviewerBot | None,
) -> CodexCloudDispatchTarget | None:
    resolved_env = codex_cloud_env or os.environ.get(CODEX_CLOUD_ENV_VAR)
    if resolved_env:
        return CodexCloudDispatchTarget(
            resolved_env, log_dir=log_dir, reviewer_bot=reviewer_bot
        )
    print(
        f"警告: {CODEX_CLOUD_ENV_VAR}が未設定のため、Codex Cloudへの"
        "ディスパッチはできません。ローカルのダミー起動にフォールバックします。",
        file=sys.stderr,
    )
    return None


def _warn_unresolved_auto_reviewer(resolved_target_name: str) -> None:
    print(
        f"警告: 実行ターゲット `{resolved_target_name}` からレビュアーボットを"
        "自動選択できません。決定論的なレビュー担当が必要な場合は "
        "`--reviewer-bot claude|codex` を明示してください。",
        file=sys.stderr,
    )


def _resolve_local_fallback_reviewer(config: TargetBuildConfig) -> ReviewerBot | None:
    reviewer_bot = resolve_reviewer_bot(config.reviewer_bot, "local")
    if reviewer_bot is None and config.local_cmd is not None:
        _warn_unresolved_auto_reviewer("local")
    return reviewer_bot


def build_dispatch_target(config: TargetBuildConfig) -> DispatchTarget:
    target_name = _resolve_target_name(
        config.dispatch_target_name, config.allow_unsafe_agent_execution
    )
    reviewer_bot = resolve_reviewer_bot(config.reviewer_bot, target_name)
    auto_dummy_fallback = (
        config.dispatch_target_name == "auto"
        and target_name == "local"
        and config.local_cmd is None
    )
    if reviewer_bot is None and not auto_dummy_fallback:
        _warn_unresolved_auto_reviewer(target_name)
    if target_name == "cloud-routine":
        cloud_target = _build_cloud_routine_target(
            config.routine_id, config.routine_token, reviewer_bot
        )
        if cloud_target is not None:
            return cloud_target
        reviewer_bot = _resolve_local_fallback_reviewer(config)
    elif target_name == "codex-cloud":
        codex_target = _build_codex_cloud_target(
            config.codex_cloud_env, config.log_dir, reviewer_bot
        )
        if codex_target is not None:
            return codex_target
        reviewer_bot = _resolve_local_fallback_reviewer(config)

    elif target_name in _LOCAL_CMD_BASE_BY_TARGET:
        return LocalProcessDispatchTarget(
            log_dir=config.log_dir,
            local_cmd=config.local_cmd
            or _default_local_cmd_template(target_name, reviewer_bot),
            reviewer_bot=reviewer_bot,
        )
    return LocalProcessDispatchTarget(
        log_dir=config.log_dir,
        local_cmd=config.local_cmd,
        reviewer_bot=reviewer_bot,
    )
