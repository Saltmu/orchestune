"""Unit and integration tests for orchestune.claim.service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestune.claim.contracts import (
    ClaimExitCode,
    ClaimFailureReason,
    ClaimRequest,
    ClaimStage,
    OwnerKind,
    ReservationKind,
)
from orchestune.claim.ownership import new_claim_id, new_owner_token, owner_token_digest
from orchestune.claim.service import claim_task, resume_claim
from orchestune.dispatch.state import (
    ActiveWorktree,
    RunState,
    load_run_state,
    save_run_state,
)
from orchestune.dispatch.worktree import WorktreePreparation
from orchestune.infra.process_utils import run_state_lock
from orchestune.labels import StatusLabel
from tests.claim_helpers import MockForge, _make_issue


class TestClaimLifecycleOrderAndSuccess:
    """Verifies §5 side-effect progression and output invariants."""

    def test_claim_task_full_lifecycle_success(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=101)
        forge = MockForge({101: issue})

        call_order: list[str] = []

        def mock_run_git(args, **kwargs):
            if "fetch" in args:
                call_order.append("git_fetch")
            return MagicMock(returncode=0, stdout="abc1234")

        def mock_prepare_worktree(
            branch, worktree_root, base_branch, claim_id, **kwargs
        ):
            call_order.append("prepare_worktree")
            target_path = Path(worktree_root) / branch.replace("/", "-")
            target_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=target_path,
                branch=branch,
                accepted=True,
                created=True,
                base_sha="base_sha_123",
            )

        original_save = save_run_state

        def tracked_save(state, path, **kwargs):
            active = state.active_worktrees.get("101")
            if active is not None:
                call_order.append(f"save_state:{active.claim_stage}")
            return original_save(state, path, **kwargs)

        with (
            patch("orchestune.claim.service.run_git", side_effect=mock_run_git),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
            patch("orchestune.claim.service.save_run_state", side_effect=tracked_save),
        ):
            request = ClaimRequest(issue_number=101, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.issue_number == 101
        assert outcome.claim_id is not None
        assert outcome.stage == ClaimStage.COMPLETED
        assert outcome.branch == "claude/issue-101-test-task"
        assert outcome.worktree_path is not None
        assert outcome.owner_token is not None

        # Verify side-effect order: fetch -> save:reserved -> prepare_worktree -> save:active_saved -> save:completed
        assert call_order == [
            "git_fetch",
            "save_state:reserved",
            "prepare_worktree",
            "save_state:active_saved",
            "save_state:completed",
        ]

        # Verify label updated
        assert (101, StatusLabel.IN_PROGRESS) in forge.labels_added
        assert (101, StatusLabel.QUEUED) in forge.labels_removed

        # Verify persisted state
        persisted = load_run_state(state_path)
        assert "101" in persisted.active_worktrees
        active = persisted.active_worktrees["101"]
        assert active.claim_id == outcome.claim_id
        assert active.claim_stage == ClaimStage.COMPLETED.value
        assert active.owner_token_digest == owner_token_digest(outcome.owner_token)

    def test_claim_task_dry_run_no_side_effects(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=102)
        forge = MockForge({102: issue})

        with (
            patch("orchestune.claim.service.run_git") as mock_git,
            patch("orchestune.claim.service.prepare_task_worktree") as mock_prep,
            patch("orchestune.claim.service.save_run_state") as mock_save,
        ):
            request = ClaimRequest(
                issue_number=102, state_path=state_path, dry_run=True
            )
            outcome = claim_task(request, apply=True, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.issue_number == 102
        assert outcome.stage == ClaimStage.VALIDATING
        mock_git.assert_not_called()
        mock_prep.assert_not_called()
        mock_save.assert_not_called()
        assert len(forge.labels_added) == 0
        assert len(forge.labels_removed) == 0

        # State file remains unmodified
        persisted = load_run_state(state_path)
        assert "102" not in persisted.active_worktrees


class TestClaimPreflightAndConflictRejections:
    """Verifies preflight and conflict failure behaviors."""

    def test_issue_not_found(self, claim_env: dict[str, Path]) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        forge = MockForge({})

        request = ClaimRequest(issue_number=999, state_path=state_path)
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.ISSUE_NOT_FOUND
        assert outcome.failure.exit_code == ClaimExitCode.ISSUE_NOT_FOUND

    def test_issue_already_in_progress(self, claim_env: dict[str, Path]) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=103, labels=(StatusLabel.IN_PROGRESS,))
        forge = MockForge({103: issue})

        request = ClaimRequest(issue_number=103, state_path=state_path)
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.ALREADY_IN_PROGRESS

    def test_conflict_with_existing_active_reservation(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=104)
        forge = MockForge({104: issue})

        # Inject existing active worktree for issue 104
        existing = ActiveWorktree(
            issue_number=104,
            branch="claude/issue-104-task",
            worktree_path="/tmp/worktree",
            pid=None,
            started_at=None,
            declared_footprint=("orchestune/foo.py",),
            claim_id="claim-existing-104",
            claim_stage=ClaimStage.COMPLETED.value,
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"104": existing}), state_path)

        request = ClaimRequest(issue_number=104, state_path=state_path)
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason in (
            ClaimFailureReason.CLAIM_CONFLICT,
            ClaimFailureReason.EXISTING_CLAIM_UNRECOVERED,
        )


class TestInterruptionAndPreservation:
    """Verifies that failures retain reservations and worktrees without requeuing."""

    def test_fetch_failure_causes_no_state_change(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=105)
        forge = MockForge({105: issue})

        def mock_run_git(args, **kwargs):
            if "fetch" in args:
                raise RuntimeError("Network failure during fetch")
            return MagicMock(returncode=0)

        with patch("orchestune.claim.service.run_git", side_effect=mock_run_git):
            request = ClaimRequest(issue_number=105, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.GIT_FETCH_FAILED

        # Verify nothing was written to run_state or labels
        persisted = load_run_state(state_path)
        assert "105" not in persisted.active_worktrees
        assert len(forge.labels_added) == 0

    def test_worktree_failure_preserves_reservation(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=106)
        forge = MockForge({106: issue})

        def mock_prepare_worktree(*args, **kwargs):
            return WorktreePreparation(
                worktree_path=Path("/tmp/invalid"),
                branch="claude/issue-106-test-task",
                accepted=False,
                rejection_reason="unclaimed_existing_worktree",
            )

        with (
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=106, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.WORKTREE_CREATION_FAILED
        assert outcome.stage == ClaimStage.RESERVED
        assert outcome.owner_token is not None

        # Acceptance Criteria: 予約は保持され、queued に戻されない
        persisted = load_run_state(state_path)
        assert "106" in persisted.active_worktrees
        assert (
            persisted.active_worktrees["106"].claim_stage == ClaimStage.RESERVED.value
        )
        assert len(forge.labels_added) == 0
        assert len(forge.labels_removed) == 0

    def test_worktree_exception_converted_to_claim_failure(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=120)
        forge = MockForge({120: issue})

        with (
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=RuntimeError("disk full or git fatal"),
            ),
        ):
            request = ClaimRequest(issue_number=120, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.WORKTREE_CREATION_FAILED
        assert outcome.stage == ClaimStage.RESERVED
        assert outcome.owner_token is not None

    def test_invalid_branch_name_is_distinguished_from_infra_failure(
        self, claim_env: dict[str, Path]
    ) -> None:
        """#943レビュー対応(Codex P2): 不正なbranch/subtask_id名（恒久的な
        入力不備、人手の修正が必要）は、OSError/gitエラーのような一時的な
        インフラ障害（`WORKTREE_CREATION_FAILED`のまま）とは異なる理由
        （`INVALID_BRANCH_NAME`）で報告され、呼び出し側が
        `status:blocked-human-review`と再試行可能な`status:blocked`とを
        正しく区別できるようにする。"""
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=121)
        forge = MockForge({121: issue})

        with (
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=ValueError("branch name is invalid"),
            ),
        ):
            request = ClaimRequest(issue_number=121, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.INVALID_BRANCH_NAME
        assert outcome.stage == ClaimStage.RESERVED

    def test_label_update_failure_preserves_reservation_and_worktree(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=107)
        forge = MockForge({107: issue})
        forge.fail_add_label = True  # Simulate GitHub API failure

        worktree_path = claim_env["worktrees_dir"] / "claude-issue-107-test-task"

        def mock_prepare_worktree(
            branch, worktree_root, base_branch, claim_id, **kwargs
        ):
            worktree_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch=branch,
                accepted=True,
                created=True,
                base_sha="sha107",
            )

        with (
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=107, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.LABEL_UPDATE_FAILED
        assert outcome.stage == ClaimStage.ACTIVE_SAVED
        assert outcome.owner_token is not None

        # Acceptance Criteria: 予約と worktree は保持され、queued に戻されない
        persisted = load_run_state(state_path)
        assert "107" in persisted.active_worktrees
        active = persisted.active_worktrees["107"]
        assert active.claim_stage == ClaimStage.ACTIVE_SAVED.value
        assert active.worktree_path == str(worktree_path)
        assert worktree_path.exists()


class TestResumeClaim:
    """Verifies resume_claim idempotency and authorization."""

    def test_resume_claim_from_reserved_stage(self, claim_env: dict[str, Path]) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=108)
        forge = MockForge({108: issue})

        token = new_owner_token()
        claim_id = new_claim_id()

        reserved_active = ActiveWorktree(
            issue_number=108,
            branch="claude/issue-108-test-task",
            worktree_path="",
            pid=None,
            started_at=None,
            declared_footprint=("orchestune/foo.py",),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.RESERVED.value,
            reservation_kind=ReservationKind.FOOTPRINT.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(
                RunState(active_worktrees={"108": reserved_active}), state_path
            )

        worktree_path = claim_env["worktrees_dir"] / "claude-issue-108-test-task"

        def mock_prepare_worktree(
            branch, worktree_root, base_branch, claim_id, **kwargs
        ):
            worktree_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch=branch,
                accepted=True,
                created=True,
                base_sha="sha108",
            )

        with (
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            outcome = resume_claim(
                claim_id=claim_id,
                owner_token=token.value,
                forge=forge,
                cwd=repo_root,
                state_path=state_path,
            )

        assert outcome.success is True
        assert outcome.claim_id == claim_id
        assert outcome.stage == ClaimStage.COMPLETED
        assert (108, StatusLabel.IN_PROGRESS) in forge.labels_added

        persisted = load_run_state(state_path)
        assert (
            persisted.active_worktrees["108"].claim_stage == ClaimStage.COMPLETED.value
        )

    def test_resume_claim_token_mismatch_rejected(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        token = new_owner_token()
        claim_id = new_claim_id()

        reserved_active = ActiveWorktree(
            issue_number=109,
            branch="claude/issue-109-test-task",
            worktree_path="",
            pid=None,
            started_at=None,
            declared_footprint=(),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.RESERVED.value,
            reservation_kind=ReservationKind.REPOSITORY.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(
                RunState(active_worktrees={"109": reserved_active}), state_path
            )

        outcome = resume_claim(
            claim_id=claim_id,
            owner_token="wrong-token-value",
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME
        assert outcome.failure.exit_code == ClaimExitCode.INVALID_RESUME

    def test_resume_claim_already_completed_is_idempotent(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        token = new_owner_token()
        claim_id = new_claim_id()
        worktree_path = claim_env["worktrees_dir"] / "claude-issue-110-test-task"

        completed_active = ActiveWorktree(
            issue_number=110,
            branch="claude/issue-110-test-task",
            worktree_path=str(worktree_path),
            pid=None,
            started_at=None,
            declared_footprint=(),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.COMPLETED.value,
            reservation_kind=ReservationKind.REPOSITORY.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(
                RunState(active_worktrees={"110": completed_active}), state_path
            )

        outcome = resume_claim(
            claim_id=claim_id,
            owner_token=token.value,
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is True
        assert outcome.stage == ClaimStage.COMPLETED
        assert outcome.claim_id == claim_id


class TestLockReentrancy:
    """Verifies dispatch internal call does not deadlock under existing run_state_lock."""

    def test_claim_task_under_held_lock(self, claim_env: dict[str, Path]) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        lock_path = state_path.with_suffix(".lock")
        issue = _make_issue(number=111)
        forge = MockForge({111: issue})

        with (
            run_state_lock(lock_path),
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                return_value=WorktreePreparation(
                    worktree_path=Path("/tmp/wt111"),
                    branch="claude/issue-111-test-task",
                    accepted=True,
                    created=True,
                    base_sha="sha111",
                ),
            ),
        ):
            request = ClaimRequest(
                issue_number=111,
                state_path=state_path,
                owner_kind=OwnerKind.DISPATCH,
            )
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.owner_kind == OwnerKind.DISPATCH

    def test_claim_task_resolves_parent_issue_base_without_view(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        footprint_with_parent = (
            "## Footprint\n\n"
            "```yaml\n"
            "subtask_id: test-task\n"
            "footprint: [orchestune/foo.py]\n"
            "parent_issue_number: 894\n"
            "```\n"
        )
        issue = _make_issue(number=101, body=footprint_with_parent)
        forge = MockForge({101: issue})

        captured_base_branch: list[str | None] = []

        def mock_prepare_worktree(
            branch, worktree_root, base_branch, claim_id, **kwargs
        ):
            captured_base_branch.append(base_branch)
            target_path = Path(worktree_root) / branch.replace("/", "-")
            target_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=target_path,
                branch=branch,
                accepted=True,
                created=True,
                base_sha="base_sha_894",
            )

        with (
            patch(
                "orchestune.claim.service.run_git", return_value=MagicMock(returncode=0)
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=101, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.base_ref == "parent/issue-894"
        assert captured_base_branch == ["parent/issue-894"]

        saved_state = load_run_state(state_path)
        active = saved_state.active_worktrees.get("101")
        assert active is not None
        assert active.base_ref == "parent/issue-894"
        assert active.base_branch == "parent/issue-894"
