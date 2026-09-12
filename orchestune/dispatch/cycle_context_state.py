"""`CycleContext`が公開するsemantic query/record APIの内部所有者（#868）。

`CycleContext`自身はコンストラクタ引数（既存フィールド）を一切変更しない公開窓口
のまま、初期観測・成功確認後の差分・起動事実の3種を`_CycleState`へ集約する。
フェーズ側から見えるのは`CycleContext`のメソッドだけで、別のDispatchSnapshot型は
作らない。

- **初期観測**: コンストラクタ入力（`tasks_by_issue`, `dependency_resolution`,
  `ci_passed_pr_issue_numbers`, `changes_requested_issue_numbers`,
  `branch_by_issue_number`, `run_state.active_worktrees`,
  `prior_parent_merge_completed_issue_numbers`）から、必要なスカラー値だけを
  コピーして所有する。入力コンテナや`ActiveWorktree`自体への参照は保持しない。
- **成功確認後の差分**: `record_completion` / `record_launch` /
  `record_transition`が反映する、Issue番号別の実効ラベル・起動事実・実行中
  フラグ。外部I/Oは行わず、呼出側が既に成功を確認した事実だけを反映する。
- **RunState**: 従来通り`CycleContext.run_state`が正本であり、本モジュールは
  構築時にスカラーを一度読むだけで、保存・履歴追加を行わない（#868の移行例外）。

Identity（依存先Issue番号の解決）は`dependency_resolution`が担い、本モジュールは
Lifecycle（実効状態）だけを扱う。`_CycleState`は#867の`DependencyStateView`を
構造的に満たし、`assess_dependencies`はその`assess_dependency_lifecycle`へ委譲する。

`_CycleState`は`rules`や構築モジュール（`cycle_context.py`）へ逆importしない。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from orchestune.consistency.desired import TaskLifecycle
from orchestune.consistency.invariants.status import (
    PRIMARY_STATUS_LABELS,
    primary_status_labels,
)
from orchestune.dispatch.dependency_assessment import (
    DependencyAssessment,
)
from orchestune.dispatch.dependency_assessment import (
    assess_dependencies as assess_dependency_lifecycle,
)
from orchestune.dispatch.dependency_resolution import TaskDependencies
from orchestune.dispatch.state import ActiveWorktree
from orchestune.dispatch.status_repair import task_lifecycle
from orchestune.labels import StatusLabel
from orchestune.models import Task

# record_*が返す競合理由の固定文字列（Issue本文セクションD/E）。
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

# 非終端主状態間の許可遷移(Issue本文セクションE)。IN_PROGRESSへの遷移は
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


@dataclass(frozen=True, slots=True)
class LaunchFact:
    """`ActiveWorktree`から投影した、起動の確定事実。

    `ActiveWorktree`自体への参照ではなく、必要な7フィールドだけの不変値。
    """

    issue_number: int
    branch: str
    worktree_path: str
    pid: int | None
    started_at: float | None
    external_id: str | None
    launch_attempt_id: str | None


def _launch_fact_from_active(active: ActiveWorktree) -> LaunchFact:
    return LaunchFact(
        issue_number=active.issue_number,
        branch=active.branch,
        worktree_path=active.worktree_path,
        pid=active.pid,
        started_at=active.started_at,
        external_id=active.external_id,
        launch_attempt_id=active.launch_attempt_id,
    )


@dataclass
class _LaunchState:
    """1Issue分の起動状態。`fact=None`かつ`indeterminate=True`は、起動前・
    結果不明・handle欠如、または同一Issueに複数のActiveWorktreeが観測された
    曖昧な状態を表す——いずれも候補viewから除外し、`record_launch`はこの状態が
    解消されるまで（`execution_active=False`の回収遷移まで）常に競合を返す。
    """

    fact: LaunchFact | None
    active: bool
    indeterminate: bool


def _has_valid_launch_handle(active: ActiveWorktree) -> bool:
    """`record_launch`のinvalid-launch判定と同じ基準(#868レビュー対応)。

    branch/worktree_pathが空、またはpidも(空文字列でない)external_idも無い
    (生存確認もプロバイダへの照会もできない)場合は、有効な起動として扱わない。
    `run_state.json`の`_parse_active_worktrees`は空文字列の`external_id`を
    そのまま保持する(起動時attemptのパーサは空文字列を既に拒否しているが、
    この構築経路は素通りする)ため、`is not None`だけでは空文字列を有効な
    handleとして誤認する。

    構築時の初期観測にもこの基準を適用しないと、`recovery`がジャーナルも
    一致するPRも見つけられずhandle無しで復元した`ActiveWorktree`
    (`_build_restored_active_worktree`参照)を、誤って確定的なLaunchFactへ
    昇格させてしまう——`record_launch`が同じ入力をinvalid-launchとして
    拒否するのと矛盾する。
    """
    return (
        bool(active.branch)
        and bool(active.worktree_path)
        and (active.pid is not None or bool(active.external_id))
    )


def _build_launch_states(
    active_worktrees: Mapping[str, ActiveWorktree],
) -> dict[int, _LaunchState]:
    by_issue: dict[int, list[ActiveWorktree]] = {}
    for active in active_worktrees.values():
        by_issue.setdefault(active.issue_number, []).append(active)

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
        if active.launch_phase not in _ACCEPTED_LAUNCH_PHASES or (
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


@dataclass
class _CycleState:
    """1サイクル分のLifecycle実効状態を所有する、`CycleContext`専用の内部型。

    公開窓口は`CycleContext`のメソッドのみ。この型を他モジュールへ公開しない。
    """

    _tasks: dict[int, Task] = field(default_factory=dict)
    _dependency_resolution: dict[int, TaskDependencies] = field(default_factory=dict)
    _ci_passed: frozenset[int] = frozenset()
    _changes_requested: frozenset[int] = frozenset()
    _branch_by_issue: dict[int, str] = field(default_factory=dict)
    _prior_completed: frozenset[int] = frozenset()
    _effective_labels: dict[int, tuple[str, ...]] = field(default_factory=dict)
    _launch_states: dict[int, _LaunchState] = field(default_factory=dict)
    _recorded_completions: set[int] = field(default_factory=set)

    @classmethod
    def from_observations(
        cls,
        *,
        tasks_by_issue: Mapping[int, Task],
        dependency_resolution: Mapping[int, TaskDependencies],
        ci_passed_pr_issue_numbers: Iterable[int],
        changes_requested_issue_numbers: Iterable[int],
        branch_by_issue_number: Mapping[int, str],
        active_worktrees: Mapping[str, ActiveWorktree],
        prior_parent_merge_completed_issue_numbers: frozenset[int],
    ) -> _CycleState:
        tasks = dict(tasks_by_issue)
        return cls(
            _tasks=tasks,
            _dependency_resolution=dict(dependency_resolution),
            _ci_passed=frozenset(ci_passed_pr_issue_numbers),
            _changes_requested=frozenset(changes_requested_issue_numbers),
            _branch_by_issue=dict(branch_by_issue_number),
            _prior_completed=frozenset(prior_parent_merge_completed_issue_numbers),
            # 初期観測はそのままのタプルを保持する(順序を変えない)。これにより
            # 未変更のTaskは`task()`から同一オブジェクトとして返る。
            _effective_labels={
                issue_number: tuple(task.status_labels)
                for issue_number, task in tasks.items()
            },
            _launch_states=_build_launch_states(active_worktrees),
        )

    # ---- read-only queries -------------------------------------------------

    def task(self, issue_number: int) -> Task | None:
        base = self._tasks.get(issue_number)
        if base is None:
            return None
        labels = self._effective_labels.get(issue_number, base.status_labels)
        if labels == base.status_labels:
            return base
        return dataclasses.replace(base, status_labels=labels)

    def dependencies_of(self, issue_number: int) -> TaskDependencies | None:
        return self._dependency_resolution.get(issue_number)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        deps = self.dependencies_of(issue_number)
        if deps is None:
            return None
        return assess_dependency_lifecycle(deps, self)

    def is_effectively_done(self, issue_number: int) -> bool:
        completed_override = (
            issue_number in self._recorded_completions
            or issue_number in self._prior_completed
        )
        labels = self._effective_labels.get(issue_number)
        if labels is None:
            return completed_override
        return task_lifecycle(labels, completed=completed_override) in (
            _TERMINAL_LIFECYCLE
        )

    def has_changes_requested(self, issue_number: int) -> bool:
        return issue_number in self._changes_requested

    def is_ci_passed(self, issue_number: int) -> bool:
        return issue_number in self._ci_passed

    def canonical_branch(self, issue_number: int) -> str | None:
        launch_state = self._launch_states.get(issue_number)
        if launch_state is not None and launch_state.fact is not None:
            return launch_state.fact.branch
        return self._branch_by_issue.get(issue_number)

    def launch_fact(self, issue_number: int) -> LaunchFact | None:
        launch_state = self._launch_states.get(issue_number)
        return None if launch_state is None else launch_state.fact

    def _is_in_progress(self, issue_number: int) -> bool:
        launch_state = self._launch_states.get(issue_number)
        return launch_state is not None and launch_state.active

    def _current_primary(self, issue_number: int) -> str | None:
        labels = self._effective_labels.get(issue_number, ())
        primaries = primary_status_labels(labels)
        return primaries[0] if len(primaries) == 1 else None

    def _candidate_tasks(self, wanted: str) -> tuple[Task, ...]:
        matches: list[Task] = []
        for issue_number, base in self._tasks.items():
            if base.issue_state != "OPEN":
                continue
            if self.is_effectively_done(issue_number):
                continue
            if self._is_in_progress(issue_number):
                continue
            if self._current_primary(issue_number) != wanted:
                continue
            task = self.task(issue_number)
            assert task is not None
            matches.append(task)
        matches.sort(key=lambda t: t.issue_number)
        return tuple(matches)

    def queued_tasks(self) -> tuple[Task, ...]:
        return self._candidate_tasks(StatusLabel.QUEUED)

    def blocked_tasks(self) -> tuple[Task, ...]:
        return self._candidate_tasks(StatusLabel.BLOCKED)

    # ---- record APIs --------------------------------------------------------

    def record_completion(self, issue_number: int) -> RecordResult:
        if issue_number not in self._tasks:
            return RecordResult(RecordStatus.CONFLICT, REASON_UNKNOWN_ISSUE)
        if self.is_effectively_done(issue_number):
            return RecordResult(RecordStatus.NOOP)
        self._recorded_completions.add(issue_number)
        self._effective_labels[issue_number] = _replace_primary_label(
            self._effective_labels.get(issue_number, ()), StatusLabel.DONE
        )
        existing = self._launch_states.get(issue_number)
        if existing is not None:
            self._launch_states[issue_number] = dataclasses.replace(
                existing, active=False
            )
        return RecordResult(RecordStatus.APPLIED)

    def _is_terminal_for_launch(self, issue_number: int) -> bool:
        if self.is_effectively_done(issue_number):
            return True
        return self._current_primary(issue_number) in _ESCALATION_TARGETS

    def record_launch(self, active: ActiveWorktree) -> RecordResult:
        issue_number = active.issue_number
        if issue_number not in self._tasks:
            return RecordResult(RecordStatus.CONFLICT, REASON_UNKNOWN_ISSUE)
        if not _has_valid_launch_handle(active) or (
            active.launch_phase not in _ACCEPTED_LAUNCH_PHASES
        ):
            return RecordResult(RecordStatus.CONFLICT, REASON_INVALID_LAUNCH)
        if self._is_terminal_for_launch(issue_number):
            return RecordResult(RecordStatus.CONFLICT, REASON_TERMINAL_STATE)

        existing = self._launch_states.get(issue_number)
        if existing is not None and existing.indeterminate:
            # 不確定・曖昧起動は、確認済みの回収(execution_active=False)遷移
            # でのみ解除できる。新しいrecord_launchで無視して上書きしない。
            return RecordResult(RecordStatus.CONFLICT, REASON_LAUNCH_MISMATCH)

        new_fact = _launch_fact_from_active(active)
        if existing is not None and existing.fact is not None and existing.active:
            if existing.fact == new_fact:
                return RecordResult(RecordStatus.NOOP)
            return RecordResult(RecordStatus.CONFLICT, REASON_LAUNCH_MISMATCH)

        self._launch_states[issue_number] = _LaunchState(
            fact=new_fact, active=True, indeterminate=False
        )
        self._effective_labels[issue_number] = _replace_primary_label(
            self._effective_labels.get(issue_number, ()), StatusLabel.IN_PROGRESS
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

    def record_transition(
        self,
        issue_number: int,
        *,
        expected_labels: tuple[str, ...],
        verified_labels: tuple[str, ...],
        execution_active: bool,
    ) -> RecordResult:
        if issue_number not in self._tasks:
            return RecordResult(RecordStatus.CONFLICT, REASON_UNKNOWN_ISSUE)

        verified_primaries = primary_status_labels(tuple(verified_labels))
        if len(verified_primaries) != 1:
            return RecordResult(RecordStatus.CONFLICT, REASON_INVALID_TRANSITION)
        target = verified_primaries[0]

        # execution_active=trueの整合性は、NOOP/APPLIEDのいずれであるかに関わらず
        # 常に検証する(#868レビュー対応)。NOOP判定を先に行うと、handle欠如の
        # 不確定起動(構築時からactive=Trueだがlaunch_fact=None)に対して同じ
        # ラベル・execution_active=trueをそのまま再送するだけで、起動事実の
        # 検証を経ずにNOOPが返ってしまう。
        if execution_active:
            if self.launch_fact(issue_number) is None:
                return RecordResult(RecordStatus.CONFLICT, REASON_EXECUTION_MISMATCH)
            if target in (StatusLabel.QUEUED, StatusLabel.BLOCKED):
                return RecordResult(RecordStatus.CONFLICT, REASON_EXECUTION_MISMATCH)

        current_labels = self._effective_labels.get(issue_number, ())
        current_active = self._is_in_progress(issue_number)
        verified_set = _normalize_labels(verified_labels)
        current_set = _normalize_labels(current_labels)

        if verified_set == current_set and execution_active == current_active:
            return RecordResult(RecordStatus.NOOP)

        expected_set = _normalize_labels(expected_labels)
        if expected_set != current_set:
            return RecordResult(RecordStatus.CONFLICT, REASON_STALE_OBSERVATION)

        current_primary = self._current_primary(issue_number)
        lifecycle = task_lifecycle(
            current_labels,
            completed=(
                issue_number in self._recorded_completions
                or issue_number in self._prior_completed
            ),
        )
        # 終端(DONE/NOT_NEEDED)からの非終端への巻き戻しは、表の内外を問わず
        # terminal-stateとして拒否する(同一実効状態の再記録は上の
        # `current_primary == target`分岐でNOOP/APPLIEDのいずれかに既に
        # 倒れている)。
        if lifecycle in _TERMINAL_LIFECYCLE and target != current_primary:
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

        self._effective_labels[issue_number] = verified_set
        self._apply_execution_active(issue_number, execution_active)
        return RecordResult(RecordStatus.APPLIED)

    def _apply_execution_active(self, issue_number: int, active: bool) -> None:
        existing = self._launch_states.get(issue_number)
        if active:
            # 呼出元(`record_transition`)がexecution_active=trueを許すのは
            # 有効な起動事実が既にある場合だけなので、`existing`は必ず存在する。
            assert existing is not None
            self._launch_states[issue_number] = dataclasses.replace(
                existing, active=True
            )
            return
        if existing is not None:
            # 確認済みの回収: 起動事実を無効化し、曖昧・不確定フラグも解除する。
            # これにより、この後の同一Issueへのrecord_launchは新規起動として
            # 受け付けられる（過去起動の再利用や無視をしない）。
            self._launch_states[issue_number] = _LaunchState(
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
