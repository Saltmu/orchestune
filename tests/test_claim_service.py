"""Unit and integration tests for orchestune.claim.service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
from orchestune.infra.process_utils import FileLockContentionError, run_state_lock
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from tests.conftest import FakeForge


class MockForge(FakeForge):
    """Mock implementation of Forge interface for issue querying and labeling."""

    def __init__(self, issues: dict[int, IssueRecord] | None = None) -> None:
        super().__init__()
        self.issues = dict(issues or {})
        self.labels_added: list[tuple[int, str]] = []
        self.labels_removed: list[tuple[int, str]] = []
        self.fail_add_label: bool = False

    def get_issue(self, issue_number: int | str) -> IssueRecord | None:
        return self.issues.get(int(issue_number))

    def add_label(self, issue_number: int | str, label: str, actor: str = "") -> None:
        if self.fail_add_label:
            raise RuntimeError("GitHub API error: add_label failed")
        self.labels_added.append((int(issue_number), label))
        super().add_label(issue_number, label, actor=actor)

    def remove_label(self, issue_number: int | str, label: str) -> None:
        self.labels_removed.append((int(issue_number), label))
        super().remove_label(issue_number, label)


def _make_issue(
    number: int = 123,
    title: str = "[FEAT] example task",
    body: str = "",
    labels: tuple[str, ...] = (StatusLabel.QUEUED,),
    state: str = "OPEN",
) -> IssueRecord:
    footprint_body = (
        "## Footprint\n\n"
        "```yaml\n"
        "subtask_id: test-task\n"
        "footprint: [orchestune/foo.py]\n"
        "```\n"
    )
    return IssueRecord(
        number=number,
        title=title,
        body=body or footprint_body,
        labels=labels,
        created_at="2026-09-20T10:00:00Z",
        state=state,
    )


@pytest.fixture
def claim_env(tmp_path: Path):
    """Sets up a workspace directory with run_state.json and git structure."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    from orchestune.infra.git_cli import run_git

    run_git(["init", "-b", "main"], cwd=repo_root)
    run_git(["config", "user.name", "Test User"], cwd=repo_root)
    run_git(["config", "user.email", "test@example.com"], cwd=repo_root)
    (repo_root / "README.md").write_text("initial")
    run_git(["add", "README.md"], cwd=repo_root)
    run_git(["commit", "-m", "initial commit"], cwd=repo_root)

    worktrees_dir = repo_root / "worktrees"
    worktrees_dir.mkdir(exist_ok=True)

    state_path = repo_root / "run_state.json"
    with run_state_lock(state_path.with_suffix(".lock")):
        save_run_state(RunState(active_worktrees={}, launch_history=[]), state_path)

    return {
        "repo_root": repo_root,
        "state_path": state_path,
        "worktrees_dir": worktrees_dir,
    }


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


