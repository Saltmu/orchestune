"""#869: `LockDependencyView`(共通`DependencyAssessment`)を読む
`_direct_dependency_canonical_branches`の決定表を単体で固定する。

`tests/test_dispatch_locks_dependency_exclusion.py`は`scan_external_locks`が
組み立てる既定view（`CycleContext`を介さない標準ラベルのみの意味論）を経由した
end-to-endの回帰群であるのに対し、本ファイルは`view`を直接差し替えられる
テスト専用double（`_FakeLockDependencyView`）で、既定viewでは再現できない
「実行中branchが既定prefix名と食い違う」ケースを含む決定表そのものを検証する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from orchestune.branch_naming import build_task_branch_name
from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_resolution import (
    TaskDependencies,
    UnresolvedDependency,
)
from orchestune.dispatch.locks import (
    _DefaultLockDependencyView,
    _direct_dependency_canonical_branches,
    scan_external_locks,
)
from orchestune.dispatch.scoring import Task
from orchestune.models import PrRecord


def _task(
    issue_number=1,
    subtask_id="task-a",
    status_labels=("status:blocked",),
    depends_on=(),
    footprint=("src/shared.py",),
):
    return Task(
        issue_number=issue_number,
        subtask_id=subtask_id,
        footprint=footprint,
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=status_labels,
        created_at="2026-01-01T00:00:00+00:00",
        depends_on=depends_on,
        parent_number=100,
    )


@dataclass
class _FakeLockDependencyView:
    """`LockDependencyView`のテスト用double。`canonical_branch`を
    `build_task_branch_name`の既定名と独立に指定できるため、実行中branchが
    既定prefix名と食い違うケースを直接組み立てられる。"""

    tasks: dict[int, Task] = field(default_factory=dict)
    assessments: dict[int, DependencyAssessment | None] = field(default_factory=dict)
    branches: dict[int, str | None] = field(default_factory=dict)

    def task(self, issue_number: int) -> Task | None:
        return self.tasks.get(issue_number)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        return self.assessments.get(issue_number)

    def canonical_branch(self, issue_number: int) -> str | None:
        return self.branches.get(issue_number)


class TestDirectDependencyCanonicalBranches:
    def test_empty_when_task_is_not_blocked(self):
        task = _task(status_labels=("status:queued",), depends_on=("dep-a",))
        view = _FakeLockDependencyView(
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),)
                )
            }
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_empty_when_assessment_is_none(self):
        task = _task(depends_on=("dep-a",))
        view = _FakeLockDependencyView(assessments={1: None})
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_empty_when_any_dependency_is_unresolved(self):
        """#799: 一部でも未解決なら、他が解決済みでも一切除外しない。"""
        task = _task(depends_on=("dep-a", "missing-dep"))
        dep_task = _task(issue_number=2, subtask_id="dep-a")
        view = _FakeLockDependencyView(
            tasks={2: dep_task},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),),
                    unresolved=(
                        UnresolvedDependency(raw="missing-dep", reason="missing"),
                    ),
                )
            },
            branches={2: build_task_branch_name(2, "dep-a")},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_excludes_nothing_for_completed_dependency(self):
        """完了済み依存は除外候補から外す（=通常の衝突判定に残す）。"""
        task = _task(depends_on=("dep-a",))
        dep_task = _task(issue_number=2, subtask_id="dep-a")
        view = _FakeLockDependencyView(
            tasks={2: dep_task},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.COMPLETED),)
                )
            },
            branches={2: build_task_branch_name(2, "dep-a")},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_includes_branch_for_non_completed_resolved_dependency(self):
        task = _task(depends_on=("dep-a",))
        dep_task = _task(issue_number=2, subtask_id="dep-a")
        expected_branch = build_task_branch_name(2, "dep-a")
        view = _FakeLockDependencyView(
            tasks={2: dep_task},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),)
                )
            },
            branches={2: expected_branch},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset(
            {expected_branch}
        )

    def test_excludes_nothing_when_dependency_task_is_missing(self):
        task = _task(depends_on=("dep-a",))
        view = _FakeLockDependencyView(
            tasks={},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),)
                )
            },
            branches={2: build_task_branch_name(2, "dep-a")},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_excludes_nothing_when_canonical_branch_is_missing(self):
        task = _task(depends_on=("dep-a",))
        dep_task = _task(issue_number=2, subtask_id="dep-a")
        view = _FakeLockDependencyView(
            tasks={2: dep_task},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),)
                )
            },
            branches={2: None},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_excludes_nothing_when_canonical_branch_mismatches_default_name(self):
        """依存元が既定prefix名と異なる実行中branch（記録済みLaunchFact由来）で
        実際に走っている場合、既定名PRとの一致はスタッキング取り込みの証拠に
        ならないため除外しない(#869)。"""
        task = _task(depends_on=("dep-a",))
        dep_task = _task(issue_number=2, subtask_id="dep-a")
        view = _FakeLockDependencyView(
            tasks={2: dep_task},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),)
                )
            },
            # 実行中の実際のbranchが既定prefix名(build_task_branch_name)と食い違う。
            branches={2: "recovered/issue-2-dep-a"},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset()

    def test_includes_only_the_matching_branch_among_multiple_dependencies(self):
        task = _task(depends_on=("dep-a", "dep-b"))
        dep_a = _task(issue_number=2, subtask_id="dep-a")
        dep_b = _task(issue_number=3, subtask_id="dep-b")
        expected_branch = build_task_branch_name(2, "dep-a")
        view = _FakeLockDependencyView(
            tasks={2: dep_a, 3: dep_b},
            assessments={
                1: DependencyAssessment(
                    resolved=(
                        AssessedDependency(2, DependencyState.WAITING),
                        AssessedDependency(3, DependencyState.COMPLETED),
                    )
                )
            },
            branches={2: expected_branch, 3: build_task_branch_name(3, "dep-b")},
        )
        assert _direct_dependency_canonical_branches(task, view) == frozenset(
            {expected_branch}
        )


