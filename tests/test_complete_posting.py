"""Issue-only, idempotent outcome posting contracts for #1003."""

from __future__ import annotations

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
    def test_dry_run_has_no_ci_journal_or_post_side_effects(
        self, tmp_path, monkeypatch
    ) -> None:
        from complete_lifecycle_test_support import lifecycle_environment

        from orchestune.complete.contracts import CompleteRequest
        from orchestune.complete.service import complete_task

        request, forge, _ = lifecycle_environment(tmp_path, monkeypatch)
        request = CompleteRequest.not_needed(
            1110,
            dry_run=True,
            state_path=request.state_path,
            owner_token="token",
            claim_id="claim-1110",
            worktree_root=tmp_path,
        )
        assert request.state_path is not None
        before = request.state_path.read_bytes()
        with (
            patch("orchestune.complete.service.run_local_ci_if_needed") as run_ci,
            patch("orchestune.complete.service.reserve_completion_locked") as reserve,
            patch("orchestune.complete.publication.post_issue_outcome") as post,
        ):
            result = complete_task(request, forge=forge)
        assert result.success and result.preview and not result.handed_off_to_gc
        assert request.state_path.read_bytes() == before
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
        assert active.completion_payload["outcome"] == active.completion_payload["body"]
        assert active.completion_handoff_ready
        assert state.completion_replay_receipts

    def test_unclaimed_not_needed_posts_with_a_durable_unique_identity(
        self, tmp_path, monkeypatch
    ) -> None:
        from complete_lifecycle_test_support import PublicationForge

        from orchestune.complete.contracts import CompleteRequest
        from orchestune.complete.service import complete_task
        from orchestune.ledger.run_state import load_run_state_readonly

        request = CompleteRequest.not_needed(
            1003, state_path=tmp_path / "run_state.json"
        )
        workspace = SimpleNamespace(
            run_state_path=request.state_path, repository_identity="repo"
        )
        monkeypatch.setattr(
            "orchestune.complete.service.resolve_claim_workspace", lambda **_: workspace
        )
        forge = PublicationForge()
        forge.labels = {"status:queued"}
        result = complete_task(request, forge=forge)
        assert result.success, result.failure
        assert result.completion_id is not None
        assert result.outcome_record is not None
        assert request.state_path is not None
        assert result.completion_id.startswith("completion-")
        assert result.completion_id != "unclaimed-not-needed-1003"
        state = load_run_state_readonly(request.state_path)
        assert not state.active_worktrees and state.completion_replay_receipts
        assert forge.comments[0]["body"] == result.outcome_record.render()
