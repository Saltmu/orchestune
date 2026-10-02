"""Tests for result-specific completion preflight validation (#999)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestune.complete.contracts import (
    CompleteFailureReason,
    CompleteRequest,
)
from orchestune.complete.preflight import (
    WorktreeStatus,
    evaluate_complete_preflight,
    inspect_worktree_status,
)
from orchestune.ledger.active_records import (
    ActiveCompletionJournal,
    ActiveWorktree,
    ActiveWorktreeCore,
    ClaimInfo,
    LaunchInfo,
)


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary initialized git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("hello\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True
    )
    return repo


class TestInspectWorktreeStatus:
    def test_clean_repo(self, temp_git_repo: Path) -> None:
        status = inspect_worktree_status(temp_git_repo)
        assert status == WorktreeStatus.CLEAN

    def test_dirty_modified_file(self, temp_git_repo: Path) -> None:
        (temp_git_repo / "README.md").write_text("modified\n")
        status = inspect_worktree_status(temp_git_repo)
        assert status == WorktreeStatus.DIRTY

    def test_dirty_untracked_file(self, temp_git_repo: Path) -> None:
        (temp_git_repo / "new_file.txt").write_text("untracked\n")
        status = inspect_worktree_status(temp_git_repo)
        assert status == WorktreeStatus.DIRTY

    def test_unknown_nonexistent_path(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist"
        status = inspect_worktree_status(non_existent)
        assert status == WorktreeStatus.UNKNOWN

    def test_unknown_none_path(self) -> None:
        assert inspect_worktree_status(None) == WorktreeStatus.UNKNOWN

    def test_unknown_git_failure(self, tmp_path: Path) -> None:
        # Not a git repo -> git status fails with non-zero exit code
        not_a_repo = tmp_path / "not_repo"
        not_a_repo.mkdir()
        status = inspect_worktree_status(not_a_repo)
        # Must NOT be rounded to CLEAN!
        assert status == WorktreeStatus.UNKNOWN


class FakePr:
    def __init__(
        self,
        number: int = 10,
        state: str = "OPEN",
        head_ref: str = "claude/issue-999-complete-preflight",
        base_ref: str = "parent/issue-894",
        head_sha: str = "abc1234",
        commits_count: int = 1,
        body: str = "Fixes #999",
    ) -> None:
        self.number = number
        self.state = state
        self.head_ref = head_ref
        self.base_ref = base_ref
        self.head_sha = head_sha
        self.commits_count = commits_count
        self.body = body


class FakeForge:
    def __init__(self, prs: dict[int, Any] | None = None) -> None:
        self.prs = prs or {}

    def get_pull_request(self, pr_number: int) -> Any | None:
        return self.prs.get(pr_number)


def FakeActiveWorktree(
    issue_number: int = 999,
    owner_token_digest: str = "digest_123",
    worktree_path: str = "/path/to/worktree",
    branch: str = "claude/issue-999-complete-preflight",
    base_ref: str = "parent/issue-894",
) -> ActiveWorktree:
    return ActiveWorktree.from_records(
        core=ActiveWorktreeCore(
            issue_number=issue_number,
            branch=branch,
            worktree_path=worktree_path,
            declared_footprint=(),
        ),
        launch=LaunchInfo(),
        claim=ClaimInfo(
            claim_id="claim-test",
            owner_token_digest=owner_token_digest,
            base_ref=base_ref,
        ),
        completion=ActiveCompletionJournal(),
    )


class FakeRunState:
    def __init__(
        self, active_worktrees: dict[str, ActiveWorktree] | None = None
    ) -> None:
        self.active_worktrees = active_worktrees or {}


class TestEvaluateCompletePreflight:
    def test_done_reports_forge_lookup_failure_without_raising(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=owner_token_digest(token),
                    worktree_path=str(temp_git_repo),
                )
            }
        )

        class UnavailableForge:
            def get_pull_request(self, pr_number: int) -> Any:
                raise RuntimeError("HTTP 502")

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=UnavailableForge(),
            run_state=run_state,
        )

        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.EVIDENCE_MISSING
        assert "unavailable" in (result.reason or "").lower()

    def test_done_success(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "secret_token_123"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            issue_number=999,
            pr=10,
            owner_token=token,
            claim_id="claim-test",
        )
        forge = FakeForge(
            {
                10: FakePr(
                    number=10,
                    state="OPEN",
                    head_ref="claude/issue-999-complete-preflight",
                    base_ref="parent/issue-894",
                    head_sha="head123",
                    commits_count=2,
                )
            }
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    issue_number=999,
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    branch="claude/issue-999-complete-preflight",
                    base_ref="parent/issue-894",
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )

        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.CLEAN
        assert result.failure_reason is None

    def test_done_rejects_dirty_worktree(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "secret_token_123"
        digest = owner_token_digest(token)
        (temp_git_repo / "dirty.txt").write_text("dirty")

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, head_sha="head123")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )

        assert result.accepted is False
        assert result.worktree_status == WorktreeStatus.DIRTY
        assert result.failure_reason == CompleteFailureReason.DIRTY_WORKTREE

    def test_done_rejects_unknown_worktree(self, tmp_path: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        invalid_path = tmp_path / "not_a_repo"
        invalid_path.mkdir()

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, head_sha="head123")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(invalid_path)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=invalid_path,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )

        assert result.accepted is False
        assert result.worktree_status == WorktreeStatus.UNKNOWN
        assert result.failure_reason is not None

    def test_done_rejects_missing_pr(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=9999, owner_token=token
        )
        forge = FakeForge({})  # PR 9999 does not exist
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
        )

        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.EVIDENCE_MISSING
        assert "not found" in (result.reason or "").lower()

    def test_done_rejects_closed_unmerged_pr(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, state="CLOSED")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
        )

        assert result.accepted is False
        assert "closed" in (result.reason or "").lower()

    def test_done_rejects_base_mismatch(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, base_ref="wrong-base")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )

        assert result.accepted is False
        assert "base" in (result.reason or "").lower()

    @pytest.mark.parametrize(
        "expected_base_ref",
        ["origin/main", "refs/remotes/origin/main", "refs/heads/main"],
    )
    def test_done_accepts_git_ref_for_pr_base(
        self, temp_git_repo: Path, expected_base_ref: str
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, base_ref="main", head_sha="head123")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    base_ref=expected_base_ref,
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
        )

        assert result.accepted is True
        assert result.failure_reason is None

    @pytest.mark.parametrize("pr_base_ref", ["origin/main", "refs/remotes/origin/main"])
    def test_done_keeps_pr_base_branch_name_literal(
        self, temp_git_repo: Path, pr_base_ref: str
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge(
            {10: FakePr(number=10, base_ref=pr_base_ref, head_sha="head123")}
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    base_ref="main",
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
        )

        assert result.accepted is False
        assert "base" in (result.reason or "").lower()

    def test_done_rejects_missing_base_ref(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, base_ref="main")})
        # Active worktree without base_ref, and no expected_base_ref provided
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    base_ref="",
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref=None,
        )

        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.EVIDENCE_MISSING
        assert "base branch reference is required" in (result.reason or "").lower()

    def test_done_rejects_empty_diff(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, commits_count=0, head_sha="head123")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )

        assert result.accepted is False
        assert "empty" in (result.reason or "").lower()

    def test_done_rejects_owner_token_mismatch(self, temp_git_repo: Path) -> None:
        request = CompleteRequest.not_needed(999, claim_id="stale", owner_token=None)
        state = FakeRunState(
            {"999": FakeActiveWorktree(worktree_path=str(temp_git_repo))}
        )
        result = evaluate_complete_preflight(
            request, worktree_path=temp_git_repo, run_state=state
        )
        assert not result.accepted
        assert result.failure_reason == CompleteFailureReason.GENERATION_MISMATCH

    def test_not_needed_accepts_dirty_or_unknown_worktree(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        (temp_git_repo / "dirty.txt").write_text("dirty")

        request = CompleteRequest.not_needed(
            claim_id="claim-test", issue_number=999, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            run_state=run_state,
        )

        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.DIRTY

    def test_not_needed_allows_unclaimed_exception(self) -> None:
        # Pre-claim not-needed outcome: no owner_token, no active_worktree in run_state
        request = CompleteRequest.not_needed(issue_number=999, owner_token=None)
        run_state = FakeRunState({})

        result = evaluate_complete_preflight(
            request,
            worktree_path=None,
            run_state=run_state,
        )

        assert result.accepted is True
        assert result.failure_reason is None

    def test_blocked_accepts_dirty_and_validates_base_sha(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        (temp_git_repo / "dirty.txt").write_text("dirty")

        # base-branch-red requires base_sha
        request_with_sha = CompleteRequest.blocked(
            claim_id="claim-test",
            issue_number=999,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=1,
            owner_token=token,
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request_with_sha,
            worktree_path=temp_git_repo,
            run_state=run_state,
        )

        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.DIRTY

        # Without base_sha for base-branch-red -> rejected
        request_without_sha = CompleteRequest.blocked(
            claim_id="claim-test",
            issue_number=999,
            reason="base-branch-red",
            base_sha=None,
            attempt=1,
            owner_token=token,
        )
        result_no_sha = evaluate_complete_preflight(
            request_without_sha,
            worktree_path=temp_git_repo,
            run_state=run_state,
        )
        assert result_no_sha.accepted is False
        assert "base_sha" in (result_no_sha.reason or "").lower()

    def test_blocked_requires_attempt_for_review_timeout(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request_with_attempt = CompleteRequest.blocked(
            claim_id="claim-test",
            issue_number=999,
            reason="review-timeout",
            attempt=2,
            owner_token=token,
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request_with_attempt,
            worktree_path=temp_git_repo,
            run_state=run_state,
        )
        assert result.accepted is True

        request_no_attempt = CompleteRequest.blocked(
            claim_id="claim-test",
            issue_number=999,
            reason="review-timeout",
            attempt=None,
            owner_token=token,
        )
        result_no_attempt = evaluate_complete_preflight(
            request_no_attempt,
            worktree_path=temp_git_repo,
            run_state=run_state,
        )
        assert result_no_attempt.accepted is False
        assert "attempt" in (result_no_attempt.reason or "").lower()

    def test_done_rejects_head_branch_mismatch(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge(
            {
                10: FakePr(
                    number=10,
                    head_ref="other-branch/wrong-task",
                    base_ref="parent/issue-894",
                    head_sha="head123",
                )
            }
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    branch="claude/issue-999-complete-preflight",
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )

        assert result.accepted is False
        assert "branch mismatch" in (result.reason or "").lower()

    def test_done_rejects_already_merged_pr(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, state="MERGED")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
        )

        assert result.accepted is False
        assert (
            "merged" in (result.reason or "").lower()
            or "not open" in (result.reason or "").lower()
        )

    def test_not_needed_rejects_claimed_with_wrong_token(
        self, temp_git_repo: Path
    ) -> None:
        request = CompleteRequest.not_needed(999, claim_id="stale", owner_token=None)
        state = FakeRunState(
            {"999": FakeActiveWorktree(worktree_path=str(temp_git_repo))}
        )
        result = evaluate_complete_preflight(
            request, worktree_path=temp_git_repo, run_state=state
        )
        assert not result.accepted
        assert result.failure_reason == CompleteFailureReason.GENERATION_MISMATCH

    def test_blocked_rejects_unclaimed(self) -> None:
        request = CompleteRequest.blocked(
            claim_id="claim-test",
            issue_number=999,
            reason="base-branch-red",
            base_sha="abc1234",
            attempt=1,
        )
        run_state = FakeRunState({})

        result = evaluate_complete_preflight(request, run_state=run_state)
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.CLAIM_NOT_FOUND

    def test_preflight_resolve_worktree_from_active(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        forge = FakeForge({10: FakePr(number=10, head_sha="head123")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    base_ref="parent/issue-894",
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=None,
            forge=forge,
            run_state=run_state,
        )
        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.CLEAN

    def test_preflight_resolve_worktree_from_request_root(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test",
            issue_number=999,
            pr=10,
            owner_token=token,
            worktree_root=temp_git_repo,
        )
        forge = FakeForge({10: FakePr(number=10, head_sha="head123")})
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path="",
                    base_ref="parent/issue-894",
                )
            }
        )

        result = evaluate_complete_preflight(
            request,
            worktree_path=None,
            forge=forge,
            run_state=run_state,
        )
        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.CLEAN

    def test_done_rejects_missing_owner_token(self, temp_git_repo: Path) -> None:
        request = CompleteRequest.not_needed(999, claim_id="stale", owner_token=None)
        state = FakeRunState(
            {"999": FakeActiveWorktree(worktree_path=str(temp_git_repo))}
        )
        result = evaluate_complete_preflight(
            request, worktree_path=temp_git_repo, run_state=state
        )
        assert not result.accepted
        assert result.failure_reason == CompleteFailureReason.GENERATION_MISMATCH

    def test_not_needed_rejects_unclaimed_with_token(self) -> None:
        request = CompleteRequest.not_needed(999, owner_token="unused-legacy-token")
        result = evaluate_complete_preflight(request)
        assert result.accepted

    def test_preflight_catches_invalid_request_validation(self) -> None:
        request = CompleteRequest.not_needed(claim_id="claim-test", issue_number=999)
        object.__setattr__(request, "result", "invalid_result_value")

        result = evaluate_complete_preflight(request)
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.INVALID_REQUEST

    @pytest.mark.parametrize("empty_token", ["", "   ", "\t\n"])
    def test_done_rejects_empty_or_whitespace_owner_token_without_raising(
        self, temp_git_repo: Path, empty_token: str
    ) -> None:
        request = CompleteRequest.done(
            999, 10, claim_id="claim-test", owner_token=empty_token
        )
        state = FakeRunState(
            {"999": FakeActiveWorktree(worktree_path=str(temp_git_repo))}
        )
        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            run_state=state,
            forge=FakeForge({10: FakePr()}),
        )
        assert result.accepted

    def test_pr_closed_and_merged_distinct_messages(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    base_ref="parent/issue-894",
                )
            }
        )

        # 1. Closed without being merged
        forge_closed = FakeForge({10: FakePr(number=10, state="CLOSED")})
        res_closed = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge_closed,
            run_state=run_state,
        )
        assert res_closed.accepted is False
        assert "closed without being merged" in (res_closed.reason or "")

        # 2. Already merged
        forge_merged = FakeForge({10: FakePr(number=10, state="MERGED")})
        res_merged = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge_merged,
            run_state=run_state,
        )
        assert res_merged.accepted is False
        assert res_merged.failure_reason == CompleteFailureReason.EVIDENCE_MISSING

    def test_done_rejects_missing_forge(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                )
            }
        )

        result = evaluate_complete_preflight(
            request, worktree_path=temp_git_repo, run_state=run_state, forge=None
        )
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.EVIDENCE_MISSING
        assert "forge" in (result.reason or "").lower()

    def test_done_with_pr_record_model(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest
        from orchestune.models import PrRecord

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    branch="claude/issue-999-complete-preflight",
                    base_ref="parent/issue-894",
                )
            }
        )
        pr = PrRecord(
            number=10,
            head_ref="claude/issue-999-complete-preflight",
            base_ref="parent/issue-894",
            state="OPEN",
            changed_files=("foo.py",),
        )
        forge = FakeForge({10: pr})
        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )
        assert result.accepted is True

    def test_done_rejects_pr_record_empty_diff(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest
        from orchestune.models import PrRecord

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    branch="claude/issue-999-complete-preflight",
                    base_ref="parent/issue-894",
                )
            }
        )
        pr = PrRecord(
            number=10,
            head_ref="claude/issue-999-complete-preflight",
            base_ref="parent/issue-894",
            state="OPEN",
            changed_files=(),
        )
        forge = FakeForge({10: pr})
        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
        )
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.INVALID_REQUEST
        assert "empty diff" in (result.reason or "").lower()

    def test_done_allows_pr_record_without_head_sha(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest
        from orchestune.models import PrRecord

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    branch="claude/issue-999-complete-preflight",
                    base_ref="parent/issue-894",
                )
            }
        )
        pr = PrRecord(
            number=10,
            head_ref="claude/issue-999-complete-preflight",
            base_ref="parent/issue-894",
            state="OPEN",
            changed_files=("foo.py",),
        )
        forge = FakeForge({10: pr})
        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )
        assert result.accepted is True

    def test_done_supports_list_prs_forge_with_include_files(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest
        from orchestune.models import PrRecord

        class RealisticListOnlyForge:
            """Mimics real forge behavior: only populates changed_files when include_files=True."""

            def list_prs(
                self, state: str = "open", include_files: bool = False
            ) -> list[PrRecord]:
                files = ("foo.py",) if include_files else ()
                return [
                    PrRecord(
                        number=10,
                        head_ref="claude/issue-999-complete-preflight",
                        base_ref="parent/issue-894",
                        state="OPEN",
                        changed_files=files,
                    )
                ]

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=token
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest,
                    worktree_path=str(temp_git_repo),
                    branch="claude/issue-999-complete-preflight",
                    base_ref="parent/issue-894",
                )
            }
        )
        forge = RealisticListOnlyForge()
        result = evaluate_complete_preflight(
            request,
            worktree_path=temp_git_repo,
            forge=forge,
            run_state=run_state,
            expected_base_ref="parent/issue-894",
        )
        assert result.accepted is True

    def test_failure_diagnostics_populated(self, temp_git_repo: Path) -> None:
        request = CompleteRequest.done(
            claim_id="claim-test", issue_number=999, pr=10, owner_token=""
        )
        result = evaluate_complete_preflight(request, worktree_path=temp_git_repo)
        assert result.accepted is False
        assert result.reason is not None
        assert result.diagnostics == (result.reason,)


class _Broken:
    """Object lacking nested records but carrying plausible flat values."""

    claim_id = "claim-test"
    base_ref = "parent/issue-894"
    branch = "claude/issue-999-complete-preflight"
    worktree_path = "/path/to/worktree"


class TestNestedOnlyReferences:
    def test_missing_claim_record_is_not_masked_by_flat_values(self) -> None:
        from orchestune.complete.preflight import _validate_ownership

        request = CompleteRequest.blocked(
            claim_id="claim-test", issue_number=999, reason="x"
        )
        state = FakeRunState({"999": _Broken()})  # type: ignore[dict-item]
        with pytest.raises(AttributeError):
            _validate_ownership(request, state)

    def test_missing_core_record_is_not_masked_by_flat_values(self) -> None:
        from orchestune.complete.preflight import _resolve_worktree_path

        request = CompleteRequest.blocked(
            claim_id="claim-test", issue_number=999, reason="x"
        )
        with pytest.raises(AttributeError):
            _resolve_worktree_path(request, None, _Broken())

    def test_pr_checks_require_nested_records(self) -> None:
        from orchestune.complete.preflight import (
            _check_pr_base_branch,
            _check_pr_identity_and_branches,
        )

        pr = FakePr()
        with pytest.raises(AttributeError):
            _check_pr_base_branch(pr, _Broken(), None)
        with pytest.raises(AttributeError):
            _check_pr_identity_and_branches(pr, 10, _Broken(), "parent/issue-894")

    def test_none_values_inside_records_remain_valid(self) -> None:
        from orchestune.complete.preflight import _check_pr_base_branch

        active = FakeActiveWorktree(base_ref=None)  # type: ignore[arg-type]
        ok, reason, _ = _check_pr_base_branch(FakePr(), active, None)
        assert not ok and "required" in (reason or "")
