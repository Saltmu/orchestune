"""Issue #867: 依存ごとのライフサイクル分類を、CycleContextやForgeに依存しない
純関数`assess_dependencies`として固定する。

本テストはstack可否などの用途判断を一切含まず、「観測された実効状態から4状態の
どれに分類されるか」と「未解決診断が無損失で決定論的に並ぶか」だけを検証する。
"""

from __future__ import annotations

import dataclasses

import pytest

from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
    assess_dependencies,
)
from orchestune.dispatch.dependency_resolution import (
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_UNKNOWN_PARENT,
    TaskDependencies,
    UnresolvedDependency,
)

_UNKNOWN_REASON = "unknown-future-reason"


class _FakeStateView:
    """3つの番号集合だけを持つread-onlyなstate view。

    `assess_dependencies`が「どの問い合わせを何回行ったか」を検証できるよう、
    呼び出しを`calls`へ記録する。`forbidden`に含まれる番号を問い合わせた場合は
    即座に失敗させ、「問い合わせてはならない番号」を機械的に固定する。
    """

    def __init__(
        self,
        *,
        done: set[int] | None = None,
        changes_requested: set[int] | None = None,
        ci_passed: set[int] | None = None,
        forbidden: set[int] | None = None,
    ) -> None:
        self.done = set(done or ())
        self.changes_requested = set(changes_requested or ())
        self.ci_passed = set(ci_passed or ())
        self.forbidden = set(forbidden or ())
        self.calls: list[tuple[str, int]] = []

    def _record(self, query: str, issue_number: int) -> None:
        assert (
            issue_number not in self.forbidden
        ), f"{query}({issue_number}) must not be queried"
        self.calls.append((query, issue_number))

    def is_effectively_done(self, issue_number: int) -> bool:
        self._record("done", issue_number)
        return issue_number in self.done

    def has_changes_requested(self, issue_number: int) -> bool:
        self._record("changes_requested", issue_number)
        return issue_number in self.changes_requested

    def is_ci_passed(self, issue_number: int) -> bool:
        self._record("ci_passed", issue_number)
        return issue_number in self.ci_passed


class _ExplodingStateView:
    """指定した問い合わせで例外を送出するview。"""

    def __init__(self, failing_query: str) -> None:
        self.failing_query = failing_query
        self.error = RuntimeError(f"{failing_query} is unavailable")

    def _maybe_raise(self, query: str) -> None:
        if query == self.failing_query:
            raise self.error

    def is_effectively_done(self, issue_number: int) -> bool:
        self._maybe_raise("done")
        return False

    def has_changes_requested(self, issue_number: int) -> bool:
        self._maybe_raise("changes_requested")
        return False

    def is_ci_passed(self, issue_number: int) -> bool:
        self._maybe_raise("ci_passed")
        return False


def _membership(flag: bool, issue_number: int) -> set[int]:
    return {issue_number} if flag else set()


@pytest.mark.parametrize(
    ("done", "changes_requested", "ci_passed", "expected"),
    [
        (False, False, False, DependencyState.WAITING),
        (False, False, True, DependencyState.CI_PASSED_UNMERGED),
        (False, True, False, DependencyState.CHANGES_REQUESTED),
        (False, True, True, DependencyState.CHANGES_REQUESTED),
        (True, False, False, DependencyState.COMPLETED),
        (True, False, True, DependencyState.COMPLETED),
        (True, True, False, DependencyState.COMPLETED),
        (True, True, True, DependencyState.COMPLETED),
    ],
)
def test_dependency_state_truth_table(
    done: bool,
    changes_requested: bool,
    ci_passed: bool,
    expected: DependencyState,
) -> None:
    """3つの観測事実の全8組合せが、固定された優先順位で1状態へ落ちる。"""
    view = _FakeStateView(
        done=_membership(done, 7),
        changes_requested=_membership(changes_requested, 7),
        ci_passed=_membership(ci_passed, 7),
    )

    assessment = assess_dependencies(TaskDependencies(resolved=(7,)), view)

    assert assessment.resolved == (AssessedDependency(issue_number=7, state=expected),)
    assert assessment.unresolved == ()


