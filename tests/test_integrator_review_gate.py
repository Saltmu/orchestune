"""Unit tests for pure child review gate decision logic and GitHub lookup."""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestune.integrator.review_gate import (
    ChildReviewGateFailure,
    ChildReviewGateInput,
    compute_review_gate_digest,
    decide_child_review_gate,
    fetch_child_review_gate_outcome,
    format_child_review_gate_escalation_comment,
    has_matching_review_gate_comment,
    parse_child_review_gate_digest,
)
from orchestune.outcome_record import (
    OutcomeLookupResult,
    OutcomeLookupState,
    OutcomeRecord,
    ReviewSummary,
)


def _make_pass_record(
    issue: int, sha: str = "a" * 40, bot: str = "claude", rounds: int = 1
) -> OutcomeRecord:
    return OutcomeRecord(
        result="done",
        issue=issue,
        pr=100 + issue,
        head_sha=sha,
        review=ReviewSummary(
            bot=bot,
            rounds=rounds,
            verdict="pass",
            reviewed_head_sha=sha,
        ),
    )


class TestDecideChildReviewGatePure:
    """decide_child_review_gate must evaluate all children and fail closed on any discrepancy."""

    def test_single_child_all_pass(self):
        sha = "1" * 40
        record = _make_pass_record(10, sha)
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is True
        assert decision.failures == ()
        assert decision.digest == ""

    def test_multiple_children_all_pass(self):
        sha1 = "1" * 40
        sha2 = "2" * 40
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha1,
                lookup_result=OutcomeLookupResult(
                    OutcomeLookupState.FOUND, _make_pass_record(10, sha1)
                ),
            ),
            ChildReviewGateInput(
                issue_number=20,
                subtask_id="task-2",
                expected_commit_oid=sha2,
                lookup_result=OutcomeLookupResult(
                    OutcomeLookupState.FOUND, _make_pass_record(20, sha2)
                ),
            ),
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is True
        assert decision.failures == ()

    def test_failure_legacy(self):
        """ReviewSummary with verdict=None is classified as 'legacy'."""
        sha = "1" * 40
        record = OutcomeRecord(
            result="done",
            issue=10,
            head_sha=sha,
            review=ReviewSummary(bot="claude", verdict=None),
        )
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "legacy"
        assert decision.failures[0].issue_number == 10
        assert decision.digest != ""

    def test_failure_skipped(self):
        """ReviewSummary with verdict='skipped' is classified as 'skipped'."""
        sha = "1" * 40
        record = OutcomeRecord(
            result="done",
            issue=10,
            head_sha=sha,
            review=ReviewSummary(bot="skip", verdict="skipped", reviewed_head_sha=sha),
        )
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "skipped"

    def test_failure_not_pass_verdict(self):
        """Verdict other than 'pass' or 'skipped' (e.g. 'fail', 'changes_requested') is classified as 'not_pass'."""
        sha = "1" * 40
        record = OutcomeRecord(
            result="done",
            issue=10,
            head_sha=sha,
            review=ReviewSummary(
                bot="claude", verdict="changes_requested", reviewed_head_sha=sha
            ),
        )
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "not_pass"

    def test_failure_not_pass_result_not_done(self):
        """result != 'done' is classified as 'not_pass'."""
        sha = "1" * 40
        record = OutcomeRecord(
            result="blocked",
            issue=10,
            head_sha=sha,
            reason="base-branch-red",
            review=ReviewSummary(bot="claude", verdict="pass", reviewed_head_sha=sha),
        )
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "not_pass"

    def test_failure_sha_mismatch_reviewed_head_sha(self):
        """reviewed_head_sha does not match expected_commit_oid."""
        sha_expected = "1" * 40
        sha_reviewed = "2" * 40
        record = OutcomeRecord(
            result="done",
            issue=10,
            head_sha=sha_expected,
            review=ReviewSummary(
                bot="claude", verdict="pass", reviewed_head_sha=sha_reviewed
            ),
        )
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha_expected,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "sha_mismatch"

    def test_failure_sha_mismatch_head_sha(self):
        """head_sha does not match expected_commit_oid."""
        sha_expected = "1" * 40
        sha_head = "3" * 40
        record = OutcomeRecord(
            result="done",
            issue=10,
            head_sha=sha_head,
            review=ReviewSummary(
                bot="claude", verdict="pass", reviewed_head_sha=sha_expected
            ),
        )
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid=sha_expected,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.FOUND, record),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "sha_mismatch"

    def test_failure_absent(self):
        """OutcomeLookupState.ABSENT is classified as 'absent'."""
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid="1" * 40,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.ABSENT, None),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "absent"

    def test_failure_lookup_unknown(self):
        """OutcomeLookupState.UNKNOWN is classified as 'lookup_unknown'."""
        inputs = [
            ChildReviewGateInput(
                issue_number=10,
                subtask_id="task-1",
                expected_commit_oid="1" * 40,
                lookup_result=OutcomeLookupResult(OutcomeLookupState.UNKNOWN, None),
            )
        ]
        decision = decide_child_review_gate(inputs)
        assert decision.passed is False
        assert len(decision.failures) == 1
        assert decision.failures[0].reason == "lookup_unknown"


