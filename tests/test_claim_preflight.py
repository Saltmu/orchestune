"""Tests for orchestune.claim.preflight validation and base resolution."""

from __future__ import annotations

from typing import Any

import pytest

from orchestune.claim.contracts import (
    ClaimExitCode,
    ClaimFailureReason,
    ReservationKind,
)
from orchestune.claim.preflight import (
    evaluate_claim_preflight,
    resolve_claim_base,
    resolve_claim_subtask_id,
)
from orchestune.dispatch.dependency_assessment import (
    AssessedDependency,
    DependencyAssessment,
    DependencyState,
)
from orchestune.dispatch.dependency_resolution import UnresolvedDependency
from orchestune.dispatch.locks import (
    KIND_BRANCH,
    KIND_PR,
    ExternalLockConflict,
    ExternalLockScanResult,
)
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord


def _make_issue(
    number: int = 938,
    title: str = "[FEAT] claim-preflight-validation: Issue状態の検証",
    body: str = "",
    labels: tuple[str, ...] = (StatusLabel.QUEUED,),
    state: str = "OPEN",
    parent: dict[str, Any] | None = None,
    blocked_by: tuple[int, ...] = (),
) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=title,
        body=body,
        labels=labels,
        created_at="2026-09-20T10:00:00Z",
        state=state,
        parent=parent,
        blocked_by=blocked_by,
    )


def _footprint_body(subtask_id: str = "claim-preflight-validation") -> str:
    return (
        f"## Footprint\n\n"
        f"```yaml\n"
        f"subtask_id: {subtask_id}\n"
        f"footprint: [orchestune/claim/preflight.py]\n"
        f"symbols: [evaluate_claim_preflight]\n"
        f"parent_issue_number: 893\n"
        f"```\n"
    )


class DummyBaseView:
    """Mock DependencyPolicyView and parent resolver for resolve_claim_base."""

    def __init__(
        self,
        assessments: dict[int, DependencyAssessment] | None = None,
        canonical_branches: dict[int, str] | None = None,
        parent_numbers: dict[int, int | None] | None = None,
        default_base: str = "origin/main",
    ) -> None:
        self._assessments = assessments or {}
        self._canonical_branches = canonical_branches or {}
        self._parent_numbers = parent_numbers or {}
        self.default_base = default_base

    def assess_dependencies(self, issue_number: int) -> DependencyAssessment | None:
        return self._assessments.get(issue_number)

    def canonical_branch(self, issue_number: int) -> str | None:
        return self._canonical_branches.get(issue_number)

    def parent_issue_number(self, issue_number: int) -> int | None:
        return self._parent_numbers.get(issue_number)


class TestResolveClaimSubtaskId:
    def test_extracts_subtask_id_from_footprint_yaml(self) -> None:
        issue = _make_issue(body=_footprint_body("my-custom-subtask"))
        assert resolve_claim_subtask_id(issue) == "my-custom-subtask"

    def test_falls_back_to_stable_issue_id_when_yaml_absent(self) -> None:
        issue = _make_issue(number=42, body="Plain issue with no footprint block")
        assert resolve_claim_subtask_id(issue) == "task-42"

    def test_falls_back_when_subtask_id_in_yaml_is_empty(self) -> None:
        issue = _make_issue(
            number=77,
            body="```yaml\nsubtask_id: ''\nfootprint: [a.py]\n```",
        )
        assert resolve_claim_subtask_id(issue) == "task-77"

    def test_subtask_id_does_not_change_when_title_changes(self) -> None:
        body = _footprint_body("stable-id")
        issue1 = _make_issue(number=938, title="Old Title", body=body)
        issue2 = _make_issue(number=938, title="New Modified Title", body=body)
        assert resolve_claim_subtask_id(issue1) == resolve_claim_subtask_id(issue2)

        # Also holds for fallback
        plain1 = _make_issue(number=938, title="Old Title", body="no footprint")
        plain2 = _make_issue(number=938, title="New Title", body="no footprint")
        assert resolve_claim_subtask_id(plain1) == resolve_claim_subtask_id(plain2)
        assert resolve_claim_subtask_id(plain1) == "task-938"


