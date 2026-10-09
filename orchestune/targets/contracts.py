"""Execution contracts shared by dispatch targets and integration review."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orchestune.forge import Forge
from orchestune.models import Usage
from orchestune.task_metadata import TaskMetadata

ReviewerBot = Literal["claude", "codex"]
ReviewerBotSetting = Literal["auto", "claude", "codex"]


@dataclass(frozen=True)
class ExecutionSelection:
    """Deterministic selection result for a task execution profile."""

    profile: str
    model: str | None
    reasoning_effort: str | None
    reason: str


@dataclass(frozen=True)
class DispatchHandle:
    """起動したエージェント実行を後から追跡するための不透明なハンドル。"""

    pid: int | None = None
    external_id: str | None = None
    external_url: str | None = None
    branch_name: str | None = None
    issue_number: int | None = None
    started_at: float | None = None
    launch_attempt_id: str | None = None
    # #1270: 実際に起動したtarget名と、その実行が書き込む追記ログの範囲。ログは
    # 前回実行の末尾が残るため、開始offset以降だけを当該実行の出力として読む。
    target_name: str | None = None
    log_path: str | None = None
    log_offset: int | None = None


@dataclass(frozen=True)
class LaunchCapabilities:
    """Attempt lookup must identify one execution by ID, not by branch similarity.

    Idempotent targets override launch_attempt to pass the ID as the provider key.
    A None lookup result is inconclusive, not proof that a launch did not happen.
    """

    durable_attempt: bool = False
    idempotent_launch: bool = False
    lookup_by_attempt: bool = False


class DispatchTarget(ABC):
    """タスクを実際にどこへディスパッチするかを表す戦略インターフェース。"""

    target_name: str | None = None
    launch_capabilities = LaunchCapabilities()

    def lookup_launch_attempt(self, attempt_id: str) -> DispatchHandle | None:
        """Return a uniquely matched handle, or None when reconciliation is unavailable."""
        return None

    def launch_attempt(
        self,
        attempt_id: str,
        task: TaskMetadata,
        branch_name: str,
        worktree_path: Path,
        *,
        force_push: bool = False,
        execution_selection: ExecutionSelection | None = None,
        base_branch: str | None = None,
    ) -> DispatchHandle:
        """Launch once; idempotent providers override this to use attempt_id as a key."""
        return self.launch(
            task,
            branch_name,
            worktree_path,
            force_push=force_push,
            execution_selection=execution_selection,
            base_branch=base_branch,
        )

    @abstractmethod
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

    def execution_status(
        self, handle: DispatchHandle
    ) -> Literal["running", "stopped", "unknown"]:
        """Return whether the external execution can still run code (#1154).

        PR・ラベル・成果物は参照しない（それらは`completion_status`の責務）。
        `stopped`はproviderが当該IDに対し再開不能な終端状態を返した場合のみ。
        未対応・ID欠落・照合不能・例外は`unknown`であり、停止確認として扱わない。
        """
        return "unknown"

    def collect_usage(self, handle: DispatchHandle) -> Usage | None:
        """#438: 完了した実行の消費量および動作モデル名を返す。取得できない場合は None。"""
        return None
