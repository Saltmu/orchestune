"""Issue #872: promotion requires every resolved dependency to be completed."""

from __future__ import annotations

import pytest

from orchestune.dependencies.assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dependencies.resolution import (
    REASON_MISSING,
    UnresolvedDependency,
)
from orchestune.dispatch.status_dependency_policy import (
    completed_dependency_ids,
    dependencies_completed,
    desired_dependency_ids,
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


def test_desired_ids_keep_numeric_ids_and_add_pending_marker_for_each_incomplete() -> (
    None
):
    assessment = _assessment(
        DependencyState.COMPLETED,
        DependencyState.WAITING,
        DependencyState.CI_PASSED_UNMERGED,
        DependencyState.CHANGES_REQUESTED,
    )

    assert desired_dependency_ids(9, assessment) == (
        "1",
        "2",
        "3",
        "4",
        "pending-dependency:9:2",
        "pending-dependency:9:3",
        "pending-dependency:9:4",
    )
    assert completed_dependency_ids((assessment,)) == {"1"}


def test_desired_ids_have_no_marker_when_every_dependency_is_completed() -> None:
    assert desired_dependency_ids(9, _assessment()) == ()
    assert desired_dependency_ids(
        9, _assessment(DependencyState.COMPLETED, DependencyState.COMPLETED)
    ) == ("1", "2")


def test_desired_ids_marker_is_not_a_completed_id() -> None:
    assessment = _assessment(DependencyState.WAITING)

    markers = set(desired_dependency_ids(9, assessment)) - {"1"}
    assert markers == {"pending-dependency:9:1"}
    assert not markers & completed_dependency_ids((assessment,))


def test_desired_ids_keep_unresolved_and_unavailable_sentinels() -> None:
    unresolved = _assessment(
        DependencyState.WAITING,
        unresolved=(UnresolvedDependency(raw="missing", reason=REASON_MISSING),),
    )

    assert desired_dependency_ids(9, unresolved) == (
        "1",
        "pending-dependency:9:1",
        "unresolved-dependency:9:0",
    )
    assert desired_dependency_ids(9, None) == (
        "unresolved-dependency:9:assessment-unavailable",
    )