class TestResolveClaimBase:
    def test_stack_target_returns_stack_base(self) -> None:
        # Issue 20 has dependency 10 which is CI_PASSED_UNMERGED
        # Dependency 10 has no dependencies of its own and canonical branch
        view = DummyBaseView(
            assessments={
                20: DependencyAssessment(
                    resolved=(
                        AssessedDependency(10, DependencyState.CI_PASSED_UNMERGED),
                    )
                ),
                10: DependencyAssessment(resolved=()),
            },
            canonical_branches={10: "claude/issue-10-dep"},
            parent_numbers={20: 893},
        )
        decision = resolve_claim_base(20, view)
        assert decision.allowed is True
        assert decision.kind == "stack"
        assert decision.base_ref == "claude/issue-10-dep"
        assert decision.target_issue_number == 10
        assert decision.failure is None

    def test_all_dependencies_completed_resolves_parent_base(self) -> None:
        # Issue 20 has completed dependencies and parent issue 893
        view = DummyBaseView(
            assessments={
                20: DependencyAssessment(
                    resolved=(AssessedDependency(10, DependencyState.COMPLETED),)
                ),
            },
            parent_numbers={20: 893},
        )
        decision = resolve_claim_base(20, view)
        assert decision.allowed is True
        assert decision.kind == "normal"
        assert decision.base_ref == "parent/issue-893"
        assert decision.failure is None

    def test_no_dependencies_standalone_resolves_default_base(self) -> None:
        # Standalone issue with no parent and no dependencies
        view = DummyBaseView(
            assessments={20: DependencyAssessment(resolved=())},
            parent_numbers={20: None},
            default_base="origin/main",
        )
        decision = resolve_claim_base(20, view)
        assert decision.allowed is True
        assert decision.kind == "normal"
        assert decision.base_ref == "origin/main"
        assert decision.failure is None

    def test_unresolved_dependencies_rejects_with_blocking_issue(self) -> None:
        # Issue 20 has unresolved dependency on issue 10 (WAITING)
        view = DummyBaseView(
            assessments={
                20: DependencyAssessment(
                    resolved=(AssessedDependency(10, DependencyState.WAITING),)
                ),
            },
            parent_numbers={20: 893},
        )
        decision = resolve_claim_base(20, view)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES
        assert decision.failure.conflicting_issue_number == 10
        assert decision.base_ref is None

    def test_unresolved_diagnostics_rejects(self) -> None:
        # Issue 20 has diagnostic unresolved dependency
        view = DummyBaseView(
            assessments={
                20: DependencyAssessment(
                    unresolved=(
                        UnresolvedDependency(raw="missing-dep", reason="not-found"),
                    )
                ),
            },
            parent_numbers={20: 893},
        )
        decision = resolve_claim_base(20, view)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES


