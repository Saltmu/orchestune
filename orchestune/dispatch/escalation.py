"""status:blocked-human-reviewへの共通エスカレーション処理（act）。"""

from __future__ import annotations

import os

from orchestune.dependencies.assessment import (
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.rules import ActiveWorktreeRuleOutcome, _RuleExecutionContext
from orchestune.labels import StatusLabel
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.run_state import ActiveWorktree, RunState
from orchestune.task_metadata import TaskMetadata


def _decide_changes_requested_escalation(
    active_task: TaskMetadata | None,
    assessment: DependencyAssessment | None,
) -> bool:
    """依存元PRがCHANGES_REQUESTEDを受けているかを副作用なしで判定する。

    #869: 依存の識別・実効状態はいずれも`CycleContext.assess_dependencies`
    （#867の共通`DependencyAssessment`）から読む。未解決診断（`unresolved`）
    だけでは原因を確定できないためfalseへ倒すが、一部が未解決でも既に判明
    している解決済み依存のCHANGES_REQUESTEDは無視しない——停止すべき状況を
    見逃さないという既存の停止契約(#799)を維持する。
    """
    if active_task is None or assessment is None:
        return False
    return any(
        dep.state is DependencyState.CHANGES_REQUESTED for dep in assessment.resolved
    )


def _apply_changes_requested_escalation(
    active: ActiveWorktree,
    active_task: TaskMetadata,
    key: str,
    run_state: RunState,
    config: DispatcherConfig,
) -> dict:
    """依存元PRがCHANGES_REQUESTEDになったタスクを一時停止する
    （プロセスkill・githubラベル/コメント・run_state削除はすべてact）。"""
    if config.apply:
        if active.launch.pid:
            try:
                os.kill(active.launch.pid, 9)
            except OSError:
                pass
        apply_human_review_escalation(
            active.core.issue_number,
            (StatusLabel.IN_PROGRESS,),
            "依存元PRが変更要求（Request Changes）を受けたため、スタックされたタスクを一時停止しました。",
            forge=config.resolved_forge,
        )
        del run_state.active_worktrees[key]
    return {
        "issue_number": active.core.issue_number,
        "subtask_id": active_task.subtask_id,
        "action": "escalated_due_to_changes_requested",
    }


def _rule_changes_requested(
    ctx: _RuleExecutionContext,
    key: str,
    active: ActiveWorktree,
    active_task: TaskMetadata | None,
) -> ActiveWorktreeRuleOutcome | None:
    """#185: 自動リベースや逸脱判定の前に、CHANGES_REQUESTEDになった親を持つかチェックする。"""
    assessment = (
        None
        if active_task is None
        else ctx.assess_dependencies(active_task.issue_number)
    )
    if not _decide_changes_requested_escalation(active_task, assessment):
        return None
    assert active_task is not None
    event = _apply_changes_requested_escalation(
        active, active_task, key, ctx.run_state, ctx.config
    )
    return ActiveWorktreeRuleOutcome(completion_event=event, terminal=True)