class TestDefaultLockDependencyView:
    """`view`省略時のフォールバック(`_default_lock_dependency_view`が組み立てる
    `_DefaultLockDependencyView`)自体のNone境界。`_direct_dependency_canonical_
    branches`からは`queued_tasks`に実在するissue_numberしか渡らないため
    通常到達しないが、Protocol実装として単体で健全であることを固定する。"""

    def test_assess_dependencies_is_none_for_unknown_issue(self):
        view = _DefaultLockDependencyView({}, {})
        assert view.assess_dependencies(999) is None

    def test_canonical_branch_is_none_for_unknown_issue(self):
        view = _DefaultLockDependencyView({}, {2: TaskDependencies(resolved=(999,))})
        assert view.canonical_branch(999) is None


class TestScanExternalLocksWithExplicitView:
    """`scan_external_locks`が`view`引数をそのまま
    `_direct_dependency_canonical_branches`へ引き継ぐことをend-to-endで確認する。"""

    def test_still_locks_against_pr_when_dependency_branch_was_renamed(self):
        task = _task(depends_on=("dep-a",))
        dep_task = _task(issue_number=2, subtask_id="dep-a", footprint=())
        default_branch = build_task_branch_name(2, "dep-a")
        view = _FakeLockDependencyView(
            tasks={1: task, 2: dep_task},
            assessments={
                1: DependencyAssessment(
                    resolved=(AssessedDependency(2, DependencyState.WAITING),)
                )
            },
            # 依存元は実際には既定名と異なるbranchで走っている。
            branches={2: "recovered/issue-2-dep-a"},
        )
        prs = [
            PrRecord(
                number=99,
                head_ref=default_branch,
                changed_files=("src/shared.py",),
                is_cross_repository=False,
            )
        ]
        result = scan_external_locks(
            [dep_task, task],
            remote_branches=[],
            prs=prs,
            active_branches=[],
            view=view,
        )
        assert [t.issue_number for t in result.to_lock] == [1]
