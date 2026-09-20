"""Additional edge case tests for orchestune.claim.service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.claim.contracts import (
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
    save_run_state,
)
from orchestune.dispatch.worktree import WorktreePreparation
from orchestune.infra.process_utils import FileLockContentionError, run_state_lock
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord
from tests.claim_helpers import MockForge, _make_issue


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

    def test_claim_task_publishes_claim_ownership_to_issue_body(
        self, claim_env: dict[str, Path]
    ) -> None:
        from orchestune.dispatch.recovery import _parse_claim_info_from_issue

        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=125)
        forge = MockForge({125: issue})
        worktree_path = claim_env["worktrees_dir"] / "claude-issue-125-task"

        def mock_prepare_worktree(*args, **kwargs):
            worktree_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch="claude/issue-125-task",
                accepted=True,
                created=True,
                base_sha="sha125",
            )

        with (
            patch(
                "orchestune.claim.service.run_git",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=125, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.stage == ClaimStage.COMPLETED
        assert len(forge.bodies_updated) > 0

        updated_issue = forge.get_issue(125)
        assert updated_issue is not None
        owner_kind, claim_id, res_kind = _parse_claim_info_from_issue(updated_issue)
        assert owner_kind == "interactive"
        assert claim_id == outcome.claim_id
        assert res_kind in {"footprint", "repository"}

    def test_claim_task_publishes_metadata_fails_returns_state_save_failed(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=126)
        forge = MockForge({126: issue})
        forge.fail_update_body = True
        worktree_path = claim_env["worktrees_dir"] / "claude-issue-126-task"

        def mock_prepare_worktree(*args, **kwargs):
            worktree_path.mkdir(parents=True, exist_ok=True)
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch="claude/issue-126-task",
                accepted=True,
                created=True,
                base_sha="sha126",
            )

        with (
            patch(
                "orchestune.claim.service.run_git",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=126, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.STATE_SAVE_FAILED
        assert outcome.stage == ClaimStage.ACTIVE_SAVED
        assert outcome.owner_token is not None

    def test_claim_task_revalidates_issue_before_labeling_rejects_closed_issue(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=127, state="open")
        forge = MockForge({127: issue})
        worktree_path = claim_env["worktrees_dir"] / "claude-issue-127-task"

        def mock_prepare_worktree(*args, **kwargs):
            worktree_path.mkdir(parents=True, exist_ok=True)
            forge.issues[127] = _make_issue(number=127, state="closed")
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch="claude/issue-127-task",
                accepted=True,
                created=True,
                base_sha="sha127",
            )

        with (
            patch(
                "orchestune.claim.service.run_git",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=127, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.ISSUE_CLOSED
        assert outcome.stage == ClaimStage.ACTIVE_SAVED
        assert len(forge.labels_added) == 0

    def test_claim_task_runtime_error_inside_lock_is_not_swallowed_as_state_lock_failed(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=128)
        forge = MockForge({128: issue})

        with (
            patch(
                "orchestune.claim.service._execute_claim_in_lock",
                side_effect=RuntimeError("internal arbitrary runtime error"),
            ),
            pytest.raises(RuntimeError, match="internal arbitrary runtime error"),
        ):
            request = ClaimRequest(issue_number=128, state_path=state_path)
            claim_task(request, forge=forge, cwd=repo_root)

    def test_claim_task_resume_dry_run_does_not_execute_side_effects(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=129)
        forge = MockForge({129: issue})
        token = new_owner_token()
        claim_id = "claim-129-dryrun"

        active = ActiveWorktree(
            issue_number=129,
            branch="claude/issue-129-task",
            worktree_path=str(claim_env["worktrees_dir"] / "claude-issue-129-task"),
            pid=None,
            started_at=None,
            declared_footprint=(),
            reservation_kind=ReservationKind.FOOTPRINT.value,
            claim_id=claim_id,
            claim_stage=ClaimStage.ACTIVE_SAVED.value,
            owner_token_digest=owner_token_digest(token.value),
        )
        with run_state_lock(state_path.with_suffix(".lock")):
            save_run_state(RunState(active_worktrees={"129": active}), state_path)

        request = ClaimRequest(
            issue_number=129,
            state_path=state_path,
            resume_claim_id=claim_id,
            owner_token=token.value,
            dry_run=True,
        )
        outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is True
        assert outcome.claim_id == claim_id
        assert outcome.stage == ClaimStage.ACTIVE_SAVED
        assert len(forge.labels_added) == 0
        assert len(forge.bodies_updated) == 0

    def test_claim_task_revalidates_blocked_dependency_before_labeling(
        self, claim_env: dict[str, Path]
    ) -> None:
        repo_root = claim_env["repo_root"]
        state_path = claim_env["state_path"]
        issue = _make_issue(number=130, state="open", labels=(StatusLabel.QUEUED,))
        forge = MockForge({130: issue})
        worktree_path = claim_env["worktrees_dir"] / "claude-issue-130-task"

        def mock_prepare_worktree(*args, **kwargs):
            worktree_path.mkdir(parents=True, exist_ok=True)
            forge.issues[130] = _make_issue(
                number=130, state="open", labels=(StatusLabel.BLOCKED,)
            )
            return WorktreePreparation(
                worktree_path=worktree_path,
                branch="claude/issue-130-task",
                accepted=True,
                created=True,
                base_sha="sha130",
            )

        with (
            patch(
                "orchestune.claim.service.run_git",
                return_value=MagicMock(returncode=0),
            ),
            patch(
                "orchestune.claim.service.prepare_task_worktree",
                side_effect=mock_prepare_worktree,
            ),
        ):
            request = ClaimRequest(issue_number=130, state_path=state_path)
            outcome = claim_task(request, forge=forge, cwd=repo_root)

        assert outcome.success is False
        assert outcome.failure is not None
        assert outcome.failure.reason == ClaimFailureReason.UNRESOLVED_DEPENDENCIES
        assert outcome.stage == ClaimStage.ACTIVE_SAVED
        assert len(forge.labels_added) == 0
