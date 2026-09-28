"""#181/#215: タスクの実ディスパッチ先を切り替え可能にするStrategyクラス群。

`DispatchTarget`を実装するクラスを差し替えるだけで、ディスパッチャーが
「何に対してタスクを実行させるか」（ローカルsubprocess・Claude Codeクラウドルーチン等）
を変更できる。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from orchestune.dispatch.reviewer import (
    ReviewerBot,
    ReviewerBotSetting,
    resolve_reviewer_bot,
)
from orchestune.forge import Forge
from orchestune.infra.git_cli import run_git
from orchestune.infra.process_utils import is_process_alive
from orchestune.targets.cloud_routine import (
    ROUTINE_ID_ENV_VAR as ROUTINE_ID_ENV_VAR,
)
from orchestune.targets.cloud_routine import (
    ROUTINE_TOKEN_ENV_VAR as ROUTINE_TOKEN_ENV_VAR,
)
from orchestune.targets.cloud_routine import (
    ClaudeCodeCloudRoutineDispatchTarget as ClaudeCodeCloudRoutineDispatchTarget,
)
from orchestune.targets.contracts import (
    DispatchHandle as DispatchHandle,
)
from orchestune.targets.contracts import (
    DispatchTarget as DispatchTarget,
)
from orchestune.targets.contracts import (
    ExecutionSelection,
)
from orchestune.targets.contracts import (
    LaunchCapabilities as LaunchCapabilities,
)
from orchestune.targets.support import (
    NONINTERACTIVE_DISPATCH_INSTRUCTION as NONINTERACTIVE_DISPATCH_INSTRUCTION,
)
from orchestune.targets.support import (
    BranchReachabilityError as BranchReachabilityError,
)
from orchestune.targets.support import (
    _lookup_issue_outcome as _lookup_issue_outcome,
)
from orchestune.targets.support import (
    _noninteractive_instruction,
    _resolve_base_branch_val,
    _task_pr_completion_status,
)
from orchestune.targets.support import (
    _push_branch_and_verify as _push_branch_and_verify,
)
from orchestune.targets.usage import LocalUsageProvider
from orchestune.targets.usage import (
    _extract_usage_from_dict as _extract_usage_from_dict,
)
from orchestune.targets.usage import _parse_usage_from_log as _parse_usage_from_log
from orchestune.task_metadata import TaskMetadata

logger = logging.getLogger(__name__)


CODEX_CLOUD_ENV_VAR = "ORCHESTUNE_CODEX_CLOUD_ENV"


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


def default_dry_run_command_builder(
    task: TaskMetadata, worktree_path: Path
) -> list[str]:
    return ["true"]


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
    task: TaskMetadata,
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
        elif cli_name == "claude":
            cmd.extend(["--effort", reasoning_effort])
        elif cli_name == "agy":
            logger.warning(
                "Target %r does not support reasoning_effort %r; skipping setting",
                f"{cli_name}-cli",
                reasoning_effort,
            )
    return cmd


class LocalProcessDispatchTarget(LocalUsageProvider, DispatchTarget):
    """ローカルマシン上のサブプロセスとしてエージェントを起動する戦略。

    デフォルト（dry runモード）では何も実行しない`default_dry_run_command_builder`を使う。
    `local_cmd`が指定された場合は、テンプレート文字列からコマンドを生成して実行する。
    """

    def __init__(
        self,
        command_builder: Callable[
            [TaskMetadata, Path], list[str]
        ] = default_dry_run_command_builder,
        log_dir: str | Path = Path("logs"),
        local_cmd: str | None = None,
        reviewer_bot: ReviewerBot | None = None,
        target_name: str | None = None,
    ):
        self._command_builder = command_builder
        self._log_dir = Path(log_dir)
        self._local_cmd = local_cmd
        self._reviewer_bot = reviewer_bot
        self.target_name = target_name

    def launch(
        self,
        task: TaskMetadata,
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

    target_name = "codex-cloud"
    launch_capabilities = LaunchCapabilities(durable_attempt=True)

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
        self, task: TaskMetadata, branch_name: str, base_branch: str | None = None
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
        task: TaskMetadata,
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
            "この実行を許可するには、信頼できる実行環境であることを確認の上、明示的に `--allow-unsafe-agent-execution` オプションを指定してください。"
        )
    return dispatch_target_name


def _build_cloud_routine_target(
    routine_id: str | None,
    routine_token: str | None,
    reviewer_bot: ReviewerBot | None,
) -> ClaudeCodeCloudRoutineDispatchTarget | None:
    resolved_id = os.environ.get(ROUTINE_ID_ENV_VAR) or routine_id
    resolved_token = os.environ.get(ROUTINE_TOKEN_ENV_VAR) or routine_token
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
    resolved_env = os.environ.get(CODEX_CLOUD_ENV_VAR) or codex_cloud_env
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
        '設定ファイルで `reviewer_bot = "claude"` または `reviewer_bot = "codex"` を指定してください。',
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
            target_name=target_name,
        )
    return LocalProcessDispatchTarget(
        log_dir=config.log_dir,
        local_cmd=config.local_cmd,
        reviewer_bot=reviewer_bot,
        target_name="local",
    )
