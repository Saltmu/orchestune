"""`CycleContext`の意味付きquery/record状態を所有する。

入力観測は構築時に値として取り込み、queryは不変な値だけを返す。`record_*`は
外部操作が成功した後の確認済み事実だけを実効状態へ反映し、外部I/Oは行わない。
依存先の識別は`dependency_resolution`、依存のライフサイクル判定は
`assess_dependency_lifecycle`の責務とする。
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.invariants.status import (
    PRIMARY_STATUS_LABELS,
    primary_status_labels,
)
from orchestune.dag.models import SubTask
from orchestune.dependencies.assessment import (
    DependencyAssessment,
)
from orchestune.dependencies.resolution import (
    TaskDependencies,
)
from orchestune.dispatch.cycle.task_view import (
    CycleLedger,
    CycleTaskView,
    LaunchFact,
    _LaunchState,
)
from orchestune.dispatch.status_repair_dependencies import task_lifecycle
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import ActiveWorktree
from orchestune.models import IssueRecord, PrRecord, Task
from orchestune.task_branch_resolution import TaskBranchResolution
from orchestune.task_metadata import CycleTask

# record_*が返す競合理由の固定文字列。
REASON_UNKNOWN_ISSUE = "unknown-issue"
REASON_TERMINAL_STATE = "terminal-state"
REASON_LAUNCH_MISMATCH = "launch-mismatch"
REASON_INVALID_LAUNCH = "invalid-launch"
REASON_STALE_OBSERVATION = "stale-observation"
REASON_INVALID_TRANSITION = "invalid-transition"
REASON_EXECUTION_MISMATCH = "execution-mismatch"

# record_launchが受け付ける`ActiveWorktree.launch_phase`。`prepared`/`unknown`/
# その他の値は不確定・起動前の予定であり、invalid-launchとして拒否する。
_ACCEPTED_LAUNCH_PHASES = (None, "launched")

# 起動を要求するIN_PROGRESSへの遷移先、エスカレーション先。
_ESCALATION_TARGETS = (
    StatusLabel.BLOCKED_HUMAN_REVIEW,
    StatusLabel.MANUAL_MERGE_REQUIRED,
)
_TERMINAL_LIFECYCLE = (TaskLifecycle.DONE, TaskLifecycle.NOT_NEEDED)

# 終端を表す主状態ラベル。確認済みの完了に後からラベルが追いつく遷移は許可する。
# 未完了タスクをこの遷移で完了にすることはできず、完了の記録は`record_completion`
# だけが行う。
_TERMINAL_TARGETS = (StatusLabel.DONE, StatusLabel.NOT_NEEDED)

# execution_active=trueが意味を持つ遷移先はIN_PROGRESSと起動継続中の
# エスカレーション状態だけ。
# それ以外(QUEUED/BLOCKED、DONE等の終端)への遷移でexecution_active=trueを
# 主張すると、record_completionが履歴として残したLaunchFactを使って
# 「DONEなのに実行中」という不変条件違反(consistency.invariants.status.
# _done_findings参照)を作り出せてしまう。ホワイトリストにより、将来の主状態追加も
# 安全側に倒す。
_EXECUTION_ACTIVE_ALLOWED_TARGETS = (
    StatusLabel.IN_PROGRESS,
    *_ESCALATION_TARGETS,
)

# 非終端主状態間の許可遷移。IN_PROGRESSへの遷移は
# 「既に記録済みの起動事実があり、execution_active=true」が別途必要——それは
# `_CycleState.record_transition`の実行時チェックが担い、この表自体には
# 含めない(遷移先として許可されているかどうかだけをここで表す)。
_NON_TERMINAL_TRANSITIONS: dict[str, frozenset[str]] = {
    StatusLabel.BLOCKED: frozenset({StatusLabel.QUEUED, StatusLabel.IN_PROGRESS}),
    StatusLabel.QUEUED: frozenset({StatusLabel.BLOCKED, StatusLabel.IN_PROGRESS}),
    StatusLabel.IN_PROGRESS: frozenset({StatusLabel.QUEUED, StatusLabel.BLOCKED}),
}


class RecordStatus(Enum):
    """record_*呼び出しの結果分類。"""

    APPLIED = "applied"
    NOOP = "noop"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class RecordResult:
    """record_*呼び出しの戻り値。CONFLICTのときのみ`reason`を持つ。"""

    status: RecordStatus
    reason: str | None = None


def _launch_fact_from_active(active: ActiveWorktree) -> LaunchFact:
    """`ActiveWorktree`から`LaunchFact`を作る。各handleは個別に健全化する。

    `_has_valid_launch_handle`は「pidかexternal_idの**いずれか**が使えるか」を
    見るOR判定なので、片方が有効なら不正なもう片方も一緒に通ってしまう
    (例: 有効なpid + `external_id=true`、有効なexternal_id + `pid=-1`)。
    そのまま型付きの`LaunchFact`へ載せると、`external_id is not None`だけを
    見る消費側がプロバイダAPIへbooleanを送ったり、pid消費側がプロセスグループ
    宛のpidを受け取ったりする。使えない値は`None`へ倒し、`LaunchFact`が常に
    宣言どおりの型であることを保証する。

    branch / worktree_pathは`_has_valid_launch_handle`が非空`str`を必須と
    しているため、ここへ到達する時点で健全な値であることが保証されている。
    """
    core = active.core
    launch = active.launch
    claim = active.claim
    return LaunchFact(
        issue_number=core.issue_number,
        branch=core.branch,
        worktree_path=core.worktree_path,
        pid=launch.pid if _is_usable_pid(launch.pid) else None,
        started_at=_usable_started_at_or_none(launch.started_at),
        external_id=_usable_str_or_none(launch.external_id),
        launch_attempt_id=_usable_str_or_none(launch.launch_attempt_id),
        owner_kind=claim.owner_kind,
        claim_id=_usable_str_or_none(claim.claim_id),
        reservation_kind=claim.reservation_kind,
    )


def _usable_started_at_or_none(value: object) -> float | None:
    """永続化された値が、有限の開始時刻として使える数値かどうか。

    `bool`は`int`のサブクラスなので除外する。`math.isfinite`は任意長の巨大整数
    （例: `10**1000`）を受け取るとC double型変換時に`OverflowError`を送出するため、
    型変換例外を安全に捕捉して`None`へ正規化する。
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def _is_non_empty_str(value: object) -> bool:
    """永続化された値が、識別子として使える非空の文字列かどうか。"""
    return isinstance(value, str) and value != ""


