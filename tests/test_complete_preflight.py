"""Tests for result-specific completion preflight validation (#999)."""

from __future__ import annotations

import subprocess
from pathlib import Path

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
    def __init__(self, prs: dict[int, FakePr] | None = None) -> None:
        self.prs = prs or {}

    def get_pull_request(self, pr_number: int) -> FakePr | None:
        return self.prs.get(pr_number)


class FakeActiveWorktree:
    def __init__(
        self,
        issue_number: int = 999,
        owner_token_digest: str = "digest_123",
        worktree_path: str = "/path/to/worktree",
        branch: str = "claude/issue-999-complete-preflight",
        base_ref: str = "parent/issue-894",
    ) -> None:
        self.issue_number = issue_number
        self.owner_token_digest = owner_token_digest
        self.worktree_path = worktree_path
        self.branch = branch
        self.base_ref = base_ref


class FakeRunState:
    def __init__(
        self, active_worktrees: dict[str, FakeActiveWorktree] | None = None
    ) -> None:
        self.active_worktrees = active_worktrees or {}


class TestEvaluateCompletePreflight:
    def test_done_success(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "secret_token_123"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(
            issue_number=999,
            pr=10,
            owner_token=token,
            claim_id="claim-123",
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
            current_head_sha="head123",
        )

        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.CLEAN
        assert result.failure_reason is None

    def test_done_rejects_dirty_worktree(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "secret_token_123"
        digest = owner_token_digest(token)
        (temp_git_repo / "dirty.txt").write_text("dirty")

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
            current_head_sha="head123",
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

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
            current_head_sha="head123",
        )

        assert result.accepted is False
        assert result.worktree_status == WorktreeStatus.UNKNOWN
        assert result.failure_reason is not None

    def test_done_rejects_missing_pr(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(issue_number=999, pr=9999, owner_token=token)
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

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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

    def test_done_rejects_unpushed_head(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
        forge = FakeForge({10: FakePr(number=10, head_sha="remote_sha_old")})
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
            current_head_sha="local_sha_new",
        )

        assert result.accepted is False
        assert "pushed" in (result.reason or "").lower()

    def test_done_rejects_empty_diff(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
            current_head_sha="head123",
        )

        assert result.accepted is False
        assert "empty" in (result.reason or "").lower()

    def test_done_rejects_owner_token_mismatch(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        digest = owner_token_digest("correct_token")

        request = CompleteRequest.done(
            issue_number=999, pr=10, owner_token="wrong_token"
        )
        forge = FakeForge({10: FakePr(number=10)})
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
        assert result.failure_reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH

    def test_not_needed_accepts_dirty_or_unknown_worktree(
        self, temp_git_repo: Path
    ) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        (temp_git_repo / "dirty.txt").write_text("dirty")

        request = CompleteRequest.not_needed(issue_number=999, owner_token=token)
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

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
            current_head_sha="head123",
        )

        assert result.accepted is False
        assert "branch mismatch" in (result.reason or "").lower()

    def test_done_rejects_already_merged_pr(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)

        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
        from orchestune.claim.ownership import owner_token_digest

        digest = owner_token_digest("token_a")
        request = CompleteRequest.not_needed(
            issue_number=999, owner_token="wrong_token"
        )
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest=digest, worktree_path=str(temp_git_repo)
                )
            }
        )

        result = evaluate_complete_preflight(request, run_state=run_state)
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH

    def test_blocked_rejects_unclaimed(self) -> None:
        request = CompleteRequest.blocked(
            issue_number=999, reason="base-branch-red", base_sha="abc1234", attempt=1
        )
        run_state = FakeRunState({})

        result = evaluate_complete_preflight(request, run_state=run_state)
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.CLAIM_NOT_FOUND

    def test_preflight_resolve_worktree_from_active(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
            current_head_sha="head123",
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
            issue_number=999, pr=10, owner_token=token, worktree_root=temp_git_repo
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
            current_head_sha="head123",
        )
        assert result.accepted is True
        assert result.worktree_status == WorktreeStatus.CLEAN

    def test_done_rejects_missing_owner_token(self, temp_git_repo: Path) -> None:
        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=None)
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest="some_digest",
                    worktree_path=str(temp_git_repo),
                )
            }
        )

        result = evaluate_complete_preflight(
            request, worktree_path=temp_git_repo, run_state=run_state
        )
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH

    def test_not_needed_rejects_unclaimed_with_token(self) -> None:
        request = CompleteRequest.not_needed(issue_number=999, owner_token="some_token")
        run_state = FakeRunState({})

        result = evaluate_complete_preflight(request, run_state=run_state)
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.CLAIM_NOT_FOUND

    def test_preflight_catches_invalid_request_validation(self) -> None:
        request = CompleteRequest.not_needed(issue_number=999)
        object.__setattr__(request, "result", "invalid_result_value")

        result = evaluate_complete_preflight(request)
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.INVALID_REQUEST

    @pytest.mark.parametrize("empty_token", ["", "   ", "\t\n"])
    def test_done_rejects_empty_or_whitespace_owner_token_without_raising(
        self, temp_git_repo: Path, empty_token: str
    ) -> None:
        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=empty_token)
        run_state = FakeRunState(
            {
                "999": FakeActiveWorktree(
                    owner_token_digest="some_digest",
                    worktree_path=str(temp_git_repo),
                )
            }
        )

        result = evaluate_complete_preflight(
            request, worktree_path=temp_git_repo, run_state=run_state
        )
        assert result.accepted is False
        assert result.failure_reason == CompleteFailureReason.OWNER_TOKEN_MISMATCH
        assert "owner token is required" in (result.reason or "").lower()

    def test_pr_closed_and_merged_distinct_messages(self, temp_git_repo: Path) -> None:
        from orchestune.claim.ownership import owner_token_digest

        token = "token"
        digest = owner_token_digest(token)
        request = CompleteRequest.done(issue_number=999, pr=10, owner_token=token)
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
        assert "already merged" in (res_merged.reason or "")
