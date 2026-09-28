"""Issue-only, idempotent outcome posting contracts for #1003."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from orchestune.outcome_record import OutcomeRecord


def _record(completion_id: str = "completion-1") -> OutcomeRecord:
    return OutcomeRecord(
        result="done",
        issue=1003,
        pr=42,
        claim_id="claim-1",
        head_sha="a" * 40,
        completion_id=completion_id,
    )


class TestPostIssueOutcome:
    def test_posts_only_after_every_comment_page_is_checked(self) -> None:
        from orchestune.complete.posting import PostingRequest, post_issue_outcome

        forge = SimpleNamespace(
            list_all_issue_comments=Mock(return_value=[]),
            create_issue_comment=Mock(
                return_value={"id": 88, "html_url": "https://example.test/comments/88"}
            ),
        )

        result = post_issue_outcome(
            PostingRequest(issue_number=1003, outcome_record=_record()), forge=forge
        )

        assert result.comment_id == "88"
        assert result.comment_url == "https://example.test/comments/88"
        forge.list_all_issue_comments.assert_called_once_with(1003)
        forge.create_issue_comment.assert_called_once_with(1003, _record().render())

    def test_reuses_matching_outcome_on_a_later_page(self) -> None:
        from orchestune.complete.posting import PostingRequest, post_issue_outcome

        matching = {
            "id": 12,
            "html_url": "https://example.test/comments/12",
            "body": _record().render(),
            "created_at": "2026-09-25T00:00:00Z",
        }

        forge = SimpleNamespace(
            list_all_issue_comments=Mock(return_value=[matching]),
            create_issue_comment=Mock(),
        )

        result = post_issue_outcome(
            PostingRequest(issue_number=1003, outcome_record=_record()), forge=forge
        )

        assert result.comment_id == "12"
        assert result.comment_url.endswith("/12")
        assert result.reused is True
        forge.create_issue_comment.assert_not_called()

    def test_response_loss_rechecks_before_a_second_post(self) -> None:
        from orchestune.complete.posting import PostingRequest, post_issue_outcome

        matching = {
            "id": 13,
            "html_url": "https://example.test/comments/13",
            "body": _record().render(),
            "created_at": "2026-09-25T00:00:00Z",
        }
        forge = SimpleNamespace(
            list_all_issue_comments=Mock(side_effect=[[], [matching]]),
            create_issue_comment=Mock(
                side_effect=RuntimeError("connection dropped after POST")
            ),
        )

        result = post_issue_outcome(
            PostingRequest(issue_number=1003, outcome_record=_record()), forge=forge
        )

        assert result.comment_id == "13"
        assert result.reused is True
        assert forge.create_issue_comment.call_count == 1
        assert forge.list_all_issue_comments.call_count == 2

    def test_unknown_lookup_never_posts_blindly(self) -> None:
        from orchestune.complete.posting import (
            OutcomeLookupUnknownError,
            PostingRequest,
            post_issue_outcome,
        )

        forge = SimpleNamespace(
            list_all_issue_comments=Mock(side_effect=RuntimeError("GitHub unavailable"))
        )

        with pytest.raises(OutcomeLookupUnknownError):
            post_issue_outcome(PostingRequest(1003, _record()), forge=forge)


class TestCompletionService:
    def test_dry_run_has_no_ci_journal_or_post_side_effects(self, tmp_path) -> None:
        from orchestune.complete.contracts import CompleteRequest
        from orchestune.complete.preflight import CompletePreflight
        from orchestune.complete.service import complete_task

        request = CompleteRequest.not_needed(
            1003, dry_run=True, state_path=tmp_path / "run_state.json"
        )
        workspace = SimpleNamespace(
            run_state_path=tmp_path / "run_state.json", repository_identity="repo"
        )
        state = SimpleNamespace(active_worktrees={}, completion_replay_receipts={})
        with (
            patch(
                "orchestune.complete.service.resolve_claim_workspace",
                return_value=workspace,
            ),
            patch(
                "orchestune.complete.service.load_run_state_readonly",
                return_value=state,
            ),
            patch(
                "orchestune.complete.service.evaluate_complete_preflight",
                return_value=CompletePreflight(accepted=True),
            ),
            patch("orchestune.complete.service.run_local_ci_if_needed") as run_ci,
            patch("orchestune.complete.service.reserve_completion_locked") as reserve,
            patch("orchestune.complete.service.post_issue_outcome") as post,
        ):
            result = complete_task(request, forge=SimpleNamespace(_run=lambda *_: "[]"))

        assert result.success is True
        assert result.preview is True
        assert result.handed_off_to_gc is False
        run_ci.assert_not_called()
        reserve.assert_not_called()
        post.assert_not_called()

    def test_rechecks_then_persists_comment_evidence_after_post(
        self, tmp_path, monkeypatch
    ) -> None:
        from complete_lifecycle_test_support import lifecycle_environment

        from orchestune.complete.service import complete_task
        from orchestune.ledger.run_state import load_run_state_readonly

        request, forge, preflight = lifecycle_environment(tmp_path, monkeypatch)
        result = complete_task(request, forge=forge)
        assert result.success
        assert preflight.call_count == 2
        assert result.outcome_record is not None
        assert result.outcome_record.completion_id == result.completion_id
        state = load_run_state_readonly(request.state_path)
        active = state.active_worktrees["1110"]
        assert active.completion_comment_url is not None
        assert active.completion_comment_url.endswith("/1")
        assert active.completion_payload is not None
        assert active.completion_payload["body"] == result.outcome_record.render()
        assert active.completion_handoff_ready
        assert state.completion_replay_receipts

    def test_unclaimed_not_needed_posts_with_a_stable_identity(self, tmp_path) -> None:
        from orchestune.complete.contracts import CompleteRequest
        from orchestune.complete.posting import PostingResult
        from orchestune.complete.preflight import CompletePreflight
        from orchestune.complete.service import complete_task

        request = CompleteRequest.not_needed(
            1003, state_path=tmp_path / "run_state.json", worktree_root=Path.cwd()
        )
        workspace = SimpleNamespace(
            run_state_path=request.state_path, repository_identity="repo"
        )
        state = SimpleNamespace(active_worktrees={}, completion_replay_receipts={})
        with (
            patch(
                "orchestune.complete.service.resolve_claim_workspace",
                return_value=workspace,
            ),
            patch(
                "orchestune.complete.service.load_run_state_readonly",
                return_value=state,
            ),
            patch(
                "orchestune.complete.service.evaluate_complete_preflight",
                return_value=CompletePreflight(accepted=True),
            ),
            patch("orchestune.complete.service.reserve_completion_locked") as reserve,
            patch(
                "orchestune.complete.service.post_issue_outcome",
                return_value=PostingResult(
                    "7", "https://example.test/comments/7", False
                ),
            ) as post,
        ):
            result = complete_task(request, forge=SimpleNamespace())

        assert result.success is True
        assert result.outcome_record is not None
        assert result.outcome_record is not None
        assert result.outcome_record.completion_id == "unclaimed-not-needed-1003"
        reserve.assert_not_called()
        assert post.call_args.args[0].outcome_record == result.outcome_record
