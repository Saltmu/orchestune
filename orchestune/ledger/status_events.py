"""`status:*`ラベルのEventモデル（#1219 Phase 3）を表す純粋モジュール。

本番の状態遷移は`transition_status_label`の呼び出し箇所・直接Forge操作・
ラベル不変の完了確定に分散しており、Eventを経由しない。このモジュールは
それらを1つのEvent列として表し、遷移・実行identity・retry予約・予算を副作用なしで
判定する。本番の判定を`apply_event`へ置き換えるものではなく、本番関数との対応は
`tests/test_status_events.py`の適合テストで機械的に結びつける。

backoff付きretry（early death / review timeout）の上限・予約の再利用・backoffの
意味は`orchestune.dispatch.retry_policy`だけが所有する。ledgerはdispatchへ依存
できないため、その判定は`BudgetLimits.plan_backoff`として呼び出し側が注入する
（テストでは`plan_retry`から作った本番アダプターを渡す）。

モデルの`pending_operation`・`confirmed_operations`はテスト上の区切りであり、
本番に存在する永続journalではない（`restart`で失われる）。永続されるのは
ラベル・retry予約（`run_state.json`）・永続予算（Issue本文・outcome）だけである。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

from orchestune.bounded_limit import exceeds_limit
from orchestune.labels import StatusLabel
from orchestune.ledger.status_machine import (
    ALLOWED_TRANSITIONS,
    LABEL_ROLES,
    LabelRole,
    TransitionPlan,
    lifecycle_labels,
)

#: ベースブランチ由来のCI失敗を示す保留マーカー（`status:*`ではない）。
BASE_BRANCH_RED_LABEL = "ci:base-branch-red"
_STATUS_PREFIX = "status:"


class Event(StrEnum):
    """本番の状態変化を表すEvent。"""

    QUEUE = "queue"
    BLOCK = "block"
    LAUNCH = "launch"
    COMPLETE = "complete"
    NOT_NEEDED = "not_needed"
    REQUEUE = "requeue"
    RECLAIM = "reclaim"
    RECOMPUTE = "recompute"
    ESCALATE = "escalate"
    #: done → queued。Integratorの仮マージCI失敗による差し戻し。
    MERGE_REVERT = "merge_revert"
    #: not-needed → queued。対応不要の独立レビューが却下した完了の取り消し。
    REVIEW_REJECT = "review_reject"
    #: lifecycleを変えない補助ラベルの付与（external-lock）。
    HOLD = "hold"
    #: lifecycleを変えない補助ラベル・保留マーカーの解除。
    RELEASE_HOLD = "release_hold"
    COMPLETE_WITHOUT_LABEL = "complete_without_label"


class Kind(StrEnum):
    """Eventの変種。補助ラベルの操作・予算の種類・完了証拠の種類を区別する。"""

    PLAIN = "plain"
    #: 対話claim。target以外の`status:*`（補助ラベルを含む）をすべて除去する。
    CLAIM = "claim"
    #: 既存のlaunch attemptの回復（予約の確定を伴わない）。
    RECOVERY = "recovery"
    RECOMPUTE = "recompute"
    BASE_BRANCH_RED = "base_branch_red"
    EXTERNAL_LOCK = "external_lock"
    EARLY_DEATH = "early_death"
    REVIEW_TIMEOUT = "review_timeout"
    MANUAL_MERGE = "manual_merge"
    #: replanの世代置換。target以外の`status:*`をすべて除去する。
    REPLAN = "replan"
    #: 同一cycleの`record_completion`。
    CYCLE = "cycle"
    #: 検証済み先行マージ（dry runの完了集合）。
    PRIOR_MERGE = "prior_merge"
    #: outcome recordだけによるnot-needed。in-progressを外し、ラベルは付けない。
    NOT_NEEDED_OUTCOME = "not_needed_outcome"


class Stage(StrEnum):
    """未確定operationの中断点。"""

    #: 予算の予約だけが永続化された（ラベルは不変）。
    RESERVED = "reserved"
    #: targetの付与と台帳確定コールバックまで完了した（旧ラベルは残る）。
    LABEL_ADDED = "label_added"


@dataclass(frozen=True)
class ExecutionIdentity:
    """本番のlaunch fact / handleに対応する確定した起動。"""

    launch_id: str


@dataclass(frozen=True)
class IndeterminateExecution:
    """起動前・結果不明・handle欠如・同一Issueの複数起動など、不確定な実行。"""

    reason: str


Execution = ExecutionIdentity | IndeterminateExecution


@dataclass(frozen=True)
class ReclaimState:
    """GC回収の予算。backoffを持たないため`retry_at`はない。"""

    count: int = 0
    pending: bool = False


@dataclass(frozen=True)
class BackoffState:
    """backoff付きretryの予約（本番の`retry_policy.RetryState`と同じ形）。"""

    count: int = 0
    retry_at: float = 0.0
    pending: bool = False


@dataclass(frozen=True)
class RetryStates:
    """`run_state.json`の`task_reclaim_counts`に永続するローカル予算。"""

    reclaim: ReclaimState = ReclaimState()
    early_death: BackoffState = BackoffState()
    review_timeout: BackoffState = BackoffState()


@dataclass(frozen=True)
class BudgetCounts:
    """予約を持たない永続予算（Issue本文のrecovery counters / outcome record）。"""

    recompute: int = 0
    base_branch_red: int = 0


#: backoff付きretryの判定。次の予約状態（再開ならそのまま）か、上限到達ならNone。
BackoffPlanner = Callable[["Kind", BackoffState, float], BackoffState | None]


@dataclass(frozen=True)
class BudgetLimits:
    """各予算の上限。既定値は持たず、呼び出し側が本番の設定から作る。"""

    max_task_reclaims: int
    max_recompute_retries: int
    base_branch_red_attempts: int
    plan_backoff: BackoffPlanner


@dataclass(frozen=True)
class PendingOperation:
    """予約・適用・確定の途中で止まったoperation。"""

    operation: str
    event: Event
    kind: Kind
    stage: Stage


@dataclass(frozen=True)
class TaskModel:
    """1タスク分のモデル状態。"""

    lifecycle: frozenset[StatusLabel]
    auxiliary: frozenset[str] = frozenset()
    completion_confirmed: bool = False
    persistent_completion: bool = False
    execution_identity: Execution | None = None
    retired_execution_identities: frozenset[Execution] = frozenset()
    counts: BudgetCounts = BudgetCounts()
    retries: RetryStates = RetryStates()
    pending_operation: PendingOperation | None = None
    confirmed_operations: frozenset[str] = frozenset()
    ledger_epoch: int = 0

    @classmethod
    def from_labels(cls, labels: tuple[str, ...] | frozenset[str]) -> TaskModel:
        """ラベル集合から作る。lifecycle・補助以外のラベルはモデルに含めない。"""
        auxiliary = frozenset(
            label
            for label in labels
            if label == BASE_BRANCH_RED_LABEL
            or LABEL_ROLES.get(label) is LabelRole.AUXILIARY  # type: ignore[call-overload]
            or (label.startswith(_STATUS_PREFIX) and label not in LABEL_ROLES)
        )
        return cls(lifecycle=lifecycle_labels(labels), auxiliary=auxiliary)

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(self.lifecycle) | self.auxiliary

    @property
    def execution_active(self) -> bool:
        return self.execution_identity is not None

    @property
    def completion_done(self) -> bool:
        """完了証拠（FINALラベル、またはラベル不変の完了確定）があるか。"""
        return self.completion_confirmed or bool(self.lifecycle & _FINAL)


@dataclass(frozen=True)
class EventInput:
    """`apply_event`への入力。

    `execution`は実行に紐づくeventの起動identity（Noneは「起動を問わない」
    未保護のevent）、`operation`は再送を識別するidentity、`stop_after`は
    中断点を表す。
    """

    event: Event
    kind: Kind = Kind.PLAIN
    execution: Execution | None = None
    operation: str | None = None
    now: float = 0.0
    stop_after: Stage | None = None


@dataclass(frozen=True)
class Applied:
    """適用されたevent。`plan`はlifecycleの追加・除去（不変ならNone）。"""

    state: TaskModel
    plan: TransitionPlan | None
    add_labels: tuple[str, ...] = ()
    remove_labels: tuple[str, ...] = ()
    escalated: bool = False


@dataclass(frozen=True)
class Rejected:
    reason: str


@dataclass(frozen=True)
class NoOp:
    reason: str


Result = Applied | Rejected | NoOp


@dataclass(frozen=True)
class EventSpec:
    """1つの(Event, Kind)の遷移規則。

    `target`がNoneのeventはlifecycleを変えない。`sources`は保持していてよい
    lifecycle（`target`自身は常に許可）、`initial`はlifecycleを1つも持たない
    状態から適用できるか。
    """

    target: StatusLabel | None
    sources: frozenset[StatusLabel]
    initial: bool = True
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()
    strip: bool = False


_ACTIVE = frozenset({StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS})
_ESCALATION = frozenset(
    {StatusLabel.BLOCKED_HUMAN_REVIEW, StatusLabel.MANUAL_MERGE_REQUIRED}
)
_FINAL = frozenset({StatusLabel.DONE, StatusLabel.NOT_NEEDED})
_ALL = _ACTIVE | _ESCALATION | _FINAL
_RUNNING = frozenset({StatusLabel.IN_PROGRESS, StatusLabel.BLOCKED})
_RC = frozenset({StatusLabel.BLOCKED_RECOMPUTE})
_RED = frozenset({BASE_BRANCH_RED_LABEL})
_EL = frozenset({StatusLabel.EXTERNAL_LOCK})


def _spec(
    target: StatusLabel | None,
    sources: frozenset[StatusLabel],
    initial: bool = True,
    **effects: object,
) -> EventSpec:
    return EventSpec(target, sources, initial, **effects)  # type: ignore[arg-type]


_Q, _B, _P = StatusLabel.QUEUED, StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS
_H = StatusLabel.BLOCKED_HUMAN_REVIEW
_E = Event
_K = Kind

#: 全(Event, Kind)の遷移規則。ここにない組は不正な入力（ValueError）。
EVENT_SPECS: Mapping[tuple[Event, Kind], EventSpec] = MappingProxyType(
    {
        (_E.QUEUE, _K.PLAIN): _spec(_Q, frozenset({_B})),
        (_E.QUEUE, _K.RECOMPUTE): _spec(_Q, frozenset({_B}), remove=_RC),
        (_E.QUEUE, _K.BASE_BRANCH_RED): _spec(_Q, frozenset({_B}), remove=_RED),
        (_E.QUEUE, _K.EXTERNAL_LOCK): _spec(_Q, frozenset(), remove=_EL),
        (_E.BLOCK, _K.PLAIN): _spec(_B, frozenset({_Q, _P})),
        (_E.BLOCK, _K.RECOMPUTE): _spec(_B, frozenset({_Q, _P}), add=_RC),
        (_E.BLOCK, _K.BASE_BRANCH_RED): _spec(_B, frozenset({_Q, _P}), add=_RED),
        (_E.LAUNCH, _K.PLAIN): _spec(_P, frozenset({_Q, _B})),
        (_E.LAUNCH, _K.CLAIM): _spec(_P, frozenset({_Q, _B}), strip=True),
        (_E.LAUNCH, _K.RECOVERY): _spec(_P, frozenset({_Q, _B})),
        (_E.COMPLETE, _K.PLAIN): _spec(StatusLabel.DONE, _ACTIVE),
        (_E.NOT_NEEDED, _K.PLAIN): _spec(StatusLabel.NOT_NEEDED, _ACTIVE),
        (_E.NOT_NEEDED, _K.REPLAN): _spec(
            StatusLabel.NOT_NEEDED, _ACTIVE | _ESCALATION, strip=True
        ),
        (_E.REQUEUE, _K.EARLY_DEATH): _spec(_Q, _RUNNING, initial=False),
        (_E.REQUEUE, _K.REVIEW_TIMEOUT): _spec(_Q, _RUNNING, initial=False),
        (_E.REQUEUE, _K.RECOVERY): _spec(_Q, _RUNNING, initial=False),
        (_E.RECLAIM, _K.PLAIN): _spec(_Q, _RUNNING, initial=False),
        (_E.RECOMPUTE, _K.PLAIN): _spec(None, frozenset({_P})),
        (_E.ESCALATE, _K.PLAIN): _spec(_H, _ACTIVE | {StatusLabel.NOT_NEEDED}),
        (_E.ESCALATE, _K.MANUAL_MERGE): _spec(
            StatusLabel.MANUAL_MERGE_REQUIRED, frozenset({_P}), initial=False
        ),
        (_E.MERGE_REVERT, _K.PLAIN): _spec(
            _Q, frozenset({StatusLabel.DONE}), initial=False
        ),
        (_E.REVIEW_REJECT, _K.PLAIN): _spec(_Q, frozenset({StatusLabel.NOT_NEEDED})),
        (_E.HOLD, _K.EXTERNAL_LOCK): _spec(None, _ACTIVE, add=_EL),
        (_E.RELEASE_HOLD, _K.RECOMPUTE): _spec(None, _ALL, remove=_RC),
        (_E.RELEASE_HOLD, _K.BASE_BRANCH_RED): _spec(None, _ALL, remove=_RED),
        (_E.RELEASE_HOLD, _K.EXTERNAL_LOCK): _spec(None, _ALL, remove=_EL),
        (_E.COMPLETE_WITHOUT_LABEL, _K.CYCLE): _spec(None, _ALL),
        (_E.COMPLETE_WITHOUT_LABEL, _K.PRIOR_MERGE): _spec(None, _ALL),
        (_E.COMPLETE_WITHOUT_LABEL, _K.NOT_NEEDED_OUTCOME): _spec(
            None, frozenset({_P}), remove=frozenset({_P})
        ),
    }
)

#: FINAL / ラベル不変の完了確定から再びACTIVEへ戻せるevent。
_COMPLETION_WITHDRAWALS = frozenset({Event.MERGE_REVERT, Event.REVIEW_REJECT})
#: lifecycleの遷移先にかかわらず実行を終えるevent。
_EXECUTION_ENDING = frozenset(
    {Event.RECLAIM, Event.REQUEUE, Event.COMPLETE_WITHOUT_LABEL}
)
#: 予算超過でエスカレーションへ切り替わる(Event, Kind)。
_BUDGETED = frozenset(
    {
        (Event.RECLAIM, Kind.PLAIN),
        (Event.REQUEUE, Kind.EARLY_DEATH),
        (Event.REQUEUE, Kind.REVIEW_TIMEOUT),
        (Event.BLOCK, Kind.BASE_BRANCH_RED),
        (Event.RECOMPUTE, Kind.PLAIN),
    }
)


@dataclass(frozen=True)
class _Decision:
    """予算判定後の実効規則と、予約後のモデル状態。"""

    spec: EventSpec
    state: TaskModel
    escalated: bool = False


def apply_event(state: TaskModel, event: EventInput, limits: BudgetLimits) -> Result:
    """`event`を`state`に適用した結果を、副作用なしで返す。"""
    spec = EVENT_SPECS.get((event.event, event.kind))
    if spec is None:
        raise ValueError(f"unknown kind {event.kind} for event {event.event}")
    if event.stop_after is not None and event.operation is None:
        raise ValueError("a stopped event needs an operation identity")
    refused = _refuse_operation(state, event) or _refuse_execution(state, event)
    if refused is not None:
        return refused
    noop = _noop(state, event)
    if noop is not None:
        return noop
    refused = _refuse_source(state, event, spec)
    if refused is not None:
        return refused
    decision = _decide_budget(state, event, spec, limits)
    if isinstance(decision, NoOp):
        return decision
    return _apply(decision, event)


def _refuse_operation(state: TaskModel, event: EventInput) -> Rejected | None:
    pending = state.pending_operation
    if pending is None:
        return None
    if event.operation != pending.operation:
        return Rejected("operation-pending")
    if (event.event, event.kind) != (pending.event, pending.kind):
        return Rejected("operation-mismatch")
    return None


def _refuse_execution(state: TaskModel, event: EventInput) -> Rejected | None:
    if event.event is Event.LAUNCH:
        return _refuse_launch(state, event.execution)
    if event.execution is not None and event.execution != state.execution_identity:
        return Rejected("stale-execution")
    return None


def _refuse_launch(state: TaskModel, execution: Execution | None) -> Rejected | None:
    """`CycleContext.record_launch`と同じ順序で起動を検証する。"""
    if not isinstance(execution, ExecutionIdentity):
        return Rejected("invalid-launch")
    if state.completion_done or state.lifecycle & _ESCALATION:
        return Rejected("terminal-state")
    current = state.execution_identity
    if isinstance(current, IndeterminateExecution):
        return Rejected("launch-mismatch")
    if current is not None and current != execution:
        return Rejected("launch-mismatch")
    return None


def _noop(state: TaskModel, event: EventInput) -> NoOp | None:
    if event.operation is not None and event.operation in state.confirmed_operations:
        return NoOp("duplicate-operation")
    if event.event is Event.LAUNCH and state.execution_identity == event.execution:
        return NoOp("same-launch")
    if event.event is Event.COMPLETE_WITHOUT_LABEL and state.completion_done:
        return NoOp("already-complete")
    return None


def _refuse_source(
    state: TaskModel, event: EventInput, spec: EventSpec
) -> Rejected | None:
    if (
        state.completion_confirmed
        and spec.target in _ACTIVE
        and event.event not in _COMPLETION_WITHDRAWALS
    ):
        return Rejected("completion-confirmed")
    held = state.lifecycle
    if not held:
        return None if spec.initial else Rejected("no-lifecycle")
    extra = held - {spec.target} if spec.target is not None else held
    offending = extra - spec.sources
    if not offending:
        return None
    if offending & _ESCALATION:
        return Rejected("escalation-held")
    if offending & _FINAL:
        return Rejected("final-held")
    return Rejected("invalid-source")


def _decide_budget(
    state: TaskModel, event: EventInput, spec: EventSpec, limits: BudgetLimits
) -> _Decision | NoOp:
    key = (event.event, event.kind)
    if key not in _BUDGETED:
        return _Decision(spec, state)
    if event.event is Event.RECLAIM:
        return _decide_reclaim(state, spec, limits)
    if event.event is Event.RECOMPUTE:
        return _decide_recompute(state, spec, limits)
    if event.event is Event.BLOCK:
        return _decide_base_branch_red(state, spec, limits)
    return _decide_backoff(state, event, spec, limits)


def _escalation(spec: EventSpec, remove: frozenset[str] = frozenset()) -> EventSpec:
    return replace(spec, target=_H, add=frozenset(), remove=spec.remove | remove)


def _decide_reclaim(
    state: TaskModel, spec: EventSpec, limits: BudgetLimits
) -> _Decision:
    """`_resolve_reclaim_count`と`_refresh_reclaim`の規則。

    予約中（pending）なら同じ回数を再利用し、そうでなければ1つ進める。
    上限の判定は予約を再利用する場合も行う。超過時も回数は記録する。
    """
    reclaim = state.retries.reclaim
    count = reclaim.count if reclaim.pending else reclaim.count + 1
    retries = replace(state.retries, reclaim=ReclaimState(count=count, pending=True))
    reserved = replace(state, retries=retries)
    if exceeds_limit(count, limits.max_task_reclaims):
        return _Decision(_escalation(spec), reserved, escalated=True)
    return _Decision(spec, reserved)


def _decide_recompute(
    state: TaskModel, spec: EventSpec, limits: BudgetLimits
) -> _Decision | NoOp:
    """`_decide_footprint_deviation_outcome`の予算規則。"""
    if StatusLabel.FORCE_SERIAL in state.auxiliary:
        return NoOp("already-forced-serial")
    count = state.counts.recompute
    if exceeds_limit(count + 1, limits.max_recompute_retries):
        forced = replace(spec, add=frozenset({StatusLabel.FORCE_SERIAL}))
        return _Decision(forced, state, escalated=True)
    counts = replace(state.counts, recompute=count + 1)
    return _Decision(spec, replace(state, counts=counts))


def _decide_base_branch_red(
    state: TaskModel, spec: EventSpec, limits: BudgetLimits
) -> _Decision:
    """attempt（今回を含む連続回数）が上限に達したらエスカレーションする。"""
    attempt = state.counts.base_branch_red + 1
    counted = replace(state, counts=replace(state.counts, base_branch_red=attempt))
    if attempt >= limits.base_branch_red_attempts:
        return _Decision(_escalation(spec, _RED), counted, escalated=True)
    return _Decision(spec, counted)


def _decide_backoff(
    state: TaskModel, event: EventInput, spec: EventSpec, limits: BudgetLimits
) -> _Decision:
    early_death = event.kind is Kind.EARLY_DEATH
    retries = state.retries
    current = retries.early_death if early_death else retries.review_timeout
    planned = limits.plan_backoff(event.kind, current, event.now)
    if planned is None:
        return _Decision(_escalation(spec), state, escalated=True)
    if early_death:
        retries = replace(retries, early_death=planned)
    else:
        retries = replace(retries, review_timeout=planned)
    return _Decision(spec, replace(state, retries=retries))


def _apply(decision: _Decision, event: EventInput) -> Applied:
    spec, state = decision.spec, decision.state
    plan = (
        None
        if spec.target is None
        else TransitionPlan(
            add=spec.target,
            remove=tuple(sorted(state.lifecycle - {spec.target})),
        )
    )
    removed = _removed_labels(state, spec)
    if event.stop_after is Stage.RESERVED:
        stopped = _stopped(state, event, Stage.RESERVED)
        return Applied(stopped, plan, escalated=decision.escalated)
    added = _with_target(state, spec)
    if event.stop_after is Stage.LABEL_ADDED:
        stopped = _stopped(_settle_on_label(added, event), event, Stage.LABEL_ADDED)
        return Applied(stopped, plan, tuple(sorted(spec.add)), (), decision.escalated)
    final = _finish(_settle_on_label(added, event), event, spec, removed)
    return Applied(
        final, plan, tuple(sorted(spec.add)), tuple(sorted(removed)), decision.escalated
    )


def _removed_labels(state: TaskModel, spec: EventSpec) -> frozenset[str]:
    """`plan`の外で直接除去するラベル（補助ラベル・保留マーカー・in-progress）。"""
    removed = set(spec.remove & state.labels)
    if spec.strip:
        removed |= {
            label
            for label in state.auxiliary
            if label.startswith(_STATUS_PREFIX) and label != spec.target
        }
    return frozenset(removed)


def _with_target(state: TaskModel, spec: EventSpec) -> TaskModel:
    lifecycle = state.lifecycle
    if spec.target is not None:
        lifecycle = lifecycle | {spec.target}
    return replace(state, lifecycle=lifecycle, auxiliary=state.auxiliary | spec.add)


def _stopped(state: TaskModel, event: EventInput, stage: Stage) -> TaskModel:
    assert event.operation is not None
    pending = PendingOperation(event.operation, event.event, event.kind, stage)
    return replace(state, pending_operation=pending)


def _settle_on_label(state: TaskModel, event: EventInput) -> TaskModel:
    """台帳確定コールバック（targetの付与直後）で確定する予約を解除する。"""
    retries = state.retries
    if event.event is Event.REQUEUE and event.kind is Kind.EARLY_DEATH:
        retries = replace(
            retries, early_death=replace(retries.early_death, pending=False)
        )
    if event.event is Event.REQUEUE and event.kind is Kind.REVIEW_TIMEOUT:
        retries = replace(
            retries, review_timeout=replace(retries.review_timeout, pending=False)
        )
    if event.event is Event.LAUNCH and event.kind is Kind.PLAIN:
        # `_record_successful_launch`は回収とearly deathの予約を確定する。
        retries = replace(
            retries,
            reclaim=replace(retries.reclaim, pending=False),
            early_death=replace(retries.early_death, pending=False),
        )
    return replace(state, retries=retries)


def _finish(
    state: TaskModel, event: EventInput, spec: EventSpec, removed: frozenset[str]
) -> TaskModel:
    lifecycle = state.lifecycle
    if spec.target is not None:
        lifecycle = frozenset({spec.target})
    lifecycle = lifecycle - removed
    retries = state.retries
    if event.event is Event.RECLAIM:
        retries = replace(retries, reclaim=replace(retries.reclaim, pending=False))
    confirmed = state.confirmed_operations
    if event.operation is not None:
        confirmed = confirmed | {event.operation}
    return _with_execution(
        replace(
            state,
            lifecycle=lifecycle,
            auxiliary=state.auxiliary - removed,
            retries=retries,
            pending_operation=None,
            confirmed_operations=confirmed,
        ),
        event,
        spec,
    )


def _with_execution(state: TaskModel, event: EventInput, spec: EventSpec) -> TaskModel:
    """実行identityと完了証拠を更新する。"""
    if event.event is Event.LAUNCH:
        return replace(state, execution_identity=event.execution)
    if event.event in _COMPLETION_WITHDRAWALS:
        state = replace(state, completion_confirmed=False, persistent_completion=False)
    if event.event is Event.COMPLETE_WITHOUT_LABEL:
        persistent = event.kind in (Kind.PRIOR_MERGE, Kind.NOT_NEEDED_OUTCOME)
        state = replace(
            state, completion_confirmed=True, persistent_completion=persistent
        )
    # 回収・再投入はエスカレーションへ切り替わっても実行を終える（プロセス停止・
    # active entryの解放）。通常のエスカレーションは起動継続中の状態を表す。
    base_spec = EVENT_SPECS.get((event.event, event.kind))
    base_target = base_spec.target if base_spec is not None else spec.target
    ends_execution = event.event in _EXECUTION_ENDING or (
        base_target is not None and base_target not in _ESCALATION | {_P}
    )
    if not ends_execution or state.execution_identity is None:
        return state
    return replace(
        state,
        execution_identity=None,
        retired_execution_identities=state.retired_execution_identities
        | {state.execution_identity},
    )


def restart(state: TaskModel, *, ledger_loss: bool) -> TaskModel:
    """プロセスの再起動。モデルのoperationは永続しないので失われる。

    `ledger_loss`はローカル台帳（`run_state.json`）の消失で、ローカル予算と
    実行identity（active worktreeの記録）を失い、台帳の世代を進める。ラベルと
    永続予算（recompute / base-branch-red）は保持する。
    """
    restarted = replace(
        state,
        pending_operation=None,
        confirmed_operations=frozenset(),
        completion_confirmed=state.completion_confirmed and state.persistent_completion,
    )
    if not ledger_loss:
        return restarted
    return replace(
        restarted,
        retries=RetryStates(),
        execution_identity=None,
        ledger_epoch=state.ledger_epoch + 1,
    )


__all__ = [
    "ALLOWED_TRANSITIONS",
    "BASE_BRANCH_RED_LABEL",
    "EVENT_SPECS",
    "Applied",
    "BackoffPlanner",
    "BackoffState",
    "BudgetCounts",
    "BudgetLimits",
    "Event",
    "EventInput",
    "EventSpec",
    "Execution",
    "ExecutionIdentity",
    "IndeterminateExecution",
    "Kind",
    "NoOp",
    "PendingOperation",
    "ReclaimState",
    "Rejected",
    "Result",
    "RetryStates",
    "Stage",
    "TaskModel",
    "apply_event",
    "restart",
]
