"""Tests for complete package shared contracts and domain models (#997)."""

from __future__ import annotations

import dataclasses

import pytest

from orchestune.claim.contracts import OwnerKind
from orchestune.complete import (
    BlockedPayload,
    CompleteExitCode,
    CompleteFailure,
    CompleteFailureReason,
    CompleteRequest,
    CompleteResult,
    CompleteStage,
    DonePayload,
    NotNeededPayload,
    can_transition,
    failure_reason_to_exit_code,
)
from orchestune.outcome_record import (
    REASON_BASE_BRANCH_RED,
    RESULT_BLOCKED,
    RESULT_DONE,
    RESULT_NOT_NEEDED,
    ReviewSummary,
)


class TestCompletePayloadsAndRequests:
    """Test input types and validation for done/not-needed/blocked (#997)."""

    def test_done_request_construction_and_validation(self):
        req = CompleteRequest.done(
            issue_number=997,
            pr=1001,
            owner_token="token-123",
            claim_id="claim-abc",
            owner_kind=OwnerKind.INTERACTIVE,
            review=ReviewSummary(bot="claude", rounds=1, verdict="approved"),
            ci="passed",
            baseline_regressions=("test_a",),
        )
        assert req.issue_number == 997
        assert req.result == RESULT_DONE
        assert req.owner_token == "token-123"
        assert req.claim_id == "claim-abc"
        assert req.owner_kind == OwnerKind.INTERACTIVE
        assert isinstance(req.payload, DonePayload)
        assert req.payload.pr == 1001
        assert req.payload.review.verdict == "approved"
        assert req.payload.ci == "passed"
        assert req.payload.baseline_regressions == ("test_a",)

        # Validation succeeds
        req.validate()

    def test_not_needed_request_construction_and_validation(self):
        req = CompleteRequest.not_needed(
            issue_number=997,
            owner_token="token-123",
            claim_id="claim-abc",
        )
        assert req.issue_number == 997
        assert req.result == RESULT_NOT_NEEDED
        assert isinstance(req.payload, NotNeededPayload)

        # Validation succeeds
        req.validate()

    def test_blocked_request_construction_and_validation(self):
        req = CompleteRequest.blocked(
            issue_number=997,
            reason=REASON_BASE_BRANCH_RED,
            base_sha="abc1234",
            attempt=1,
            owner_token="token-123",
            claim_id="claim-abc",
            review=ReviewSummary(bot="claude", rounds=2, verdict="timeout"),
        )
        assert req.issue_number == 997
        assert req.result == RESULT_BLOCKED
        assert isinstance(req.payload, BlockedPayload)
        assert req.payload.reason == REASON_BASE_BRANCH_RED
        assert req.payload.base_sha == "abc1234"
        assert req.payload.attempt == 1
        assert req.payload.review.verdict == "timeout"

        # Validation succeeds
        req.validate()

    def test_validation_rejects_mismatched_payloads(self):
        # done without DonePayload / without pr
        invalid_done = CompleteRequest(
            issue_number=997,
            result=RESULT_DONE,
            payload=None,
        )
        with pytest.raises(
            ValueError, match="Done request requires a DonePayload with pr"
        ):
            invalid_done.validate()

        # not-needed with DonePayload or BlockedPayload
        invalid_not_needed = CompleteRequest(
            issue_number=997,
            result=RESULT_NOT_NEEDED,
            payload=DonePayload(pr=100),
        )
        with pytest.raises(
            ValueError,
            match="Not-needed request must only have NotNeededPayload or None",
        ):
            invalid_not_needed.validate()

        invalid_not_needed_blocked = CompleteRequest(
            issue_number=997,
            result=RESULT_NOT_NEEDED,
            payload=BlockedPayload(reason="some reason"),
        )
        with pytest.raises(
            ValueError,
            match="Not-needed request must only have NotNeededPayload or None",
        ):
            invalid_not_needed_blocked.validate()

        # blocked without reason
        invalid_blocked = CompleteRequest(
            issue_number=997,
            result=RESULT_BLOCKED,
            payload=BlockedPayload(reason=""),
        )
        with pytest.raises(
            ValueError,
            match="Blocked request requires a BlockedPayload with non-empty reason",
        ):
            invalid_blocked.validate()

        # invalid result kind
        invalid_result = CompleteRequest(
            issue_number=997,
            result="unknown-kind",
            payload=None,
        )
        with pytest.raises(ValueError, match="Invalid complete result: 'unknown-kind'"):
            invalid_result.validate()

    def test_to_outcome_record_derivation(self):
        req_done = CompleteRequest.done(
            issue_number=997,
            pr=1001,
            review=ReviewSummary(bot="claude", rounds=1, verdict="approved"),
            ci="passed",
            baseline_regressions=("test_a",),
        )
        rec_done = req_done.to_outcome_record()
        assert rec_done.result == RESULT_DONE
        assert rec_done.issue == 997
        assert rec_done.pr == 1001
        assert rec_done.review.verdict == "approved"
        assert rec_done.ci == "passed"
        assert rec_done.baseline_regressions == ("test_a",)

        req_not_needed = CompleteRequest.not_needed(issue_number=997)
        rec_not_needed = req_not_needed.to_outcome_record()
        assert rec_not_needed.result == RESULT_NOT_NEEDED
        assert rec_not_needed.issue == 997
        assert rec_not_needed.pr is None

        req_blocked = CompleteRequest.blocked(
            issue_number=997,
            reason=REASON_BASE_BRANCH_RED,
            base_sha="def5678",
            attempt=2,
        )
        rec_blocked = req_blocked.to_outcome_record()
        assert rec_blocked.result == RESULT_BLOCKED
        assert rec_blocked.issue == 997
        assert rec_blocked.reason == REASON_BASE_BRANCH_RED
        assert rec_blocked.base_sha == "def5678"
        assert rec_blocked.attempt == 2

    def test_owner_token_is_masked_in_repr(self):
        req = CompleteRequest.done(
            issue_number=997,
            pr=1001,
            owner_token="super-secret-token-value",
        )
        assert "super-secret-token-value" not in repr(req)


