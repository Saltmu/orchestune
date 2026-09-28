"""Claude Code routine launch and completion shared by both workflows."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal

from orchestune.forge import Forge
from orchestune.targets.contracts import (
    DispatchHandle,
    DispatchTarget,
    ExecutionSelection,
    LaunchCapabilities,
    ReviewerBot,
)
from orchestune.targets.support import (
    _noninteractive_instruction,
    _push_branch_and_verify,
    _resolve_base_branch_val,
    _task_pr_completion_status,
)
from orchestune.task_metadata import TaskMetadata

logger = logging.getLogger(__name__)
ROUTINE_ID_ENV_VAR = "ORCHESTUNE_ROUTINE_ID"
ROUTINE_TOKEN_ENV_VAR = "ORCHESTUNE_ROUTINE_TOKEN"


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
    target_name = "cloud-routine"
    launch_capabilities = LaunchCapabilities(durable_attempt=True)

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
        self, task: TaskMetadata, branch_name: str, base_branch: str | None = None
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

    def _fire(
        self, text: str, model: str | None = None, *, retry: bool = True
    ) -> dict[str, Any]:
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
        if retry:
            return self._fire_with_retry(request)
        with urllib.request.urlopen(request, timeout=30) as response:
            result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return result

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
            retry=False,
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

    def fire_text_once(self, text: str) -> DispatchHandle:
        """Fire a durable policy intent once; ambiguous responses require lookup."""
        payload = self._fire(text, retry=False)
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
