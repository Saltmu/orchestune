"""Tests for orchestune.claim contracts and package scaffolding invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestune.claim import (
    ClaimExitCode,
    ClaimFailure,
    ClaimFailureReason,
    ClaimOutcome,
    ClaimRequest,
    ClaimStage,
    OwnerKind,
    ReservationKind,
    failure_reason_to_exit_code,
)
from orchestune.claim import cli as claim_cli


class TestClaimContracts:
    def test_owner_kind_values(self) -> None:
        assert {k.value for k in OwnerKind} == {"interactive", "dispatch"}
        assert OwnerKind.INTERACTIVE == "interactive"
        assert OwnerKind.DISPATCH == "dispatch"

    def test_reservation_kind_values(self) -> None:
        assert {k.value for k in ReservationKind} == {"footprint", "repository"}
        assert ReservationKind.FOOTPRINT == "footprint"
        assert ReservationKind.REPOSITORY == "repository"

    def test_claim_stage_values(self) -> None:
        expected_stages = {
            "validating",
            "fetched",
            "reserved",
            "worktree_prepared",
            "active_saved",
            "labeled",
            "completed",
        }
        assert {s.value for s in ClaimStage} == expected_stages

    def test_claim_failure_reason_exhaustive_mapping_to_exit_code(self) -> None:
        reasons = list(ClaimFailureReason)
        assert len(reasons) > 0

        mapped_codes: set[ClaimExitCode] = set()
        for reason in reasons:
            code = failure_reason_to_exit_code(reason)
            assert isinstance(code, ClaimExitCode)
            assert code != ClaimExitCode.SUCCESS
            assert int(code) > 0
            mapped_codes.add(code)

        # Each failure reason must have a mapped code
        assert len(mapped_codes) == len(
            reasons
        ), "Expected 1:1 mapping between reason and exit code"

    def test_failure_reason_to_exit_code_rejects_unknown(self) -> None:
        with pytest.raises(KeyError):
            failure_reason_to_exit_code("unknown_reason")  # type: ignore[arg-type]

    def test_claim_request_creation(self) -> None:
        req = ClaimRequest(issue_number=123)
        assert req.issue_number == 123
        assert req.owner_kind == OwnerKind.INTERACTIVE
        assert req.dry_run is False
        assert req.resume_claim_id is None
        assert req.owner_token is None

    def test_claim_failure_and_exit_code_property(self) -> None:
        failure = ClaimFailure(
            reason=ClaimFailureReason.ISSUE_NOT_FOUND,
            message="Issue #999 not found",
            conflicting_issue_number=None,
            next_actions=("Verify the issue number.",),
        )
        assert failure.reason == ClaimFailureReason.ISSUE_NOT_FOUND
        assert failure.exit_code == failure_reason_to_exit_code(
            ClaimFailureReason.ISSUE_NOT_FOUND
        )
        assert failure.next_actions == ("Verify the issue number.",)

    def test_claim_outcome_success_and_failure(self) -> None:
        success_outcome = ClaimOutcome(
            success=True,
            issue_number=123,
            claim_id="claim-123-abc",
            branch="feat/issue-123-foo",
            worktree_path=Path("/tmp/worktree/feat-issue-123"),
            base_ref="origin/main",
            owner_kind=OwnerKind.INTERACTIVE,
            reservation_kind=ReservationKind.FOOTPRINT,
            stage=ClaimStage.COMPLETED,
        )
        assert success_outcome.success is True
        assert success_outcome.failure is None
        assert success_outcome.claim_id == "claim-123-abc"

        fail_outcome = ClaimOutcome(
            success=False,
            issue_number=123,
            failure=ClaimFailure(
                reason=ClaimFailureReason.ALREADY_IN_PROGRESS,
                message="Already in progress",
            ),
        )
        assert fail_outcome.success is False
        assert fail_outcome.failure is not None
        assert fail_outcome.failure.reason == ClaimFailureReason.ALREADY_IN_PROGRESS


class TestClaimStubs:
    """Verify that remaining scaffolded functions raise NotImplementedError when called."""

    def test_cli_stubs_raise_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            claim_cli.main()