def _is_usable_pid(value: object) -> bool:
    """永続化された値が、生存確認に使える正の整数pidかどうか。

    POSIXでは`0`や負数のpidはプロセスグループ宛のシグナル送信という別の意味を
    持つため、識別子としては使えない。`bool`は`int`のサブクラスなので除外する。
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _usable_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _has_valid_launch_handle(active: ActiveWorktree) -> bool:
    """`record_launch`と同じinvalid-launch判定基準を使う。

    branch/worktree_pathが空、またはpidも(空文字列でない)external_idも無い
    (生存確認もプロバイダへの照会もできない)場合は、有効な起動として扱わない。
    `run_state.json`の`parse_active_worktrees`は空文字列の`external_id`を
    そのまま保持する(起動時attemptのパーサは空文字列を既に拒否しているが、
    この構築経路は素通りする)ため、`is not None`だけでは空文字列を有効な
    handleとして誤認する。

    構築時の初期観測にもこの基準を適用しないと、`recovery`がジャーナルも
    一致するPRも見つけられずhandle無しで復元した`ActiveWorktree`
    (`_build_restored_active_worktree`参照)を、誤って確定的なLaunchFactへ
    昇格させてしまう——`record_launch`が同じ入力をinvalid-launchとして
    拒否するのと矛盾する。

    branch / worktree_path / external_idはいずれも非空の`str`、pidは正の整数の
    みを有効と扱う。`parse_active_worktrees`は`run_state.json`の値を検証せず
    そのまま復元するため、`true`や数値・`0`・負数のような使えない値も届き得る
    ——`bool(...)`や`is not None`だけではこれらを有効なhandleとして誤認する。

    なお本判定は「pidかexternal_idの**いずれか**が使えるか」というOR判定なので、
    片方だけが有効な場合ももう片方の不正値ごと通る。型付きの`LaunchFact`へ
    載せる前の健全化は`_launch_fact_from_active`が個別に行う。
    """
    core = active.core
    launch = active.launch
    return (
        _is_non_empty_str(core.branch)
        and _is_non_empty_str(core.worktree_path)
        and (_is_usable_pid(launch.pid) or _is_non_empty_str(launch.external_id))
    )


def _build_launch_states(
    active_worktrees: Mapping[str, ActiveWorktree],
) -> dict[int, _LaunchState]:
    by_issue: dict[int, list[ActiveWorktree]] = {}
    for active in active_worktrees.values():
        by_issue.setdefault(active.core.issue_number, []).append(active)

    states: dict[int, _LaunchState] = {}
    for issue_number, entries in by_issue.items():
        if len(entries) > 1:
            # 同一Issueに複数のActiveWorktreeが観測された曖昧起動。最新を
            # 勝手に選ばず、候補viewから除外して保持する。
            states[issue_number] = _LaunchState(
                fact=None, active=True, indeterminate=True
            )
            continue
        active = entries[0]
        if active.launch.launch_phase not in _ACCEPTED_LAUNCH_PHASES or (
            not _has_valid_launch_handle(active)
        ):
            # 起動前(prepared)・結果不明(unknown)、またはhandle欠如
            # (pid/external_idいずれも無い)の不確定起動。
            states[issue_number] = _LaunchState(
                fact=None, active=True, indeterminate=True
            )
            continue
        states[issue_number] = _LaunchState(
            fact=_launch_fact_from_active(active), active=True, indeterminate=False
        )
    return states


def _normalize_labels(labels: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(labels))) if labels else ()


def _replace_primary_label(
    current: tuple[str, ...], new_primary: str
) -> tuple[str, ...]:
    remaining = {label for label in current if label not in PRIMARY_STATUS_LABELS}
    remaining.add(new_primary)
    return tuple(sorted(remaining))


@dataclass(frozen=True)
class CycleObservations:
    """1サイクル分の観測スナップショット（#1666）。"""

    tasks_by_issue: Mapping[int, Task] = field(default_factory=dict)
    dependency_resolution: Mapping[int, TaskDependencies] = field(default_factory=dict)
    ci_passed_pr_issue_numbers: Iterable[int] = ()
    changes_requested_issue_numbers: Iterable[int] = ()
    branch_by_issue_number: Mapping[int, str] = field(default_factory=dict)
    active_worktrees: Mapping[str, ActiveWorktree] = field(default_factory=dict)
    prior_parent_merge_completed_issue_numbers: frozenset[int] = frozenset()
    prior_parent_merge_hold_issue_numbers: frozenset[int] = frozenset()
    prior_parent_merge_repair_attempted_issue_numbers: frozenset[int] = frozenset()
    issue_records_by_number: Mapping[int, IssueRecord] = field(default_factory=dict)
    prs: Iterable[PrRecord] = ()
    branch_resolutions_by_issue: Mapping[int, TaskBranchResolution] = field(
        default_factory=dict
    )


