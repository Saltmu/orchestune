"""外部実行（クラウド等）の停止確認と、停止未確認時の枠保持判定（#1154）。

台帳（`active_worktrees`）から外部実行のエントリを外してよいのは、providerが
当該実行の「再開不能な終端状態」を返した場合だけである。PR・ラベル・成果物の
完了判定（`DispatchTarget.completion_status`）は成果物の判断であり、外部プロセスが
まだコードを実行し得るかどうかの証拠にはならない。

ここで得た状態は1回の操作内の観測に限り、RunStateへ「停止済み」として永続化しない。
状態取得の失敗・未対応・照合不能は`unknown`であり、停止確認として扱わない。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from orchestune.dispatch.attempt_record import read_attempt
from orchestune.dispatch.config import DispatcherConfig
from orchestune.dispatch.cycle_events import ExternalExecutionHeldCompletion
from orchestune.issue_notice import post_notice_if_changed
from orchestune.labels import StatusLabel
from orchestune.ledger.active_records import ActiveWorktree
from orchestune.ledger.escalation import apply_human_review_escalation
from orchestune.ledger.external_stop_receipts import matching_confirmation
from orchestune.ledger.run_state import RunState
from orchestune.targets.contracts import DispatchHandle

RuntimeState = Literal["running", "stopped", "unknown"]
HoldReason = Literal["timeout", "stale", "completion"]

ACTION_EXTERNAL_EXECUTION_HELD = "external_execution_held"
HELD_NOTICE_KIND = "external-execution-held"


@dataclass(frozen=True, slots=True)
class ExternalExecutionHold:
    """停止未確認のため、台帳エントリ・ハンドル・枠を保持すべき判定。"""

    issue_number: int
    reason: HoldReason
    runtime_state: RuntimeState
    claim_id: str | None
    launch_attempt_id: str | None
    external_id: str | None

    def event(self, *, subtask_id: str = "") -> ExternalExecutionHeldCompletion:
        """イベント化する。トークン・認証情報は含めない。"""
        return ExternalExecutionHeldCompletion(
            issue_number=self.issue_number,
            subtask_id=subtask_id,
            reason=self.reason,
            claim_id=self.claim_id,
            launch_attempt_id=self.launch_attempt_id,
            external_id=self.external_id,
            runtime_state=self.runtime_state,
        )


def is_external_execution(active: ActiveWorktree) -> bool:
    external_id = active.launch.external_id
    return external_id is not None and not external_id.startswith("recovered-pr:")


def _attempt_matches_provider(
    active: ActiveWorktree, config: DispatcherConfig, *, strict: bool = False
) -> bool:
    """現在の設定だけを根拠に別providerの同名IDを問い合わせない。

    durable attemptを持つtargetでは、保存済みの起動記録（target・外部ID）が
    現在のtargetと一致する場合だけ照合する。対応が不明な旧記録は`unknown`扱い。
    """
    target = config.dispatch_target
    assert target is not None
    if not strict and target.launch_capabilities.durable_attempt is not True:
        return True
    try:
        attempt = read_attempt(config.resolved_forge, active.core.issue_number)
    except Exception:
        return False
    return (
        attempt is not None
        and attempt.target == target.target_name
        and attempt.external_id == active.launch.external_id
        and (
            not strict
            or (
                active.launch.launch_attempt_id is not None
                and attempt.phase == "launched"
                and attempt.branch == active.core.branch
                and attempt.started_at == active.launch.started_at
            )
        )
        and (
            active.launch.launch_attempt_id is None
            or attempt.attempt_id == active.launch.launch_attempt_id
        )
    )


def observe_runtime_state(
    active: ActiveWorktree, config: DispatcherConfig
) -> RuntimeState:
    """外部実行の現在の実行状態を1回観測する。例外・不正値は`unknown`。"""
    launch = active.launch
    target = config.dispatch_target
    if launch.external_id is None or target is None:
        return "unknown"
    if not _attempt_matches_provider(active, config):
        return "unknown"
    handle = DispatchHandle(
        pid=launch.pid,
        external_id=launch.external_id,
        external_url=launch.external_url,
        branch_name=active.core.branch,
        issue_number=active.core.issue_number,
        started_at=launch.started_at,
        launch_attempt_id=launch.launch_attempt_id,
    )
    try:
        status = target.execution_status(handle)
    except Exception:
        return "unknown"
    return status if status in ("running", "stopped") else "unknown"


def probe_runtime_status(config: DispatcherConfig, external_id: str) -> RuntimeState:
    """整合性観測用のIDだけの問い合わせ。失敗は例外にせず`unknown`にする。"""
    target = config.dispatch_target
    if target is None:
        return "unknown"
    try:
        status = target.execution_status(DispatchHandle(external_id=external_id))
    except Exception:
        return "unknown"
    return status if status in ("running", "stopped") else "unknown"


def hold_if_not_stopped(
    active: ActiveWorktree,
    config: DispatcherConfig,
    reason: HoldReason,
    *,
    observe: Callable[[ActiveWorktree, DispatcherConfig], RuntimeState] | None = None,
    state: RunState | None = None,
    repository_id: str | None = None,
) -> ExternalExecutionHold | None:
    """外部実行が停止確認できなければ保持判定を返す。回収してよければ`None`。

    ローカル実行（外部IDなし）は既存のPID検査に委ね、ここでは止めない。
    """
    if not is_external_execution(active):
        return None
    runtime = (observe or observe_runtime_state)(active, config)
    if runtime == "stopped":
        return None
    if (
        runtime == "unknown"
        and state is not None
        and repository_id is not None
        and matching_confirmation(state, active, repository_id)
    ):
        return None
    return ExternalExecutionHold(
        issue_number=active.core.issue_number,
        reason=reason,
        runtime_state=runtime,
        claim_id=active.claim.claim_id,
        launch_attempt_id=active.launch.launch_attempt_id,
        external_id=active.launch.external_id,
    )


def _hold_notice(hold: ExternalExecutionHold) -> str:
    return (
        f"外部実行の停止を確認できないため、自動回収（{hold.reason}）を見送り、"
        "status:blocked-human-reviewへ送りました。\n"
        f"実行状態: `{hold.runtime_state}`（external_id: `{hold.external_id}`）\n"
        "台帳（active_worktrees）と実行ハンドルは保持され、並行枠も占有されたままです。"
        "自動での再投入は行いません。\n"
        "クラウド側の実行状態と成果物を確認し、停止・完了を確認してから復旧してください。"
    )


def send_hold_to_human_review(
    hold: ExternalExecutionHold,
    status_labels: tuple[str, ...],
    config: DispatcherConfig,
) -> bool:
    """保持時に`status:blocked-human-review`へ送る。台帳は一切変更しない。

    すでに人間確認ラベルが付いている場合は通知を重複させない。
    API失敗でも台帳は保持される（呼び出し側は解放しない）。
    """
    if StatusLabel.BLOCKED_HUMAN_REVIEW in status_labels:
        return False
    try:
        apply_human_review_escalation(
            hold.issue_number,
            status_labels,
            _hold_notice(hold),
            forge=config.resolved_forge,
        )
    except Exception as exc:  # noqa: BLE001 - 次サイクルで同条件を再評価する
        print(
            f"Warning: failed to send the held external execution of issue "
            f"#{hold.issue_number} to human review: {exc}",
            file=sys.stderr,
        )
        return False
    return True


def _completed_hold_notice(hold: ExternalExecutionHold) -> str:
    return (
        "成果物（PR/Outcome）の完了を検出しましたが、外部実行の停止を確認できないため、"
        "台帳（active_worktrees）と実行ハンドルを保持し、並行枠も占有したままにしています。\n"
        f"実行状態: `{hold.runtime_state}`（external_id: `{hold.external_id}`）\n"
        "完了結果を保つためラベルは変更していません。"
        "providerが停止を返すと次のGCサイクルで回収されます。"
        "停止を確認できないtarget（Cloud Routine等）は、クラウド側で停止を確認してから復旧してください。"
    )


def notify_completed_hold(
    hold: ExternalExecutionHold, config: DispatcherConfig
) -> None:
    """成果物完了後の保持をIssueへ残す。完了結果のラベルは上書きしない。

    毎サイクル再評価されるため、本文が前回と同じなら投稿しない。
    """
    post_notice_if_changed(
        config.resolved_forge,
        hold.issue_number,
        HELD_NOTICE_KIND,
        _completed_hold_notice(hold),
    )
