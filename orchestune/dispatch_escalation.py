"""status:blocked-human-reviewへの共通エスカレーション処理（act）。"""

from __future__ import annotations

import os
from collections.abc import Callable

from orchestune.dispatch_config import DispatcherConfig
from orchestune.dispatch_labels import transition_status_label
from orchestune.dispatch_rules import ActiveWorktreeRuleOutcome, CycleContext
from orchestune.dispatch_scoring import Task
from orchestune.dispatch_state import ActiveWorktree, RunState
from orchestune.forge import Forge, GitHubForge

_REMOVABLE_STATUS_LABELS = ("status:in-progress", "status:queued", "status:blocked")


def apply_human_review_escalation(
    issue_number: int,
    current_status_labels: tuple[str, ...],
    comment: str,
    forge: Forge | None = None,
    on_label_applied: Callable[[], None] | None = None,
) -> None:
    """現在保持しているstatus:*ラベル（in-progress/queued/blocked）を除去した上で
    status:blocked-human-reviewを付与し、理由をコメントする。

    空コミット完了・重複起動検知・CHANGES_REQUESTEDエスカレーションの3箇所で
    重複していたラベル遷移ロジックを集約したもの。`config.apply`によるゲーティング
    は呼び出し側の責務とし、この関数自体は常に無条件で実行する。

    #512/PR#520レビュー12巡目対応(Codex P1): `on_label_applied`が渡された場合、
    ラベル遷移が成功した直後（コメント投稿の前）に呼び出す。GC回収の呼び出し元は
    ここでローカルの帳簿を確定させる: コメント投稿だけが失敗したときにローカルを
    未確定のまま残すと、GitHub側は既にstatus:blocked-human-reviewなのに次サイクルも
    エスカレーションを再試行し続け、帳簿エントリがクオータを占有し続けてしまう。
    """
    forge = forge or GitHubForge()
    transition_status_label(
        forge,
        issue_number,
        "status:blocked-human-review",
        (label for label in _REMOVABLE_STATUS_LABELS if label in current_status_labels),
    )
    if on_label_applied is not None:
        on_label_applied()
    forge.add_comment(issue_number, comment)


def _decide_changes_requested_escalation(
    active_task: Task | None, changes_requested_subtask_ids: set[str]
) -> bool:
    """依存元PRがCHANGES_REQUESTEDを受けているかを副作用なしで判定する。"""
    if active_task and active_task.depends_on:
        return any(
            dep in changes_requested_subtask_ids for dep in active_task.depends_on
        )
    return False


def _apply_changes_requested_escalation(
    active: ActiveWorktree,
    active_task: Task,
    key: str,
    run_state: RunState,
    config: DispatcherConfig,
) -> dict:
    """依存元PRがCHANGES_REQUESTEDになったタスクを一時停止する
    （プロセスkill・githubラベル/コメント・run_state削除はすべてact）。"""
    if config.apply:
        if active.pid:
            try:
                os.kill(active.pid, 9)
            except OSError:
                pass
        apply_human_review_escalation(
            active.issue_number,
            ("status:in-progress",),
            "依存元PRが変更要求（Request Changes）を受けたため、スタックされたタスクを一時停止しました。",
            forge=config.resolved_forge,
        )
        del run_state.active_worktrees[key]
    return {
        "issue_number": active.issue_number,
        "subtask_id": active_task.subtask_id,
        "action": "escalated_due_to_changes_requested",
    }


def _rule_changes_requested(
    ctx: CycleContext, key: str, active: ActiveWorktree, active_task: Task | None
) -> ActiveWorktreeRuleOutcome | None:
    """#185: 自動リベースや逸脱判定の前に、CHANGES_REQUESTEDになった親を持つかチェックする。"""
    if not _decide_changes_requested_escalation(
        active_task, ctx.changes_requested_subtask_ids
    ):
        return None
    assert active_task is not None
    event = _apply_changes_requested_escalation(
        active, active_task, key, ctx.run_state, ctx.config
    )
    return ActiveWorktreeRuleOutcome(completion_event=event, terminal=True)