def test_queries_short_circuit_and_deduplicate() -> None:
    """問い合わせはdone→changes→CIの順で短絡し、同一Issueを再照会しない。"""
    view = _FakeStateView(done={1}, changes_requested={1, 2}, ci_passed={1, 2, 3})

    assessment = assess_dependencies(
        TaskDependencies(resolved=(1, 2, 3, 4, 1, 3)), view
    )

    assert assessment.resolved == (
        AssessedDependency(issue_number=1, state=DependencyState.COMPLETED),
        AssessedDependency(issue_number=2, state=DependencyState.CHANGES_REQUESTED),
        AssessedDependency(issue_number=3, state=DependencyState.CI_PASSED_UNMERGED),
        AssessedDependency(issue_number=4, state=DependencyState.WAITING),
    )
    # COMPLETEDは1問い合わせ、CHANGES_REQUESTEDは2、CI_PASSED_UNMERGEDとWAITINGは3。
    # 重複入力(1, 3)による再照会は発生しない。
    assert view.calls == [
        ("done", 1),
        ("done", 2),
        ("changes_requested", 2),
        ("done", 3),
        ("changes_requested", 3),
        ("ci_passed", 3),
        ("done", 4),
        ("changes_requested", 4),
        ("ci_passed", 4),
    ]


def test_resolved_order_and_mixed_states() -> None:
    """解決済み分類はIssue番号昇順で、入力順に依存しない。"""
    states = {"done": {30}, "changes_requested": {10}, "ci_passed": {20}}
    expected = (
        AssessedDependency(issue_number=10, state=DependencyState.CHANGES_REQUESTED),
        AssessedDependency(issue_number=20, state=DependencyState.CI_PASSED_UNMERGED),
        AssessedDependency(issue_number=30, state=DependencyState.COMPLETED),
        AssessedDependency(issue_number=40, state=DependencyState.WAITING),
    )
    forward = TaskDependencies(resolved=(30, 10, 40, 20, 30))
    reversed_input = TaskDependencies(resolved=tuple(reversed(forward.resolved)))

    forward_assessment = assess_dependencies(forward, _FakeStateView(**states))
    reversed_assessment = assess_dependencies(reversed_input, _FakeStateView(**states))

    assert forward_assessment.resolved == expected
    assert reversed_assessment == forward_assessment


def test_empty_unresolved_and_partial_inputs() -> None:
    """空・未解決のみ・一部解決・全COMPLETED＋未解決ありを区別する。

    未解決診断の候補番号(20, 40)は解決済みではないため、viewへ問い合わせては
    ならない。`forbidden`でそれを機械的に固定する。
    """
    diagnostic = UnresolvedDependency(
        raw="build", reason=REASON_AMBIGUOUS, candidates=(40, 20, 40)
    )
    normalized = UnresolvedDependency(
        raw="build", reason=REASON_AMBIGUOUS, candidates=(20, 40, 40)
    )

    empty_view = _FakeStateView()
    assert assess_dependencies(TaskDependencies(), empty_view) == DependencyAssessment()
    assert empty_view.calls == []

    unresolved_only_view = _FakeStateView(forbidden={20, 40})
    unresolved_only = assess_dependencies(
        TaskDependencies(unresolved=(diagnostic,)), unresolved_only_view
    )
    assert unresolved_only.resolved == ()
    assert unresolved_only.unresolved == (normalized,)
    assert unresolved_only_view.calls == []

    partial = assess_dependencies(
        TaskDependencies(resolved=(30, 10, 30), unresolved=(diagnostic,)),
        _FakeStateView(done={10}, forbidden={20, 40}),
    )
    assert partial.resolved == (
        AssessedDependency(issue_number=10, state=DependencyState.COMPLETED),
        AssessedDependency(issue_number=30, state=DependencyState.WAITING),
    )
    assert partial.unresolved == (normalized,)

    all_completed = assess_dependencies(
        TaskDependencies(resolved=(10, 30), unresolved=(diagnostic,)),
        _FakeStateView(done={10, 30}, forbidden={20, 40}),
    )
    assert [entry.state for entry in all_completed.resolved] == [
        DependencyState.COMPLETED,
        DependencyState.COMPLETED,
    ]
    assert all_completed.unresolved == (normalized,)


