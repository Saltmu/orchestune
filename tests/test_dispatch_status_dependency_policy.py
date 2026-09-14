"""Issue #872: promotion requires every resolved dependency to be completed."""

from __future__ import annotations

import pytest

from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_resolution import (
    REASON_MISSING,
    UnresolvedDependency,
)
from orchestune.dispatch.status_dependency_policy import (
    completed_dependency_ids,
    dependencies_completed,
)


def _assessment(
    *states: DependencyState,
    unresolved: tuple[UnresolvedDependency, ...] = (),
) -> DependencyAssessment:
    return DependencyAssessment(
        resolved=tuple(
            AssessedDependency(issue_number=index, state=state)
            for index, state in enumerate(states, start=1)
        ),
        unresolved=unresolved,
    )


@pytest.mark.parametrize(
    ("assessment", "expected"),
    (
        (None, False),
        (_assessment(), True),
        (_assessment(DependencyState.COMPLETED), True),
        (
            _assessment(DependencyState.COMPLETED, DependencyState.COMPLETED),
            True,
        ),
        (_assessment(DependencyState.CI_PASSED_UNMERGED), False),
        (_assessment(DependencyState.CHANGES_REQUESTED), False),
        (_assessment(DependencyState.WAITING), False),
        (
            _assessment(
                DependencyState.COMPLETED,
                unresolved=(
                    UnresolvedDependency(raw="missing", reason=REASON_MISSING),
                ),
            ),
            False,
        ),
        (
            _assessment(DependencyState.COMPLETED, DependencyState.WAITING),
            False,
        ),
    ),
)
def test_dependencies_completed_truth_table(
    assessment: DependencyAssessment | None, expected: bool
) -> None:
    assert dependencies_completed(assessment) is expected


def test_completed_ids_come_only_from_fully_completed_assessments() -> None:
    completed = _assessment(DependencyState.COMPLETED)
    partial = _assessment(DependencyState.COMPLETED, DependencyState.WAITING)
    unresolved = _assessment(
        DependencyState.COMPLETED,
        unresolved=(UnresolvedDependency(raw="missing", reason=REASON_MISSING),),
    )

    assert completed_dependency_ids((None, completed, partial, unresolved)) == {"1"}
    assert completed_dependency_ids((partial,)) == {"1"}
    assert completed_dependency_ids((unresolved,)) == {"1"}