class TestCompleteStageTransitions:
    """Test progression stages and state transitions (#997)."""

    def test_valid_sequential_transitions(self):
        # Normal complete progression
        assert can_transition(
            CompleteStage.INITIALIZING, CompleteStage.PREFLIGHT_VALIDATING
        )
        assert can_transition(
            CompleteStage.PREFLIGHT_VALIDATING, CompleteStage.EVIDENCE_VERIFYING
        )
        assert can_transition(
            CompleteStage.EVIDENCE_VERIFYING, CompleteStage.JOURNALING
        )
        assert can_transition(CompleteStage.JOURNALING, CompleteStage.POSTING)
        assert can_transition(CompleteStage.POSTING, CompleteStage.HANDED_OFF_TO_GC)

    def test_skipping_stages_or_backward_transitions_rejected(self):
        # Backward transitions rejected
        assert not can_transition(CompleteStage.HANDED_OFF_TO_GC, CompleteStage.POSTING)
        assert not can_transition(CompleteStage.POSTING, CompleteStage.INITIALIZING)

        # Illegal skipping rejected
        assert not can_transition(CompleteStage.INITIALIZING, CompleteStage.POSTING)
        assert not can_transition(
            CompleteStage.INITIALIZING, CompleteStage.HANDED_OFF_TO_GC
        )


class TestCompleteResultAndGCBoundary:
    """Test CLI and GC boundary: success means handoff to GC and excludes CompletionReceipt (#997)."""

    def test_successful_complete_result_represents_gc_handoff(self):
        res = CompleteResult.success_result(
            issue_number=997,
            result=RESULT_DONE,
            stage=CompleteStage.HANDED_OFF_TO_GC,
            claim_id="claim-abc",
            owner_kind=OwnerKind.INTERACTIVE,
            pr=1001,
        )
        assert res.success is True
        assert res.issue_number == 997
        assert res.result == RESULT_DONE
        assert res.stage == CompleteStage.HANDED_OFF_TO_GC
        assert res.handed_off_to_gc is True
        assert res.pr == 1001
        assert res.failure is None

    def test_complete_result_excludes_completion_receipt(self):
        """Acceptance Criteria: 成功はGC引渡しまでを意味し、CompletionReceiptを含まない。"""
        field_names = {f.name for f in dataclasses.fields(CompleteResult)}
        # Ensure CompletionReceipt is NOT a field of CompleteResult
        assert "receipt" not in field_names
        assert "completion_receipt" not in field_names

        # Inspect annotations to verify no reference to CompletionReceipt
        for field_type in CompleteResult.__annotations__.values():
            assert "CompletionReceipt" not in str(field_type)

        res = CompleteResult.success_result(
            issue_number=997,
            result=RESULT_DONE,
            stage=CompleteStage.HANDED_OFF_TO_GC,
        )
        assert not hasattr(res, "receipt")
        assert not hasattr(res, "completion_receipt")

    def test_successful_complete_result_rejects_non_handed_off_stage(self):
        """Codex finding: Reject successful results before GC handoff."""
        with pytest.raises(
            ValueError,
            match="Successful CompleteResult requires CompleteStage.HANDED_OFF_TO_GC",
        ):
            CompleteResult.success_result(
                issue_number=997,
                result=RESULT_DONE,
                stage=CompleteStage.INITIALIZING,
            )


class TestCompleteFailureAndExitCodes:
    """Test failure reasons and mapping to exit codes (#997)."""

    def test_failure_exit_code_derivation(self):
        failure = CompleteFailure(
            reason=CompleteFailureReason.CLAIM_NOT_FOUND,
            message="No active claim found for issue #997",
            issue_number=997,
        )
        assert failure.exit_code == CompleteExitCode.CLAIM_NOT_FOUND
        assert failure.exit_code == 11

    def test_all_failure_reasons_mapped(self):
        for reason in CompleteFailureReason:
            exit_code = failure_reason_to_exit_code(reason)
            assert isinstance(exit_code, CompleteExitCode)

    def test_unmapped_failure_reason_raises_key_error(self):
        with pytest.raises(KeyError, match="Unmapped complete failure reason"):
            failure_reason_to_exit_code("unknown-reason")  # type: ignore

    def test_complete_result_failure_representation(self):
        failure = CompleteFailure(
            reason=CompleteFailureReason.OWNER_TOKEN_MISMATCH,
            message="Caller owner token does not match active claim",
            issue_number=997,
        )
        res = CompleteResult.failure_result(
            issue_number=997,
            result=RESULT_DONE,
            stage=CompleteStage.PREFLIGHT_VALIDATING,
            failure=failure,
        )
        assert res.success is False
        assert res.handed_off_to_gc is False
        assert res.failure == failure
