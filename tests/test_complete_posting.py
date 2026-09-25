"""Issue-only, idempotent outcome posting contracts for #1003."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

        calls: list[tuple[tuple[str, ...], str | None]] = []

        def runner(args: list[str], input_text: str | None = None):
            calls.append((tuple(args), input_text))
            if "--method" not in args:
                return [[], []]
            return {"id": 88, "html_url": "https://example.test/comments/88"}

        result = post_issue_outcome(
            PostingRequest(issue_number=1003, outcome_record=_record()), runner=runner
        )

        assert result.comment_id == "88"
        assert result.comment_url == "https://example.test/comments/88"
        assert len(calls) == 2
        assert all(call[0][0] == "gh" for call in calls)
        assert "/issues/1003/comments" in " ".join(calls[0][0])
        assert "/issues/1003/comments" in " ".join(calls[1][0])
        assert calls[1][1] is not None

    def test_reuses_matching_outcome_on_a_later_page(self) -> None:
        from orchestune.complete.posting import PostingRequest, post_issue_outcome

        matching = {
            "id": 12,
            "html_url": "https://example.test/comments/12",
            "body": _record().render(),
            "created_at": "2026-09-25T00:00:00Z",
        }

        def runner(args: list[str], input_text: str | None = None):
            assert "--method" not in args
            return [[], [matching]]

        result = post_issue_outcome(
            PostingRequest(issue_number=1003, outcome_record=_record()), runner=runner
        )

        assert result.comment_id == "12"
        assert result.comment_url.endswith("/12")
        assert result.reused is True

    def test_response_loss_rechecks_before_a_second_post(self) -> None:
        from orchestune.complete.posting import PostingRequest, post_issue_outcome

        matching = {
            "id": 13,
            "html_url": "https://example.test/comments/13",
            "body": _record().render(),
            "created_at": "2026-09-25T00:00:00Z",
        }
        list_calls = 0
        post_calls = 0

        def runner(args: list[str], input_text: str | None = None):
            nonlocal list_calls, post_calls
            if "--method" not in args:
                list_calls += 1
                return [] if list_calls == 1 else [[matching]]
            post_calls += 1
            raise RuntimeError("connection dropped after POST")

        result = post_issue_outcome(
            PostingRequest(issue_number=1003, outcome_record=_record()), runner=runner
        )

        assert result.comment_id == "13"
        assert result.reused is True
        assert post_calls == 1
        assert list_calls == 2

    def test_unknown_lookup_never_posts_blindly(self) -> None:
        from orchestune.complete.posting import (
            OutcomeLookupUnknownError,
            PostingRequest,
            post_issue_outcome,
        )

        def unavailable(args: list[str], input_text: str | None = None):
            raise RuntimeError("GitHub unavailable")

        with pytest.raises(OutcomeLookupUnknownError):
            post_issue_outcome(PostingRequest(1003, _record()), runner=unavailable)


class TestCompletionService:
    def test_dry_run_has_no_ci_journal_or_post_side_effects(self, tmp_path) -> None:
        from orchestune.complete.contracts import CompleteRequest
        from orchestune.complete.preflight import CompletePreflight
        from orchestune.complete.service import complete_task

        request = CompleteRequest.not_needed(
            1003, dry_run=True, state_path=tmp_path / "run_state.json"
        )
        workspace = SimpleNamespace(run_state_path=tmp_path / "run_state.json")
        state = SimpleNamespace(active_worktrees={})
        with (
            patch(
                "orchestune.complete.service.resolve_claim_workspace",
                return_value=workspace,
            ),
            patch("orchestune.complete.service.load_run_state", return_value=state),
            patch(
                "orchestune.complete.service.evaluate_complete_preflight",
                return_value=CompletePreflight(accepted=True),
            ),
            patch("orchestune.complete.service.run_local_ci_if_needed") as run_ci,
            patch("orchestune.complete.service.reserve_completion") as reserve,
            patch("orchestune.complete.service.post_issue_outcome") as post,
        ):
            result = complete_task(request, forge=SimpleNamespace(_run=lambda *_: "[]"))

        assert result.success is True
        run_ci.assert_not_called()
        reserve.assert_not_called()
        post.assert_not_called()

    def test_rechecks_then_persists_comment_evidence_after_post(self, tmp_path) -> None:
        from orchestune.complete.contracts import CompleteRequest
        from orchestune.complete.journal import CompletionJournal
        from orchestune.complete.posting import PostingResult
        from orchestune.complete.preflight import CompletePreflight
        from orchestune.complete.service import complete_task

        state_path = tmp_path / "run_state.json"
        active = SimpleNamespace(base_ref="parent/issue-894")
        state = SimpleNamespace(active_worktrees={"1003": active})
        request = CompleteRequest.not_needed(
            1003,
            owner_token="token",
            claim_id="claim-1",
            state_path=state_path,
            worktree_root=Path.cwd(),
        )
        journal = CompletionJournal(
            issue_number=1003,
            claim_id="claim-1",
            completion_id="completion-1",
            result="not-needed",
            stage="journaling",
        )
        workspace = SimpleNamespace(run_state_path=state_path)
        with (
            patch(
                "orchestune.complete.service.resolve_claim_workspace",
                return_value=workspace,
            ),
            patch("orchestune.complete.service.load_run_state", return_value=state),
            patch(
                "orchestune.complete.service.evaluate_complete_preflight",
                return_value=CompletePreflight(accepted=True),
            ) as preflight,
            patch(
                "orchestune.complete.service.reserve_completion", return_value=journal
            ),
            patch(
                "orchestune.complete.service.post_issue_outcome",
                return_value=PostingResult(
                    "7", "https://example.test/comments/7", False
                ),
            ),
            patch("orchestune.complete.service.mark_handoff_ready") as handoff,
        ):
            result = complete_task(request, forge=SimpleNamespace(_run=lambda *_: "[]"))

        assert result.success is True
        assert preflight.call_count == 2
        assert result.outcome_record is not None
        assert result.outcome_record.completion_id == "completion-1"
        handoff.assert_called_once()
        assert handoff.call_args.kwargs["comment_url"].endswith("/7")