class TestEvaluateClaimPreflight:
    def test_e1_issue_not_found(self) -> None:
        decision = evaluate_claim_preflight(None)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.ISSUE_NOT_FOUND
        assert decision.failure.exit_code == ClaimExitCode.ISSUE_NOT_FOUND

    def test_e2_issue_closed(self) -> None:
        issue = _make_issue(state="CLOSED")
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.ISSUE_CLOSED
        assert decision.failure.exit_code == ClaimExitCode.ISSUE_CLOSED

    def test_e3_already_in_progress(self) -> None:
        issue = _make_issue(labels=(StatusLabel.IN_PROGRESS,))
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.ALREADY_IN_PROGRESS
        assert decision.failure.exit_code == ClaimExitCode.ALREADY_IN_PROGRESS

    @pytest.mark.parametrize(
        "label",
        [
            StatusLabel.BLOCKED_HUMAN_REVIEW,
            StatusLabel.MANUAL_MERGE_REQUIRED,
        ],
    )
    def test_e4_terminal_escalation(self, label: StatusLabel) -> None:
        issue = _make_issue(labels=(label,))
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.TERMINAL_ESCALATION
        assert decision.failure.exit_code == ClaimExitCode.TERMINAL_ESCALATION

    def test_e5_blocked_with_unresolved_dependencies_rejected(self) -> None:
        issue = _make_issue(labels=(StatusLabel.BLOCKED,))
        view = DummyBaseView(
            assessments={
                938: DependencyAssessment(
                    resolved=(AssessedDependency(936, DependencyState.WAITING),)
                ),
            },
            parent_numbers={938: 893},
        )
        decision = evaluate_claim_preflight(issue, view=view)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES
        assert decision.failure.conflicting_issue_number == 936

    def test_e5_blocked_with_stack_target_allowed(self) -> None:
        issue = _make_issue(
            labels=(StatusLabel.BLOCKED,),
            body=_footprint_body("stackable-task"),
        )
        view = DummyBaseView(
            assessments={
                938: DependencyAssessment(
                    resolved=(
                        AssessedDependency(936, DependencyState.CI_PASSED_UNMERGED),
                    )
                ),
                936: DependencyAssessment(resolved=()),
            },
            canonical_branches={936: "claude/issue-936-ownership-state-schema"},
            parent_numbers={938: 893},
        )
        decision = evaluate_claim_preflight(issue, view=view)
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.base_ref == "claude/issue-936-ownership-state-schema"
        assert decision.stack_target_issue_number == 936
        assert decision.subtask_id == "stackable-task"

    def test_e6_external_lock_label_rejected(self) -> None:
        issue = _make_issue(labels=(StatusLabel.EXTERNAL_LOCK,))
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.EXTERNAL_LOCK_CONFLICT

    def test_e6_external_lock_scan_conflict_rejected_with_diagnostics(self) -> None:
        issue = _make_issue(labels=(StatusLabel.QUEUED,))
        scan_result = ExternalLockScanResult(
            to_lock=[],
            to_unlock=[],
            conflicts={
                938: (
                    ExternalLockConflict(
                        kind=KIND_BRANCH,
                        source="feat/conflicting-branch",
                        files=("orchestune/claim/preflight.py",),
                    ),
                )
            },
        )
        decision = evaluate_claim_preflight(issue, external_locks=scan_result)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.EXTERNAL_LOCK_CONFLICT
        assert decision.failure.conflicting_branch == "feat/conflicting-branch"
        assert "feat/conflicting-branch" in decision.failure.message

    def test_e6_external_lock_pr_conflict_with_issue_number(self) -> None:
        issue = _make_issue(labels=(StatusLabel.QUEUED,))
        scan_result = ExternalLockScanResult(
            to_lock=[],
            to_unlock=[],
            conflicts={
                938: (
                    ExternalLockConflict(
                        kind=KIND_PR,
                        source="#950",
                        files=("orchestune/claim/preflight.py",),
                    ),
                )
            },
        )
        decision = evaluate_claim_preflight(issue, external_locks=scan_result)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.EXTERNAL_LOCK_CONFLICT
        assert decision.failure.conflicting_issue_number == 950

    def test_conflicting_status_labels_rejected(self) -> None:
        issue = _make_issue(labels=(StatusLabel.QUEUED, StatusLabel.BLOCKED))
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is False
        assert decision.failure is not None
        # Conflicting labels must be rejected fail-closed
        assert decision.failure.reason in (
            ClaimFailureReason.CLAIM_CONFLICT,
            ClaimFailureReason.UNRESOLVED_DEPENDENCIES,
            ClaimFailureReason.TERMINAL_ESCALATION,
        )

    def test_h1_plain_issue_without_footprint_accepted_as_repository_reservation(
        self,
    ) -> None:
        issue = _make_issue(
            number=500,
            body="Just a simple issue with no footprint YAML block.",
            labels=(StatusLabel.QUEUED,),
        )
        view = DummyBaseView(
            assessments={500: DependencyAssessment(resolved=())},
            parent_numbers={500: None},
            default_base="origin/main",
        )
        decision = evaluate_claim_preflight(issue, view=view)
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.subtask_id == "task-500"
        assert decision.reservation_kind == ReservationKind.REPOSITORY
        assert decision.base_ref == "origin/main"

    def test_footprint_issue_accepted_as_footprint_reservation(self) -> None:
        issue = _make_issue(
            number=938,
            body=_footprint_body("claim-preflight-validation"),
            labels=(StatusLabel.QUEUED,),
        )
        view = DummyBaseView(
            assessments={938: DependencyAssessment(resolved=())},
            parent_numbers={938: 893},
        )
        decision = evaluate_claim_preflight(issue, view=view)
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.subtask_id == "claim-preflight-validation"
        assert decision.reservation_kind == ReservationKind.FOOTPRINT
        assert decision.base_ref == "parent/issue-893"

    def test_malformed_yaml_falls_back_safely(self) -> None:
        issue = _make_issue(
            number=700,
            body="```yaml\n: invalid: yaml: - content\n```",
            labels=(StatusLabel.QUEUED,),
        )
        view = DummyBaseView(
            assessments={700: DependencyAssessment(resolved=())},
            parent_numbers={700: None},
        )
        decision = evaluate_claim_preflight(issue, view=view)
        assert decision.allowed is True
        assert decision.subtask_id == "task-700"
        assert decision.reservation_kind == ReservationKind.REPOSITORY

    def test_done_or_not_needed_labels_rejected(self) -> None:
        for lbl in (StatusLabel.DONE, StatusLabel.NOT_NEEDED):
            issue = _make_issue(labels=(lbl,))
            decision = evaluate_claim_preflight(issue)
            assert decision.allowed is False
            assert decision.failure is not None
            assert decision.failure.reason == ClaimFailureReason.CLAIM_CONFLICT

    def test_assessment_only_with_pending_dependencies_rejected(self) -> None:
        issue = _make_issue(number=800, labels=(StatusLabel.QUEUED,))
        assessment = DependencyAssessment(
            resolved=(AssessedDependency(799, DependencyState.WAITING),)
        )
        decision = evaluate_claim_preflight(issue, assessment=assessment)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES
        assert decision.failure.conflicting_issue_number == 799

    def test_multiple_stack_candidates_rejected_without_fallback(self) -> None:
        view = DummyBaseView(
            assessments={
                20: DependencyAssessment(
                    resolved=(
                        AssessedDependency(10, DependencyState.CI_PASSED_UNMERGED),
                        AssessedDependency(11, DependencyState.CI_PASSED_UNMERGED),
                    )
                ),
            },
            parent_numbers={20: 893},
        )
        decision = resolve_claim_base(20, view)
        assert decision.allowed is False
        assert decision.base_ref is None
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES

    def test_claim_base_resolution_view_protocol_conformance(self) -> None:
        view = DummyBaseView()
        from orchestune.claim.preflight import ClaimBaseResolutionView

        assert isinstance(view, ClaimBaseResolutionView)

    def test_assessment_only_with_status_blocked_rejected(self) -> None:
        issue = _make_issue(number=800, labels=(StatusLabel.BLOCKED,))
        assessment = DependencyAssessment(
            resolved=(AssessedDependency(799, DependencyState.COMPLETED),)
        )
        # Even if assessment dependencies are completed, status:blocked without view cannot verify stacking
        decision = evaluate_claim_preflight(issue, assessment=assessment)
        assert decision.allowed is False
        assert decision.failure is not None
        assert decision.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES
        assert "no resolution view" in decision.failure.message

    def test_footprint_issue_without_view_resolves_parent_base(self) -> None:
        issue = _make_issue(
            number=938,
            body=_footprint_body("claim-preflight-validation"),
            labels=(StatusLabel.QUEUED,),
        )
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.base_ref == "parent/issue-893"

    def test_native_sub_issue_without_view_resolves_parent_base(self) -> None:
        issue = _make_issue(
            number=938,
            body="No footprint block",
            labels=(StatusLabel.QUEUED,),
            parent={"number": 894},
        )
        decision = evaluate_claim_preflight(issue)
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.base_ref == "parent/issue-894"

    def test_plain_issue_without_view_resolves_default_base(self) -> None:
        issue = _make_issue(
            number=500,
            body="Plain issue with no parent",
            labels=(StatusLabel.QUEUED,),
        )
        decision = evaluate_claim_preflight(issue, default_base="origin/main")
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.base_ref == "origin/main"

    def test_issue_with_parent_and_custom_default_base_uses_custom_base(self) -> None:
        issue = _make_issue(
            number=938,
            body=_footprint_body("claim-preflight-validation"),
            labels=(StatusLabel.QUEUED,),
        )
        decision = evaluate_claim_preflight(issue, default_base="custom-base")
        assert decision.allowed is True
        assert decision.failure is None
        assert decision.base_ref == "custom-base"
