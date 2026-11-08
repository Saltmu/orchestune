"""Claim-start evidence survives retries without guessing a new baseline."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.claim import service
from orchestune.claim.contracts import ClaimRequest, ClaimStage
from orchestune.claim.read_checkpoint import (
    checkpoint_marker,
    ensure_read_checkpoint,
    restore_read_checkpoint,
)
from orchestune.claim.service import claim_task, resume_claim
from orchestune.forge import ForgeError
from orchestune.infra.git_cli import run_git
from orchestune.labels import StatusLabel
from orchestune.ledger.run_state import load_run_state
from orchestune.read_dependencies.contracts import (
    CheckpointAvailability,
    ReadCheckpointV1,
)
from tests.claim_helpers import MockForge, _make_issue

REPOSITORY = "owner/repo"
ISSUE = 1770
BASE = "a" * 40
LATER = "b" * 40


def _checkpoint(claim_id: str = "claim-one") -> ReadCheckpointV1:
    return ReadCheckpointV1(
        repository=REPOSITORY,
        issue_number=ISSUE,
        claim_id=claim_id,
        input_base_sha=BASE,
        availability=CheckpointAvailability.RECORDED,
    )


def _restore(*bodies: str, claim_id: str = "claim-one") -> ReadCheckpointV1:
    return restore_read_checkpoint(
        [{"body": body} for body in bodies], REPOSITORY, ISSUE, claim_id
    )


class CheckpointForge(MockForge):
    def get_repository_slug(self) -> str:
        return REPOSITORY


def test_marker_roundtrip_is_non_secret_and_generation_bound() -> None:
    marker = checkpoint_marker(_checkpoint())
    assert _restore(marker) == _checkpoint()
    assert _restore(marker, marker) == _checkpoint()
    assert (
        _restore(marker, claim_id="claim-two").availability
        == CheckpointAvailability.UNAVAILABLE
    )
    assert "token" not in marker
    assert "path" not in marker


def test_conflicting_duplicates_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        _restore(
            checkpoint_marker(_checkpoint()),
            checkpoint_marker(dataclasses.replace(_checkpoint(), input_base_sha=LATER)),
        )


@pytest.mark.parametrize(
    "replacement",
    [
        {"repository": "other/repo"},
        {"issue_number": ISSUE + 1},
        {"version": 2},
        {"version": True},
        {"input_base_sha": "bad"},
        {"availability": "invalid"},
        {"owner_token": "secret"},
    ],
)
def test_invalid_marker_is_rejected(replacement: dict[str, object]) -> None:
    payload = dataclasses.asdict(_checkpoint()) | replacement
    marker = "```orchestune-read-checkpoint\n" + json.dumps(payload) + "\n```"
    with pytest.raises((ValueError, TypeError)):
        _restore(marker)


@pytest.mark.parametrize("payload", ["{", "[]", '{"version":1,"version":1}'])
def test_malformed_json_is_rejected(payload: str) -> None:
    with pytest.raises(ValueError):
        _restore("```orchestune-read-checkpoint\n" + payload + "\n```")


def test_missing_checkpoint_is_unavailable_without_a_write() -> None:
    forge = CheckpointForge()
    checkpoint = ensure_read_checkpoint(forge, REPOSITORY, ISSUE, "claim-old")
    assert checkpoint.input_base_sha is None
    assert checkpoint.availability == CheckpointAvailability.UNAVAILABLE
    assert not forge.event_comments


def test_explicit_unavailable_checkpoint_is_not_replaced() -> None:
    forge = CheckpointForge()
    unavailable = dataclasses.replace(
        _checkpoint(),
        input_base_sha=None,
        availability=CheckpointAvailability.UNAVAILABLE,
    )
    forge.create_issue_comment(ISSUE, checkpoint_marker(unavailable))
    assert (
        ensure_read_checkpoint(
            forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=LATER
        )
        == unavailable
    )
    assert len(forge.event_comments) == 1


def test_crlf_marker_is_restored_and_incomplete_marker_is_rejected() -> None:
    marker = checkpoint_marker(_checkpoint())
    assert _restore(marker.replace("\n", "\r\n")) == _checkpoint()
    with pytest.raises(ValueError, match="incomplete"):
        _restore(marker.rsplit("```", 1)[0])


def test_idempotent_persistence_keeps_initial_base_and_separates_new_claim() -> None:
    forge = CheckpointForge()
    first = ensure_read_checkpoint(
        forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=BASE
    )
    resumed = ensure_read_checkpoint(
        forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=LATER
    )
    second = ensure_read_checkpoint(
        forge, REPOSITORY, ISSUE, "claim-two", input_base_sha=LATER
    )
    assert first == resumed
    assert second.input_base_sha == LATER
    assert len(forge.event_comments) == 2


def test_response_loss_recovers_the_same_post_without_duplication() -> None:
    forge = CheckpointForge()
    original = forge.create_issue_comment

    def lost(number: int | str, body: str) -> dict[str, object]:
        original(number, body)
        raise ForgeError("response lost")

    with patch.object(forge, "create_issue_comment", side_effect=lost):
        assert (
            ensure_read_checkpoint(
                forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=BASE
            )
            == _checkpoint()
        )
    assert len(forge.event_comments) == 1


def test_failed_read_never_posts_a_new_checkpoint() -> None:
    forge = CheckpointForge()
    with patch.object(
        forge, "list_all_issue_comments", side_effect=ForgeError("offline")
    ):
        with pytest.raises(ForgeError):
            ensure_read_checkpoint(
                forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=BASE
            )
    assert not forge.event_comments


def test_failed_post_without_evidence_remains_a_failure() -> None:
    forge = CheckpointForge()
    with patch.object(forge, "create_issue_comment", side_effect=ForgeError("offline")):
        with pytest.raises(ForgeError):
            ensure_read_checkpoint(
                forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=BASE
            )
    assert not forge.event_comments


def test_success_response_without_durable_marker_is_not_accepted() -> None:
    forge = CheckpointForge()
    with patch.object(forge, "create_issue_comment", return_value={"id": 1}):
        with pytest.raises(ValueError, match="could not be verified"):
            ensure_read_checkpoint(
                forge, REPOSITORY, ISSUE, "claim-one", input_base_sha=BASE
            )


def _advance_and_rebase(repo: Path, worktree: Path) -> str:
    (worktree / "child.py").write_text("child = 1\n", encoding="utf-8")
    run_git(["add", "child.py"], cwd=worktree)
    run_git(["commit", "-m", "child change"], cwd=worktree)
    (repo / "parent.py").write_text("parent = 1\n", encoding="utf-8")
    run_git(["add", "parent.py"], cwd=repo)
    run_git(["commit", "-m", "parent change"], cwd=repo)
    run_git(["rebase", "main"], cwd=worktree)
    return run_git(["merge-base", "HEAD", "main"], cwd=worktree).stdout.strip()


def test_service_resume_after_label_failure_keeps_checkpoint(
    claim_env: dict[str, Path],
) -> None:
    forge = CheckpointForge({ISSUE: _make_issue(number=ISSUE)})
    forge.fail_add_label = True
    request = ClaimRequest(issue_number=ISSUE, state_path=claim_env["state_path"])
    with patch.object(service, "_perform_git_fetch", return_value=None):
        outcome = claim_task(
            request, forge=forge, cwd=claim_env["repo_root"], default_base="main"
        )
    assert not outcome.success
    assert outcome.stage == ClaimStage.ACTIVE_SAVED
    assert outcome.claim_id and outcome.owner_token
    comments = forge.list_all_issue_comments(ISSUE)
    checkpoint = restore_read_checkpoint(comments, REPOSITORY, ISSUE, outcome.claim_id)
    active = load_run_state(claim_env["state_path"]).active_worktrees[str(ISSUE)]
    assert checkpoint.input_base_sha == active.claim.base_sha
    assert checkpoint.availability == CheckpointAvailability.RECORDED
    assert outcome.worktree_path is not None
    advanced = _advance_and_rebase(claim_env["repo_root"], outcome.worktree_path)
    assert advanced != checkpoint.input_base_sha
    forge.fail_add_label = False
    with patch.object(service, "_perform_git_fetch", return_value=None):
        resumed = resume_claim(
            outcome.claim_id,
            outcome.owner_token,
            forge=forge,
            cwd=claim_env["repo_root"],
            state_path=claim_env["state_path"],
            default_base="main",
        )
    assert resumed.success
    assert forge.list_all_issue_comments(ISSUE) == comments


def test_checkpoint_publication_failure_preserves_ownership_and_labels(
    claim_env: dict[str, Path],
) -> None:
    forge = CheckpointForge({ISSUE: _make_issue(number=ISSUE)})
    with (
        patch.object(service, "_perform_git_fetch", return_value=None),
        patch.object(forge, "create_issue_comment", side_effect=ForgeError("offline")),
    ):
        outcome = claim_task(
            ClaimRequest(issue_number=ISSUE, state_path=claim_env["state_path"]),
            forge=forge,
            cwd=claim_env["repo_root"],
            default_base="main",
        )
    assert not outcome.success
    assert outcome.stage == ClaimStage.ACTIVE_SAVED
    assert not forge.bodies_updated
    assert not forge.labels_added
    assert not forge.labels_removed
    assert StatusLabel.QUEUED in forge.get_issue_labels(ISSUE)
    assert outcome.claim_id and outcome.owner_token
    resumed = resume_claim(
        outcome.claim_id,
        outcome.owner_token,
        forge=forge,
        cwd=claim_env["repo_root"],
        state_path=claim_env["state_path"],
        default_base="main",
    )
    assert resumed.success
    assert not forge.event_comments  # A missing checkpoint on resume is never invented.
