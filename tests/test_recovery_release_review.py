"""Tests for recovery --release-to-review mode (#1643)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestune.claim.workspace import ClaimWorkspace, resolve_claim_workspace
from orchestune.labels import StatusLabel
from orchestune.ledger.recovery_receipts import (
    RELEASE_OPERATION,
    build_recovery_receipt,
    recovery_receipt_key,
)
from orchestune.ledger.run_state import ActiveWorktree, RunState, save_run_state
from orchestune.models import IssueRecord
from orchestune.recovery.contracts import RecoveryRequest
from orchestune.recovery.release_review import (
    apply_release_review_label,
    verify_release_review_target,
)
from orchestune.recovery.service import recover_claim
from tests.conftest import FakeForge
from tests.dispatch_test_support import make_test_active_worktree


def _issue(
    number: int,
    labels: list[str] | tuple[str, ...],
    state: str = "OPEN",
    body: str = "",
) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"Issue {number}",
        body=body,
        labels=tuple(labels),
        created_at="2026-11-03T00:00:00Z",
        state=state,
    )


class MockForge(FakeForge):
    def __init__(self, issues: dict[int, IssueRecord] | None = None) -> None:
        super().__init__()
        self.labels_added: list[tuple[int, str]] = []
        self.labels_removed: list[tuple[int, str]] = []
        if issues:
            for num, issue in issues.items():
                self.issues[num] = issue
                self.issue_states[num] = issue.state

    def add_label(
        self, issue_number: int | str, label: str, actor: str = "bot"
    ) -> None:
        num = int(issue_number)
        self.labels_added.append((num, label))
        super().add_label(issue_number, label, actor=actor)

    def remove_label(self, issue_number: int | str, label: str) -> None:
        num = int(issue_number)
        self.labels_removed.append((num, label))
        super().remove_label(issue_number, label)


def _setup_workspace(tmp_path: Path) -> tuple[ClaimWorkspace, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    (repo / "init.txt").write_text("initial")
    subprocess.run(["git", "add", "init.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    state_dir = repo / ".orchestune"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "run_state.json"
    ws = resolve_claim_workspace(repo, explicit_state_path=state_file)
    return ws, repo, state_file


def _active_with_claim(
    issue_number: int,
    claim_id: str,
    worktree_path: Path,
    branch: str = "claude/issue-1629-task",
    repository_id: str = "test-repo",
) -> ActiveWorktree:
    return make_test_active_worktree(
        issue_number=issue_number,
        owner_kind="interactive",
        claim_id=claim_id,
        branch=branch,
        worktree_path=str(worktree_path),
        pid=None,
        repository_id=repository_id,
    )


def test_apply_release_review_label_transitions_label() -> None:
    issue = _issue(1629, [StatusLabel.IN_PROGRESS])
    forge = MockForge({1629: issue})

    apply_release_review_label(forge, 1629, (StatusLabel.IN_PROGRESS,))

    assert (1629, StatusLabel.BLOCKED_HUMAN_REVIEW) in forge.labels_added
    assert (1629, StatusLabel.IN_PROGRESS) in forge.labels_removed
    assert StatusLabel.BLOCKED_HUMAN_REVIEW in forge.get_issue_labels(1629)
    assert StatusLabel.IN_PROGRESS not in forge.get_issue_labels(1629)


def test_verify_release_review_target_accepts_open_in_progress() -> None:
    body = "## Footprint\n```yaml\nsubtask_id: task\nclaim_id: claim-1629\nfootprint: [a.py]\n```"
    issue = _issue(1629, [StatusLabel.IN_PROGRESS], state="OPEN", body=body)
    forge = MockForge({1629: issue})

    verified, old_labels = verify_release_review_target(forge, 1629, "claim-1629")
    assert verified.number == 1629
    assert StatusLabel.IN_PROGRESS in old_labels


def test_verify_release_review_target_rejects_closed_or_final_labels() -> None:
    closed_issue = _issue(101, [StatusLabel.IN_PROGRESS], state="CLOSED")
    done_issue = _issue(102, [StatusLabel.DONE], state="OPEN")
    not_needed_issue = _issue(103, [StatusLabel.NOT_NEEDED], state="OPEN")
    forge = MockForge({101: closed_issue, 102: done_issue, 103: not_needed_issue})

    with pytest.raises(ValueError, match="OPEN"):
        verify_release_review_target(forge, 101, "claim-101")

    with pytest.raises(ValueError, match="done"):
        verify_release_review_target(forge, 102, "claim-102")

    with pytest.raises(ValueError, match="not-needed"):
        verify_release_review_target(forge, 103, "claim-103")


def test_verify_release_review_target_rejects_claim_mismatch() -> None:
    body = "## Footprint\n```yaml\nsubtask_id: task\nclaim_id: claim-different\n```"
    issue = _issue(1629, [StatusLabel.IN_PROGRESS], state="OPEN", body=body)
    forge = MockForge({1629: issue})

    with pytest.raises(ValueError, match="claim"):
        verify_release_review_target(forge, 1629, "claim-1629")


def test_recover_claim_release_to_review_preview(tmp_path: Path) -> None:
    import subprocess

    ws, repo, state_file = _setup_workspace(tmp_path)
    wt = tmp_path / "wt-1629"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-1629-b", str(wt)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    active = _active_with_claim(
        1629, "claim-1629", wt, branch="wt-1629-b", repository_id=ws.repository_identity
    )
    save_run_state(RunState(active_worktrees={"1629": active}), state_file)

    body = "## Footprint\n```yaml\nclaim_id: claim-1629\n```"
    issue = _issue(1629, [StatusLabel.IN_PROGRESS], state="OPEN", body=body)
    forge = MockForge({1629: issue})

    req = RecoveryRequest(
        issue_number=1629,
        claim_id="claim-1629",
        reason="ワーカー停止確認",
        release_to_review=True,
        state_path=state_file,
    )

    result = recover_claim(req, cwd=repo, forge=forge)

    assert result.success is True
    assert result.action == "would_release_to_review"
    # No label changes in preview
    assert forge.labels_added == []
    # Active claim remains
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "1629" in state["active_worktrees"]


def test_recover_claim_release_to_review_apply(tmp_path: Path) -> None:
    import subprocess

    ws, repo, state_file = _setup_workspace(tmp_path)
    wt = tmp_path / "wt-1629"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-1629-b", str(wt)],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # create dummy dirty untracked file to verify retention
    (wt / "untracked.txt").write_text("retained work")
    active = _active_with_claim(
        1629, "claim-1629", wt, branch="wt-1629-b", repository_id=ws.repository_identity
    )
    save_run_state(RunState(active_worktrees={"1629": active}), state_file)

    body = "## Footprint\n```yaml\nclaim_id: claim-1629\n```"
    issue = _issue(1629, [StatusLabel.IN_PROGRESS], state="OPEN", body=body)
    forge = MockForge({1629: issue})

    req = RecoveryRequest(
        issue_number=1629,
        claim_id="claim-1629",
        reason="ワーカー停止確認",
        release_to_review=True,
        apply=True,
        state_path=state_file,
    )

    result = recover_claim(req, cwd=repo, forge=forge)

    assert result.success is True
    assert result.action == "released_to_review"

    # Label transitioned
    assert (1629, StatusLabel.BLOCKED_HUMAN_REVIEW) in forge.labels_added
    assert (1629, StatusLabel.IN_PROGRESS) in forge.labels_removed

    # Active claim removed
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert "1629" not in state["active_worktrees"]

    # Release receipt recorded
    receipt_key = recovery_receipt_key(
        ws.repository_identity, 1629, "claim-1629", RELEASE_OPERATION
    )
    assert receipt_key in state["recovery_receipts"]
    assert state["recovery_receipts"][receipt_key]["operation"] == RELEASE_OPERATION

    # Worktree retained
    assert (wt / "untracked.txt").read_text(encoding="utf-8") == "retained work"


def test_recover_claim_release_to_review_already_released_receipt(
    tmp_path: Path,
) -> None:
    """#1643: Operator ran normal recover first (leaving receipt without label transition).
    Running --release-to-review reconciles the label using the existing release receipt."""
    ws, repo, state_file = _setup_workspace(tmp_path)
    wt = tmp_path / "wt-1629"
    wt.mkdir(parents=True)
    active = _active_with_claim(
        1629, "claim-1629", wt, branch="wt-1629-b", repository_id=ws.repository_identity
    )

    receipt_key, receipt = build_recovery_receipt(
        active=active,
        repository_id=ws.repository_identity,
        issue_number=1629,
        operation=RELEASE_OPERATION,
        reason="normal recover earlier",
    )

    save_run_state(
        RunState(
            active_worktrees={},
            recovery_receipts={receipt_key: receipt},
        ),
        state_file,
    )

    body = "## Footprint\n```yaml\nclaim_id: claim-1629\n```"
    issue = _issue(1629, [StatusLabel.IN_PROGRESS], state="OPEN", body=body)
    forge = MockForge({1629: issue})

    req = RecoveryRequest(
        issue_number=1629,
        claim_id="claim-1629",
        reason="ラベル復旧",
        release_to_review=True,
        apply=True,
        state_path=state_file,
    )

    result = recover_claim(req, cwd=repo, forge=forge)

    assert result.success is True
    assert result.action == "released_to_review"
    assert (1629, StatusLabel.BLOCKED_HUMAN_REVIEW) in forge.labels_added
    assert (1629, StatusLabel.IN_PROGRESS) in forge.labels_removed


def test_verify_release_review_target_rejects_malformed_yaml() -> None:
    body = "## Footprint\n```yaml\n: invalid: yaml: [\n```"
    issue = _issue(1629, [StatusLabel.IN_PROGRESS], state="OPEN", body=body)
    forge = MockForge({1629: issue})

    with pytest.raises(ValueError, match="Malformed footprint YAML"):
        verify_release_review_target(forge, 1629, "claim-1629")


def test_recovery_request_validate_rejects_release_to_review_with_external_id() -> None:
    req = RecoveryRequest(
        issue_number=1629,
        claim_id="claim-1629",
        reason="test",
        release_to_review=True,
        external_id="ext-123",
    )
    with pytest.raises(ValueError, match="cannot mix recovery modes"):
        req.validate()

    req2 = RecoveryRequest(
        issue_number=1629,
        claim_id="claim-1629",
        reason="test",
        release_to_review=True,
        launch_attempt_id="launch-123",
    )
    with pytest.raises(ValueError, match="cannot mix recovery modes"):
        req2.validate()