class TestDigestAndCommentFormatting:
    """Digest computation must be deterministic and comments must include restart instructions."""

    def test_digest_is_deterministic_and_order_independent(self):
        f1 = ChildReviewGateFailure(
            issue_number=10, reason="skipped", expected_sha="1" * 40
        )
        f2 = ChildReviewGateFailure(
            issue_number=20, reason="sha_mismatch", expected_sha="2" * 40
        )

        d1 = compute_review_gate_digest([f1, f2])
        d2 = compute_review_gate_digest([f2, f1])
        assert d1 == d2
        assert len(d1) == 64

    def test_comment_formatting_contains_marker_and_digest(self):
        f1 = ChildReviewGateFailure(
            issue_number=10,
            subtask_id="task-a",
            reason="skipped",
            expected_sha="1" * 40,
        )
        f2 = ChildReviewGateFailure(
            issue_number=20,
            subtask_id="task-b",
            reason="sha_mismatch",
            expected_sha="2" * 40,
        )
        digest = compute_review_gate_digest([f1, f2])
        comment = format_child_review_gate_escalation_comment(
            failures=[f1, f2], digest=digest
        )

        assert f"<!-- orchestune:child-review-gate digest={digest} -->" in comment
        assert "#10" in comment
        assert "`task-a`" in comment
        assert "skipped" in comment
        assert "#20" in comment
        assert "`task-b`" in comment
        assert "sha_mismatch" in comment
        # Restart guidance must distinguish skipped/legacy, sha_mismatch, lookup_unknown
        assert "--child-review-gate off" in comment
        assert "再レビュー" in comment

    def test_digest_parsing_and_idempotency_detection(self):
        digest = "abcdef" * 10 + "1234"
        body = f"Some header\n<!-- orchestune:child-review-gate digest={digest} -->\nSome text"
        assert parse_child_review_gate_digest(body) == digest

        comments = [
            {"body": "Hello world"},
            {"body": body},
        ]
        assert has_matching_review_gate_comment(comments, digest) is True
        assert has_matching_review_gate_comment(comments, "differentdigest") is False


class TestFetchChildReviewGateOutcome:
    """Integration of OutcomeLookup via Forge.list_comments and find_child_outcome_record."""

    def test_fetch_outcome_success(self):
        forge = MagicMock()
        record = _make_pass_record(10)
        forge.list_comments.return_value = [
            {"body": record.render(), "created_at": "2026-08-01T00:00:00Z"}
        ]

        result = fetch_child_review_gate_outcome(forge, 10)
        assert result.state == OutcomeLookupState.FOUND
        assert result.record == record

    def test_fetch_outcome_absent(self):
        forge = MagicMock()
        forge.list_comments.return_value = [{"body": "no outcome here"}]

        result = fetch_child_review_gate_outcome(forge, 10)
        assert result.state == OutcomeLookupState.ABSENT
        assert result.record is None

    def test_fetch_outcome_unknown_on_forge_exception(self):
        forge = MagicMock()
        forge.list_comments.side_effect = RuntimeError("network error")

        result = fetch_child_review_gate_outcome(forge, 10)
        assert result.state == OutcomeLookupState.UNKNOWN
        assert result.record is None
