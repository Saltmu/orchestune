from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_policy import (
    StackTarget,
    decide_stack_target,
    has_pending_dependencies,
)
from orchestune.dispatch.dependency_policy_compat import with_confirmed_completions
from orchestune.dispatch.dependency_resolution import (
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


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (None, True),
        (_assessment(), False),
        (_assessment((1, DependencyState.COMPLETED)), False),
        (_assessment((1, DependencyState.CI_PASSED_UNMERGED)), True),
        (_assessment((1, DependencyState.CHANGES_REQUESTED)), True),
        (_assessment((1, DependencyState.WAITING)), True),
        (
            _assessment(
                unresolved=(UnresolvedDependency(raw="missing", reason=REASON_MISSING),)
            ),
            True,
        ),
    ],
)
def test_has_pending_dependencies_truth_table(
    assessment: DependencyAssessment | None, expected: bool
) -> None:
    assert has_pending_dependencies(assessment) is expected


def test_confirmed_completion_adapter_reclassifies_only_confirmed_resolved_items() -> (
    None
):
    unresolved = UnresolvedDependency(raw="missing", reason=REASON_MISSING)
    original = _assessment(
        (1, DependencyState.WAITING),
        (2, DependencyState.CHANGES_REQUESTED),
        unresolved=(unresolved,),
    )
    confirmed = {1}
    source = FakeDependencyPolicyView(
        {3: original}, {1: "feat/issue-1-a", 2: "feat/issue-2-b"}
    )
    view = with_confirmed_completions(source, confirmed)
    confirmed.add(2)

    actual = view.assess_dependencies(3)

    assert actual == _assessment(
        (1, DependencyState.COMPLETED),
        (2, DependencyState.CHANGES_REQUESTED),
        unresolved=(unresolved,),
    )
    assert original.resolved[0].state is DependencyState.WAITING
    assert view.canonical_branch(2) == "feat/issue-2-b"


def test_confirmed_completion_adapter_preserves_none_and_requeries_live_view() -> None:
    source = FakeDependencyPolicyView({3: None})
    view = with_confirmed_completions(source, {1})

    assert view.assess_dependencies(3) is None
    source.assessments[3] = _assessment((1, DependencyState.WAITING))
    assert view.assess_dependencies(3) == _assessment((1, DependencyState.COMPLETED))
    assert source.assessment_calls == [3, 3]
