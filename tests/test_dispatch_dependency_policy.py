from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from orchestune.dependencies.assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dependencies.policy import (
    StackTarget,
    decide_stack_target,
    dependencies_completed,
    has_pending_dependencies,
)
from orchestune.dependencies.resolution import (
    REASON_MISSING,
    UnresolvedDependency,
)


def _assessment(
    *dependencies: tuple[int, DependencyState],
    unresolved: tuple[UnresolvedDependency, ...] = (),
) -> DependencyAssessment:
    return DependencyAssessment(
        resolved=tuple(AssessedDependency(*dependency) for dependency in dependencies),
        unresolved=unresolved,
    )


@dataclass
class FakeDependencyPolicyView:
    assessments: dict[int, DependencyAssessment | None]
    branches: dict[int, str | None] = field(default_factory=dict)
    assessment_calls: list[int] = field(default_factory=list)
    branch_calls: list[int] = field(default_factory=list)

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        self.assessment_calls.append(issue_number)
        return self.assessments.get(issue_number)

    def canonical_branch(self, issue_number: int) -> str | None:
        self.branch_calls.append(issue_number)
        return self.branches.get(issue_number)


@pytest.mark.parametrize(
    ("subject", "assessment", "reason", "blocking"),
    [
        (10, None, "assessment-unavailable", 10),
        (
            10,
            _assessment(
                unresolved=(UnresolvedDependency(raw="missing", reason=REASON_MISSING),)
            ),
            "unresolved-dependency",
            10,
        ),
        (10, _assessment(), "no-stack-dependency", 10),
        (
            10,
            _assessment((1, DependencyState.COMPLETED)),
            "no-stack-dependency",
            10,
        ),
        (
            10,
            _assessment((3, DependencyState.WAITING)),
            "dependency-not-ci-passed",
            3,
        ),
        (
            10,
            _assessment((2, DependencyState.CHANGES_REQUESTED)),
            "dependency-not-ci-passed",
            2,
        ),
        (
            10,
            _assessment(
                (2, DependencyState.CI_PASSED_UNMERGED),
                (3, DependencyState.CI_PASSED_UNMERGED),
            ),
            "multiple-stack-dependencies",
            10,
        ),
        (
            10,
            _assessment((10, DependencyState.CI_PASSED_UNMERGED)),
            "self-dependency",
            10,
        ),
    ],
)
def test_decide_stack_target_rejects_direct_dependency_cases(
    subject: int,
    assessment: DependencyAssessment | None,
    reason: str,
    blocking: int,
) -> None:
    view = FakeDependencyPolicyView({subject: assessment})

    decision = decide_stack_target(subject, view)

    assert decision.target is None
    assert decision.reason == reason
    assert decision.blocking_issue_number == blocking
    assert decision.assessment == assessment
    assert view.assessment_calls == [subject]
    assert view.branch_calls == []


@pytest.mark.parametrize(
    ("grand_assessment", "reason"),
    [
        (None, "grand-assessment-unavailable"),
        (
            _assessment(
                unresolved=(UnresolvedDependency(raw="c", reason=REASON_MISSING),)
            ),
            "grand-unresolved-dependency",
        ),
        (
            _assessment((1, DependencyState.CI_PASSED_UNMERGED)),
            "grand-dependency-incomplete",
        ),
        (
            _assessment((1, DependencyState.WAITING)),
            "grand-dependency-incomplete",
        ),
    ],
)
def test_decide_stack_target_rejects_unavailable_or_incomplete_grand_dependencies(
    grand_assessment: DependencyAssessment | None,
    reason: str,
) -> None:
    subject = _assessment((2, DependencyState.CI_PASSED_UNMERGED))
    view = FakeDependencyPolicyView({3: subject, 2: grand_assessment})

    decision = decide_stack_target(3, view)

    assert decision.target is None
    assert decision.reason == reason
    assert decision.blocking_issue_number == 2
    assert decision.assessment == grand_assessment
    assert view.assessment_calls == [3, 2]
    assert view.branch_calls == []