_OBSERVATION_FIELDS = frozenset(f.name for f in dataclasses.fields(CycleObservations))


def _resolve_cycle_observations(
    observations: CycleObservations | None,
    kwargs: dict[str, Any],
    fn_name: str,
    *,
    default_active_worktrees: Mapping[str, ActiveWorktree] | None = None,
) -> CycleObservations:
    base = observations or CycleObservations(
        active_worktrees=(
            {} if default_active_worktrees is None else default_active_worktrees
        )
    )
    if (
        observations is not None
        and not base.active_worktrees
        and default_active_worktrees
    ):
        base = dataclasses.replace(base, active_worktrees=default_active_worktrees)
    overrides: dict[str, Any] = {}
    for name in _OBSERVATION_FIELDS:
        if name in kwargs:
            val = kwargs.pop(name)
            if val is None and name in (
                "branch_resolutions_by_issue",
                "issue_records_by_number",
            ):
                overrides[name] = {}
            else:
                overrides[name] = val
    if kwargs:
        unexpected = ", ".join(sorted(kwargs))
        raise TypeError(f"{fn_name}() got unexpected keyword argument(s): {unexpected}")
    return dataclasses.replace(base, **overrides) if overrides else base


class _CycleState:
    """1サイクル分のLifecycle実効状態を所有する、`CycleContext`専用の内部型。

    公開窓口は`CycleContext`のメソッドのみ。この型を他モジュールへ公開しない。
    """

    def __init__(
        self,
        observations: CycleObservations | None = None,
        *,
        completion_blocked: Callable[[int], bool] | None = None,
        **kwargs: Any,
    ) -> None:
        resolved = _resolve_cycle_observations(
            observations, kwargs, "_CycleState.__init__"
        )
        ledger = CycleLedger(
            effective_labels={},
            recorded_completions=set(),
            launch_states=_build_launch_states(resolved.active_worktrees),
        )
        self._ledger = ledger
        self._view = CycleTaskView(
            vars(resolved), ledger, completion_blocked=completion_blocked
        )

    # ---- read-only query delegates -----------------------------------------

    def _labels(self, issue_number: int) -> tuple[str, ...]:
        return self._view.labels(issue_number)

    def _effective_task(self, issue_number: int) -> Task | None:
        return self._view.effective_task(issue_number)

    def task(self, issue_number: int) -> CycleTask | None:
        return self._view.task(issue_number)

    def dependencies_of(self, issue_number: int) -> TaskDependencies | None:
        return self._view.dependencies_of(issue_number)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        return self._view.assess_dependencies(issue_number)

    def is_effectively_done(self, issue_number: int) -> bool:
        return self._view.is_effectively_done(issue_number)

    def is_completion_confirmed(self, issue_number: int) -> bool:
        return self._view.is_completion_confirmed(issue_number)

    def has_changes_requested(self, issue_number: int) -> bool:
        return self._view.has_changes_requested(issue_number)

    def is_ci_passed(self, issue_number: int) -> bool:
        return self._view.is_ci_passed(issue_number)

    def canonical_branch(self, issue_number: int) -> str | None:
        return self._view.canonical_branch(issue_number)

    def branch_resolution(self, issue_number: int) -> TaskBranchResolution | None:
        return self._view.branch_resolution(issue_number)

    def launch_fact(self, issue_number: int) -> LaunchFact | None:
        return self._view.launch_fact(issue_number)

    def _is_in_progress(self, issue_number: int) -> bool:
        return self._view.is_in_progress(issue_number)

    def _current_primary(self, issue_number: int) -> str | None:
        return self._view.current_primary(issue_number)

    def _candidate_tasks(self, wanted: str) -> tuple[CycleTask, ...]:
        return self._view.candidate_tasks(wanted)

    def queued_tasks(self) -> tuple[CycleTask, ...]:
        return self._view.queued_tasks()

    def blocked_tasks(self) -> tuple[CycleTask, ...]:
        return self._view.blocked_tasks()

    def tasks(self) -> tuple[CycleTask, ...]:
        return self._view.tasks()

    def issue_records(self) -> tuple[IssueRecord, ...]:
        return self._view.issue_records()

    def pull_requests(self) -> tuple[PrRecord, ...]:
        return self._view.pull_requests()

    def is_prior_merge_held(self, issue_number: int) -> bool:
        return self._view.is_prior_merge_held(issue_number)

    def is_prior_merge_repair_attempted(self, issue_number: int) -> bool:
        return self._view.is_prior_merge_repair_attempted(issue_number)

    def dag_inputs(self, issue_numbers: tuple[int, ...]) -> tuple[SubTask, ...]:
        return self._view.dag_inputs(issue_numbers)

    # ---- record APIs --------------------------------------------------------

    def record_completion(self, issue_number: int) -> RecordResult:
        if not self._view.has_task(issue_number):
            return RecordResult(RecordStatus.CONFLICT, REASON_UNKNOWN_ISSUE)
        if self.is_effectively_done(issue_number):
            return RecordResult(RecordStatus.NOOP)
        self._ledger.recorded_completions.add(issue_number)
        self._ledger.effective_labels[issue_number] = _replace_primary_label(
            self._labels(issue_number), StatusLabel.DONE
        )
        existing = self._ledger.launch_states.get(issue_number)
        if existing is not None:
            self._ledger.launch_states[issue_number] = dataclasses.replace(
                existing, active=False
            )
        return RecordResult(RecordStatus.APPLIED)

    def _is_terminal_for_launch(self, issue_number: int) -> bool:
        if self.is_effectively_done(issue_number):
            return True
        return any(label in self._labels(issue_number) for label in _ESCALATION_TARGETS)

    def record_launch(self, active: ActiveWorktree) -> RecordResult:
        issue_number = active.core.issue_number
        if not self._view.has_task(issue_number):
            return RecordResult(RecordStatus.CONFLICT, REASON_UNKNOWN_ISSUE)
        if not _has_valid_launch_handle(active) or (
            active.launch.launch_phase not in _ACCEPTED_LAUNCH_PHASES
        ):
            return RecordResult(RecordStatus.CONFLICT, REASON_INVALID_LAUNCH)
        if self._is_terminal_for_launch(issue_number):
            return RecordResult(RecordStatus.CONFLICT, REASON_TERMINAL_STATE)

        existing = self._ledger.launch_states.get(issue_number)
        if existing is not None and existing.indeterminate:
            # 不確定・曖昧起動は、確認済みの回収(execution_active=False)遷移
            # でのみ解除できる。新しいrecord_launchで無視して上書きしない。
            return RecordResult(RecordStatus.CONFLICT, REASON_LAUNCH_MISMATCH)

        new_fact = _launch_fact_from_active(active)
        if existing is not None and existing.fact is not None and existing.active:
            if existing.fact == new_fact:
                return RecordResult(RecordStatus.NOOP)
            return RecordResult(RecordStatus.CONFLICT, REASON_LAUNCH_MISMATCH)

        self._ledger.launch_states[issue_number] = _LaunchState(
            fact=new_fact, active=True, indeterminate=False
        )
        self._ledger.effective_labels[issue_number] = _replace_primary_label(
            self._labels(issue_number), StatusLabel.IN_PROGRESS
        )
        return RecordResult(RecordStatus.APPLIED)

    def _allowed_transition(
        self,
        current_primary: str | None,
        target: str,
        lifecycle: TaskLifecycle,
    ) -> bool:
        """遷移先が表内かどうかだけを判定する。

        起動事実の存在・`execution_active`との整合性は呼出側
        (`record_transition`)が別途チェックする——ここで`False`を返すのは
        「表外」(invalid-transition)の場合だけに限定し、起動整合性の失敗
        (execution-mismatch)と理由を混同しない。
        """
        if current_primary == target:
            return True
        if lifecycle in _TERMINAL_LIFECYCLE and target in _TERMINAL_TARGETS:
            # 既に実効完了しているタスクのラベルが、確定済みの完了へ追いつく
            # ケース(例: 検証済み先行マージで完了したがラベルはqueuedのまま
            # だったIssueに、後から確認済みのstatus:doneが付く)。巻き戻しでは
            # ないため、人手判断待ち等の他ルールより先に許可する。
            return True
        if current_primary in _ESCALATION_TARGETS:
            # 人手判断待ちからの自動解除は許可しない。
            return False
        if target in _ESCALATION_TARGETS:
            return current_primary in (
                StatusLabel.QUEUED,
                StatusLabel.BLOCKED,
                StatusLabel.IN_PROGRESS,
            )
        if current_primary is None:
            # 主状態なし/複数主状態(Lifecycle=OPEN)からのrepair。
            if lifecycle != TaskLifecycle.OPEN:
                return False
            return target in (
                StatusLabel.QUEUED,
                StatusLabel.BLOCKED,
                StatusLabel.IN_PROGRESS,
            )
        return target in _NON_TERMINAL_TRANSITIONS.get(current_primary, frozenset())

    def _reject_inconsistent_execution(
        self, issue_number: int, target: str, execution_active: bool
    ) -> RecordResult | None:
        """`execution_active=true`の主張が成立するかを検証する。

        呼出側は**NOOP判定より前に**これを通す。NOOP判定を
        先に行うと、handle欠如の不確定起動(構築時からactive=Trueだが
        launch_fact=None)に対して同じラベル・`execution_active=true`をそのまま
        再送するだけで、起動事実の検証を経ずにNOOPが返ってしまう。
        """
        if not execution_active:
            return None
        if (
            self.is_effectively_done(issue_number)
            or self.launch_fact(issue_number) is None
        ):
            return RecordResult(RecordStatus.CONFLICT, REASON_EXECUTION_MISMATCH)
        if target not in _EXECUTION_ACTIVE_ALLOWED_TARGETS:
            return RecordResult(RecordStatus.CONFLICT, REASON_EXECUTION_MISMATCH)
        return None

    def _reject_disallowed_transition(
        self,
        issue_number: int,
        current_labels: tuple[str, ...],
        target: str,
        execution_active: bool,
    ) -> RecordResult | None:
        """遷移表・終端規則・起動条件に照らして遷移先を検証する。"""
        current_primary = self._current_primary(issue_number)
        lifecycle = task_lifecycle(
            current_labels,
            completed=self.is_completion_confirmed(issue_number),
        )
        # 終端(DONE/NOT_NEEDED)から**非終端**への巻き戻しは、表の内外を問わず
        # terminal-stateとして拒否する。終端ラベルへの遷移は巻き戻しではなく
        # 「確定済みの完了にラベルが追いつく」ケースなので、ここでは弾かず
        # `_allowed_transition`の判断に委ねる。
        if lifecycle in _TERMINAL_LIFECYCLE and target not in _TERMINAL_TARGETS:
            return RecordResult(RecordStatus.CONFLICT, REASON_TERMINAL_STATE)
        if not self._allowed_transition(current_primary, target, lifecycle):
            return RecordResult(RecordStatus.CONFLICT, REASON_INVALID_TRANSITION)
        if (
            target == StatusLabel.IN_PROGRESS
            and current_primary != target
            and not execution_active
        ):
            # 新たにIN_PROGRESSへ入る遷移はexecution_active=trueとの組でのみ
            # 有効。既にIN_PROGRESSな同一主状態への再記録(起動終了の反映)は
            # 別枠であり、ここでは対象にしない。
            return RecordResult(RecordStatus.CONFLICT, REASON_INVALID_TRANSITION)
        return None

    def record_transition(
        self,
        issue_number: int,
        *,
        expected_labels: tuple[str, ...],
        verified_labels: tuple[str, ...],
        execution_active: bool,
    ) -> RecordResult:
        if not self._view.has_task(issue_number):
            return RecordResult(RecordStatus.CONFLICT, REASON_UNKNOWN_ISSUE)

        verified_primaries = primary_status_labels(tuple(verified_labels))
        if len(verified_primaries) != 1:
            return RecordResult(RecordStatus.CONFLICT, REASON_INVALID_TRANSITION)
        target = verified_primaries[0]

        conflict = self._reject_inconsistent_execution(
            issue_number, target, execution_active
        )
        if conflict is not None:
            return conflict

        current_labels = self._labels(issue_number)
        verified_set = _normalize_labels(verified_labels)
        current_set = _normalize_labels(current_labels)

        if verified_set == current_set and execution_active == self._is_in_progress(
            issue_number
        ):
            return RecordResult(RecordStatus.NOOP)

        if _normalize_labels(expected_labels) != current_set:
            return RecordResult(RecordStatus.CONFLICT, REASON_STALE_OBSERVATION)

        conflict = self._reject_disallowed_transition(
            issue_number, current_labels, target, execution_active
        )
        if conflict is not None:
            return conflict

        self._ledger.effective_labels[issue_number] = verified_set
        self._apply_execution_active(issue_number, execution_active)
        return RecordResult(RecordStatus.APPLIED)

    def _apply_execution_active(self, issue_number: int, active: bool) -> None:
        existing = self._ledger.launch_states.get(issue_number)
        if active:
            # 呼出元(`record_transition`)がexecution_active=trueを許すのは
            # 有効な起動事実が既にある場合だけなので、`existing`は必ず存在する。
            assert existing is not None
            self._ledger.launch_states[issue_number] = dataclasses.replace(
                existing, active=True
            )
            return
        if existing is not None:
            # 確認済みの回収: 起動事実を無効化し、曖昧・不確定フラグも解除する。
            # これにより、この後の同一Issueへのrecord_launchは新規起動として
            # 受け付けられる（過去起動の再利用や無視をしない）。
            self._ledger.launch_states[issue_number] = _LaunchState(
                fact=None, active=False, indeterminate=False
            )


__all__ = [
    "LaunchFact",
    "RecordResult",
    "RecordStatus",
    "REASON_EXECUTION_MISMATCH",
    "REASON_INVALID_LAUNCH",
    "REASON_INVALID_TRANSITION",
    "REASON_LAUNCH_MISMATCH",
    "REASON_STALE_OBSERVATION",
    "REASON_TERMINAL_STATE",
    "REASON_UNKNOWN_ISSUE",
]
