"""#119: status:queuedラベルを付与したactorのGitHub権限を検証する（decide/act）。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestune import github
from orchestune.dispatch_escalation import apply_human_review_escalation
from orchestune.dispatch_scoring import Task

if TYPE_CHECKING:
    from orchestune.dispatcher import DispatcherConfig

_AUTHORIZED_PERMISSIONS = frozenset({"admin", "maintain", "write", "triage"})


@dataclass
class ActorVerificationDecision:
    task: Task
    actor: str
    permission: str
    is_authorized: bool


def _decide_actor_verification(
    candidate_tasks: list[Task],
) -> list[ActorVerificationDecision]:
    """`status:queued`を付与したactorのリポジトリ権限を判定する（読み取りのみ）。

    #208: actorを特定できない・権限判定でエラーが発生したタスクは、サイクル全体を
    停止させず、安全側（未認可）として扱い次のタスクの判定を継続する。
    """
    decisions = []
    for task in candidate_tasks:
        actor = ""
        try:
            actor = github.get_label_actor(task.issue_number, "status:queued")
            permission = github.get_actor_permission(actor)
        except Exception as exc:
            print(
                f"Warning: actor検証に失敗しました (issue #{task.issue_number}, "
                f"actor={actor!r}): {exc}. 未認可として扱います。",
                file=sys.stderr,
            )
            permission = "none"
        decisions.append(
            ActorVerificationDecision(
                task=task,
                actor=actor,
                permission=permission,
                is_authorized=permission in _AUTHORIZED_PERMISSIONS,
            )
        )
    return decisions


def _apply_actor_verification(
    decisions: list[ActorVerificationDecision],
    config: DispatcherConfig,
) -> list[Task]:
    """権限不足のタスクを起動候補から除外し、`config.apply`時のみ
    `status:blocked-human-review`へエスカレーションする。"""
    authorized_tasks = []
    for decision in decisions:
        if decision.is_authorized:
            authorized_tasks.append(decision.task)
            continue
        if config.apply:
            apply_human_review_escalation(
                decision.task.issue_number,
                decision.task.status_labels,
                f"actor権限検証: `status:queued`ラベルを付与したユーザー "
                f"`{decision.actor}` のリポジトリ権限は `{decision.permission}` であり、"
                f"必要な権限（triage以上）を満たしていません。\n"
                f"自動起動をスキップし、`status:blocked-human-review` に変更しました。"
                f"意図した操作であれば、権限を持つユーザーが再度 `status:queued` を"
                f"付与してください。",
            )
    return authorized_tasks