@pytest.mark.parametrize("branch", [None, ""])
def test_decide_stack_target_rejects_missing_canonical_branch(
    branch: str | None,
) -> None:
    subject = _assessment((2, DependencyState.CI_PASSED_UNMERGED))
    grand = _assessment((1, DependencyState.COMPLETED))
    view = FakeDependencyPolicyView({3: subject, 2: grand}, {2: branch})

    decision = decide_stack_target(3, view)

    assert decision.target is None
    assert decision.reason == "branch-unavailable"
    assert decision.blocking_issue_number == 2
    assert decision.assessment == grand
    assert view.assessment_calls == [3, 2]
    assert view.branch_calls == [2]


@pytest.mark.parametrize(
    "grand_assessment",
    [
        _assessment(),
        _assessment((1, DependencyState.COMPLETED)),
    ],
    ids=["no-grand-dependencies", "all-grand-dependencies-completed"],
)
def test_decide_stack_target_returns_the_only_safe_dependency(
    grand_assessment: DependencyAssessment,
) -> None:
    subject = _assessment(
        (1, DependencyState.COMPLETED),
        (2, DependencyState.CI_PASSED_UNMERGED),
    )
    view = FakeDependencyPolicyView(
        {3: subject, 2: grand_assessment}, {2: "feat/issue-2-b"}
    )

    decision = decide_stack_target(3, view)

    assert decision.target == StackTarget(2, "feat/issue-2-b")
    assert decision.reason is None
    assert decision.blocking_issue_number is None
    assert decision.assessment == subject
    assert view.assessment_calls == [3, 2]
    assert view.branch_calls == [2]


_MISSING = UnresolvedDependency(raw="missing", reason=REASON_MISSING)

# (assessment, dependencies_completed); has_pending_dependencies is its negation.
_COMPLETION_CASES = [
    pytest.param(None, False, id="assessment-unavailable"),
    pytest.param(_assessment(), True, id="no-dependencies"),
    pytest.param(
        _assessment((1, DependencyState.COMPLETED)), True, id="single-completed"
    ),
    pytest.param(
        _assessment((1, DependencyState.COMPLETED), (2, DependencyState.COMPLETED)),
        True,
        id="all-completed",
    ),
    pytest.param(
        _assessment((1, DependencyState.CI_PASSED_UNMERGED)),
        False,
        id="ci-passed-unmerged",
    ),
    pytest.param(
        _assessment((1, DependencyState.CHANGES_REQUESTED)),
        False,
        id="changes-requested",
    ),
    pytest.param(_assessment((1, DependencyState.WAITING)), False, id="waiting"),
    pytest.param(
        _assessment((1, DependencyState.COMPLETED), (2, DependencyState.WAITING)),
        False,
        id="completed-and-waiting",
    ),
    pytest.param(_assessment(unresolved=(_MISSING,)), False, id="unresolved-only"),
    pytest.param(
        _assessment((1, DependencyState.COMPLETED), unresolved=(_MISSING,)),
        False,
        id="completed-and-unresolved",
    ),
]


@pytest.mark.parametrize(("assessment", "expected"), _COMPLETION_CASES)
def test_dependencies_completed_truth_table(
    assessment: DependencyAssessment | None, expected: bool
) -> None:
    assert dependencies_completed(assessment) is expected


@pytest.mark.parametrize(("assessment", "expected"), _COMPLETION_CASES)
def test_has_pending_dependencies_is_the_negation_of_completion(
    assessment: DependencyAssessment | None, expected: bool
) -> None:
    assert has_pending_dependencies(assessment) is (not expected)


@pytest.mark.parametrize(
    "state",
    [state for state in DependencyState if state is not DependencyState.COMPLETED],
)
def test_any_incomplete_state_keeps_dependencies_pending(
    state: DependencyState,
) -> None:
    assessment = _assessment((1, DependencyState.COMPLETED), (2, state))

    assert dependencies_completed(assessment) is False
    assert has_pending_dependencies(assessment) is True


def test_dependencies_package_exports_the_canonical_predicate() -> None:
    from orchestune import dependencies

    assert dependencies.dependencies_completed is dependencies_completed
    assert "dependencies_completed" in dependencies.__all__