def test_unresolved_diagnostics_are_lossless_and_sorted() -> None:
    """未解決診断は(reason, raw, candidates)の辞書式昇順で、件数も候補重複も失わない。"""
    diagnostics = (
        UnresolvedDependency(raw="b", reason=REASON_MISSING),
        UnresolvedDependency(raw="a", reason=REASON_MISSING, candidates=(9, 2, 9)),
        UnresolvedDependency(raw="a", reason=REASON_MISSING, candidates=(3,)),
        UnresolvedDependency(raw="a", reason=REASON_AMBIGUOUS, candidates=(5, 1)),
        UnresolvedDependency(raw="a", reason=_UNKNOWN_REASON),
        UnresolvedDependency(raw="9", reason=REASON_UNKNOWN_PARENT),
        UnresolvedDependency(raw="10", reason=REASON_UNKNOWN_PARENT),
        UnresolvedDependency(raw="b", reason=REASON_MISSING),
    )
    expected = (
        # reasonが第1キー。
        UnresolvedDependency(raw="a", reason=REASON_AMBIGUOUS, candidates=(1, 5)),
        # 同一reason内ではrawが第2キー、同一rawではcandidatesが第3キー。
        UnresolvedDependency(raw="a", reason=REASON_MISSING, candidates=(2, 9, 9)),
        UnresolvedDependency(raw="a", reason=REASON_MISSING, candidates=(3,)),
        # 重複診断は件数ごと保持する。
        UnresolvedDependency(raw="b", reason=REASON_MISSING),
        UnresolvedDependency(raw="b", reason=REASON_MISSING),
        # 未知のreason文字列もそのまま保持し、reason順に並ぶ。
        UnresolvedDependency(raw="a", reason=_UNKNOWN_REASON),
        # rawは文字列として比較する（数値変換しないので"10" < "9"）。
        UnresolvedDependency(raw="10", reason=REASON_UNKNOWN_PARENT),
        UnresolvedDependency(raw="9", reason=REASON_UNKNOWN_PARENT),
    )

    forward = assess_dependencies(
        TaskDependencies(unresolved=diagnostics), _FakeStateView()
    )
    reversed_assessment = assess_dependencies(
        TaskDependencies(unresolved=tuple(reversed(diagnostics))), _FakeStateView()
    )

    assert forward.unresolved == expected
    assert reversed_assessment == forward


def test_assessment_is_immutable_and_does_not_modify_input() -> None:
    """返却値は実行時にも変更不能で、入力`TaskDependencies`を書き換えない。"""
    dependencies = TaskDependencies(
        resolved=(30, 10),
        unresolved=(
            UnresolvedDependency(raw="a", reason=REASON_MISSING, candidates=(9, 2)),
        ),
    )
    before = dataclasses.replace(dependencies)

    assessment = assess_dependencies(dependencies, _FakeStateView(done={10}))

    assert isinstance(assessment.resolved, tuple)
    assert isinstance(assessment.unresolved, tuple)
    assert isinstance(assessment.unresolved[0].candidates, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.resolved = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.resolved[0].state = DependencyState.WAITING  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.unresolved[0].raw = "b"  # type: ignore[misc]
    with pytest.raises(TypeError):
        assessment.resolved[0] = assessment.resolved[1]  # type: ignore[index]
    with pytest.raises(TypeError):
        assessment.unresolved[0].candidates[0] = 0  # type: ignore[index]

    assert dependencies == before
    assert dependencies.resolved == (30, 10)
    assert dependencies.unresolved[0].candidates == (9, 2)


def test_reassessment_observes_updated_view() -> None:
    """再評価は最新の観測を反映し、既に返したAssessmentは変化しない。"""
    diagnostic = UnresolvedDependency(raw="a", reason=REASON_MISSING)
    dependencies = TaskDependencies(resolved=(5,), unresolved=(diagnostic,))
    view = _FakeStateView()

    before = assess_dependencies(dependencies, view)
    assert before.resolved == (
        AssessedDependency(issue_number=5, state=DependencyState.WAITING),
    )

    view.done.add(5)
    after = assess_dependencies(dependencies, view)

    assert before.resolved == (
        AssessedDependency(issue_number=5, state=DependencyState.WAITING),
    )
    assert after.resolved == (
        AssessedDependency(issue_number=5, state=DependencyState.COMPLETED),
    )
    assert before.unresolved == after.unresolved == (diagnostic,)


@pytest.mark.parametrize("failing_query", ["done", "changes_requested", "ci_passed"])
def test_view_error_propagates(failing_query: str) -> None:
    """必要な問い合わせで起きた例外をWAITINGや空Assessmentへ握りつぶさない。"""
    view = _ExplodingStateView(failing_query)

    with pytest.raises(RuntimeError, match=f"{failing_query} is unavailable"):
        assess_dependencies(TaskDependencies(resolved=(1,)), view)