class TestAdditionalClaimServiceEdgeCases:
    """Verifies edge cases such as empty tokens, repository-wide conflicts, and stage resumption."""

    def test_resume_claim_from_active_saved_skips_worktree_creation(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=112)
        forge = MockForge({112: issue})
        token = new_owner_token()
        claim_id = new_claim_id()
        wt_path = claim_env["worktrees_dir"] / "claude-issue-112-test-task"
        wt_path.mkdir(parents=True, exist_ok=True)

        active = ActiveWorktree(
            issue_number=112,
            branch="claude/issue-112-test-task",
            worktree_path=str(wt_path),
            pid=None,
            started_at=None,
            declared_footprint=("orchestune/foo.py",),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.ACTIVE_SAVED.value,
            reservation_kind=ReservationKind.FOOTPRINT.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"112": active}), state_path)

        with patch("orchestune.claim.service.prepare_task_worktree") as mock_prep:
            outcome = resume_claim(
                claim_id=claim_id,
                owner_token=token.value,
                forge=forge,
                cwd=repo_root,
                state_path=state_path,
            )

        assert outcome.success is True
        assert outcome.stage == ClaimStage.COMPLETED
        mock_prep.assert_not_called()
        assert (112, StatusLabel.IN_PROGRESS) in forge.labels_added

    def test_claim_task_explicit_resume_via_request(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=113)
        forge = MockForge({113: issue})
        token = new_owner_token()
        claim_id = new_claim_id()
        wt_path = claim_env["worktrees_dir"] / "claude-issue-113-test-task"
        wt_path.mkdir(parents=True, exist_ok=True)

        active = ActiveWorktree(
            issue_number=113,
            branch="claude/issue-113-test-task",
            worktree_path=str(wt_path),
            pid=None,
            started_at=None,
            declared_footprint=("orchestune/foo.py",),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.ACTIVE_SAVED.value,
            reservation_kind=ReservationKind.FOOTPRINT.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"113": active}), state_path)

        request = ClaimRequest(
            issue_number=113,
            state_path=state_path,
            resume_claim_id=claim_id,
            owner_token=token.value,
        )
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.stage == ClaimStage.COMPLETED
        assert outcome.claim_id == claim_id

    def test_claim_task_repository_wide_reservation_conflict(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=114)
        forge = MockForge({114: issue})

        repo_wide_active = ActiveWorktree(
            issue_number=99,
            branch="claude/issue-99-task",
            worktree_path="/tmp/wt99",
            pid=None,
            started_at=None,
            declared_footprint=(),
            reservation_kind=ReservationKind.REPOSITORY.value,
            claim_id="claim-repo-99",
            claim_stage=ClaimStage.COMPLETED.value,
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(
                RunState(active_worktrees={"99": repo_wide_active}), state_path
            )

        request = ClaimRequest(issue_number=114, state_path=state_path)
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.CLAIM_CONFLICT
        assert outcome.failure.conflicting_issue_number == 99

    def test_resume_claim_empty_token_rejected(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]

        outcome = resume_claim(
            claim_id="claim-any",
            owner_token="   ",
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME

    def test_resume_claim_nonexistent_claim_id_rejected(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]

        outcome = resume_claim(
            claim_id="claim-nonexistent",
            owner_token="some-token",
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME

    def test_resume_claim_repository_identity_mismatch_rejected(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]

        token = new_owner_token()
        claim_id = new_claim_id()

        active = ActiveWorktree(
            issue_number=115,
            branch="claude/issue-115-test",
            worktree_path="",
            pid=None,
            started_at=None,
            declared_footprint=(),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.RESERVED.value,
            reservation_kind=ReservationKind.FOOTPRINT.value,
            owner_token_digest=owner_token_digest(token),
            repository_id="other-owner/other-repo",
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"115": active}), state_path)

        outcome = resume_claim(
            claim_id=claim_id,
            owner_token=token,
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.INVALID_RESUME
        assert "Repository identity mismatch" in outcome.failure.message
        assert outcome.owner_token == token.value

    def test_resume_claim_lock_contention_returns_state_lock_failed(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]

        with patch(
            "orchestune.claim.service.run_state_lock",
            side_effect=FileLockContentionError("lock is busy"),
        ):
            outcome = resume_claim(
                claim_id="claim-test",
                owner_token="valid-token",
                cwd=repo_root,
                state_path=state_path,
            )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.STATE_LOCK_FAILED
        assert "Could not acquire run_state lock" in outcome.failure.message

    def test_claim_task_worktree_save_state_failure_returns_recoverable_token(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=121)
        forge = MockForge({121: issue})
        worktree_path = claim_env["worktrees_dir"] / "claude-issue-121-task"

        def mock_prepare_worktree(*args, **kwargs):
            worktree_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch="claude/issue-121-task",
                accepted=True,
                created=True,
                base_sha="sha121",
            )

        call_count = 0
        original_save = save_run_state

        def failing_second_save(state, path):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise OSError("Disk full on active_saved persistence")
            original_save(state, path)

        with (
            patch(
                "orchestune.claim.service.run_git",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
            patch(
                "orchestune.claim.service.save_run_state",
                side_effect=failing_second_save,
            ),
        ):
            request = ClaimRequest(issue_number=121, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.STATE_SAVE_FAILED
        assert outcome.stage == ClaimStage.RESERVED
        assert outcome.owner_token is not None

    def test_resume_claim_rejected_when_issue_closed(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=122, state="closed")
        forge = MockForge({122: issue})

        token = new_owner_token()
        claim_id = new_claim_id()
        active = ActiveWorktree(
            issue_number=122,
            branch="claude/issue-122-task",
            worktree_path="/tmp/wt122",
            pid=None,
            started_at=None,
            declared_footprint=(),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.ACTIVE_SAVED.value,
            reservation_kind=ReservationKind.REPOSITORY.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"122": active}), state_path)

        outcome = resume_claim(
            claim_id=claim_id,
            owner_token=token,
            forge=forge,
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.ISSUE_CLOSED
        assert outcome.owner_token == token.value
        assert len(forge.labels_added) == 0

    def test_resume_claim_rejected_when_issue_has_terminal_escalation(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(
            number=123,
            state="open",
            labels=(StatusLabel.BLOCKED_HUMAN_REVIEW.value,),
        )
        forge = MockForge({123: issue})

        token = new_owner_token()
        claim_id = new_claim_id()
        active = ActiveWorktree(
            issue_number=123,
            branch="claude/issue-123-task",
            worktree_path="/tmp/wt123",
            pid=None,
            started_at=None,
            declared_footprint=(),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.ACTIVE_SAVED.value,
            reservation_kind=ReservationKind.REPOSITORY.value,
            owner_token_digest=owner_token_digest(token),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"123": active}), state_path)

        outcome = resume_claim(
            claim_id=claim_id,
            owner_token=token,
            forge=forge,
            cwd=repo_root,
            state_path=state_path,
        )

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.TERMINAL_ESCALATION
        assert outcome.owner_token == token.value
        assert len(forge.labels_added) == 0

    def test_claim_task_conflict_evaluation_fails_closed_on_metadata_lookup_exception(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        candidate_issue = _make_issue(number=124)

        class FailingForge(MockForge):
            def get_issue(self, issue_number: int | str) -> IssueRecord | None:
                if int(issue_number) == 99:
                    raise RuntimeError("Transient GitHub API 500 error")
                return super().get_issue(issue_number)

        forge = FailingForge({124: candidate_issue})

        active = ActiveWorktree(
            issue_number=99,
            branch="claude/issue-99-task",
            worktree_path="/tmp/wt99",
            pid=None,
            started_at=None,
            declared_footprint=(),
            owner_kind=OwnerKind.INTERACTIVE.value,
            claim_id="claim-active-99",
            claim_stage=ClaimStage.COMPLETED.value,
            reservation_kind=ReservationKind.FOOTPRINT.value,
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"99": active}), state_path)

        request = ClaimRequest(issue_number=124, state_path=state_path)
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.CLAIM_CONFLICT
        assert "metadata lookup failure" in outcome.failure.message
