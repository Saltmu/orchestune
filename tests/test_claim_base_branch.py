"""Tests for orchestune.claim.base_branch ensuring parent/issue-<N> exists."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.claim import base_branch
from orchestune.claim.base_branch import ensure_claim_base_ref
from orchestune.claim.contracts import ClaimFailureReason
from orchestune.infra.git_cli import GitResult
from orchestune.models import IssueRecord
from tests.claim_helpers import MockForge


def _make_issue(
    number: int,
    *,
    title: str = "[EPIC] Parent Epic",
    body: str = "<!-- orchestune:decomposition-plan-parent -->\nParent plan",
) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=title,
        body=body,
        state="OPEN",
        labels=(),
        created_at="2026-01-01T00:00:00Z",
    )


class TestEnsureClaimBaseRef:
    def test_non_parent_base_ref_is_ignored(self) -> None:
        forge = MockForge()
        mock_runner = MagicMock()
        result = ensure_claim_base_ref(
            "origin/main",
            forge=forge,
            repository_root="/fake/repo",
            git_runner=mock_runner,
        )
        assert result is None
        mock_runner.assert_not_called()

    def test_branch_already_exists_does_not_call_ensure_parent_branch(self) -> None:
        forge = MockForge()

        def fake_git_runner(cmd: list[str], **kwargs: object) -> GitResult:
            # show-ref succeeds
            return GitResult(returncode=0, stdout="dummy_sha", stderr="")

        with patch.object(base_branch, "ensure_parent_branch") as mock_ensure:
            result = ensure_claim_base_ref(
                "parent/issue-100",
                forge=forge,
                repository_root="/fake/repo",
                git_runner=fake_git_runner,
            )
            assert result is None
            mock_ensure.assert_not_called()

    def test_parent_issue_not_found_fails_with_base_resolution_failed(self) -> None:
        forge = MockForge({})  # Issue 100 absent

        def fake_git_runner(cmd: list[str], **kwargs: object) -> GitResult:
            return GitResult(returncode=1, stdout="", stderr="")

        with patch.object(base_branch, "ensure_parent_branch") as mock_ensure:
            result = ensure_claim_base_ref(
                "parent/issue-100",
                forge=forge,
                repository_root="/fake/repo",
                git_runner=fake_git_runner,
            )
            assert result is not None
            assert result.reason == ClaimFailureReason.BASE_RESOLUTION_FAILED
            assert "Parent issue #100" in result.message
            assert len(result.next_actions) > 0
            mock_ensure.assert_not_called()

    def test_parent_issue_not_epic_fails_with_base_resolution_failed(self) -> None:
        non_epic = _make_issue(
            100, title="Ordinary task", body="No decomposition marker"
        )
        forge = MockForge({100: non_epic})

        def fake_git_runner(cmd: list[str], **kwargs: object) -> GitResult:
            return GitResult(returncode=1, stdout="", stderr="")

        with patch.object(base_branch, "ensure_parent_branch") as mock_ensure:
            result = ensure_claim_base_ref(
                "parent/issue-100",
                forge=forge,
                repository_root="/fake/repo",
                git_runner=fake_git_runner,
            )
            assert result is not None
            assert result.reason == ClaimFailureReason.BASE_RESOLUTION_FAILED
            mock_ensure.assert_not_called()

    def test_parent_epic_provisions_branch_successfully(self) -> None:
        epic = _make_issue(100)
        forge = MockForge({100: epic})

        state = {"created": False}

        def fake_git_runner(cmd: list[str], **kwargs: object) -> GitResult:
            if state["created"]:
                return GitResult(returncode=0, stdout="dummy_sha", stderr="")
            return GitResult(returncode=1, stdout="", stderr="")

        def fake_ensure(parent_issue_number: int, *, cwd: object = None) -> None:
            assert parent_issue_number == 100
            assert cwd == Path("/fake/repo")
            state["created"] = True

        with patch.object(
            base_branch, "ensure_parent_branch", side_effect=fake_ensure
        ) as mock_ensure:
            result = ensure_claim_base_ref(
                "parent/issue-100",
                forge=forge,
                repository_root=Path("/fake/repo"),
                git_runner=fake_git_runner,
            )
            assert result is None
            mock_ensure.assert_called_once()

    def test_parent_epic_provision_fails_if_branch_still_absent(self) -> None:
        epic = _make_issue(100)
        forge = MockForge({100: epic})

        def fake_git_runner(cmd: list[str], **kwargs: object) -> GitResult:
            # show-ref always fails even after ensure_parent_branch
            return GitResult(returncode=1, stdout="", stderr="")

        with patch.object(base_branch, "ensure_parent_branch") as mock_ensure:
            result = ensure_claim_base_ref(
                "parent/issue-100",
                forge=forge,
                repository_root="/fake/repo",
                git_runner=fake_git_runner,
            )
            assert result is not None
            assert result.reason == ClaimFailureReason.BASE_RESOLUTION_FAILED
            assert "Failed to create or verify base branch" in result.message
            mock_ensure.assert_called_once()
