"""Contracts for the shared worktree operations package."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.models import PrRecord
from orchestune.worktree_ops.claim_marker import (
    claim_lock_path,
    claim_marker_path,
    read_claim_marker,
    remove_claim_marker,
    write_claim_marker,
)
from orchestune.worktree_ops.preparation import (
    WorktreePreparation,
    prepare_task_worktree,
)
from orchestune.worktree_ops.temp_branches import (
    prune_stale_integration_temp_branches,
)


def _init_repository(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    (path / "README.md").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=path, check=True)
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_claim_marker_round_trip_and_lock_are_siblings(tmp_path: Path) -> None:
    worktree = tmp_path / "task-worktree"

    write_claim_marker(
        worktree,
        claim_id="claim-123",
        branch="claim/task-123",
        base_sha="abc123",
        branch_created=True,
    )

    assert claim_marker_path(worktree) == tmp_path / "task-worktree.claim.json"
    assert claim_lock_path(worktree) == tmp_path / "task-worktree.claim.lock"
    assert read_claim_marker(worktree) == {
        "claim_id": "claim-123",
        "branch": "claim/task-123",
        "base_sha": "abc123",
        "branch_created": True,
    }

    remove_claim_marker(worktree)
    assert read_claim_marker(worktree) is None


def test_prepare_task_worktree_preserves_base_and_ownership(
    tmp_path: Path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    base_branch = _init_repository(repository)
    monkeypatch.chdir(repository)
    worktree_root = tmp_path / "worktrees"

    result = prepare_task_worktree(
        "claim/issue-123-task-1", worktree_root, base_branch, "claim-123"
    )

    assert isinstance(result, WorktreePreparation)
    assert result.accepted is True
    assert result.created is True
    assert result.branch_created is True
    assert result.base_sha
    assert result.worktree_path == worktree_root / "claim-issue-123-task-1"
    assert result.worktree_path.exists()
    assert read_claim_marker(result.worktree_path) == {
        "claim_id": "claim-123",
        "branch": "claim/issue-123-task-1",
        "base_sha": result.base_sha,
        "branch_created": True,
    }


def test_prune_stale_temp_branches_keeps_open_and_fresh_refs(fake_forge):
    with patch(
        "orchestune.worktree_ops.temp_branches.run_git", autospec=True
    ) as run_git:
        run_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "origin/integration/temp-parent-issue-1-old 100\n"
                "origin/integration/temp-parent-issue-1-open 100\n"
                "origin/integration/temp-parent-issue-1-fresh 950\n"
            ),
            stderr="",
        )
        fake_forge.list_open_prs.return_value = [
            PrRecord(
                number=1,
                head_ref="integration/temp-parent-issue-1-open",
                changed_files=(),
            )
        ]

        removed = prune_stale_integration_temp_branches(
            Path("/repo"), forge=fake_forge, now=1_000, max_age_seconds=100
        )

    assert removed == ["integration/temp-parent-issue-1-old"]
    fake_forge.delete_branch.assert_called_once_with(
        "integration/temp-parent-issue-1-old"
    )


class TestHeldIntegrationBranches:
    """#820: a held worktree's temp branch is evidence and is never auto-collected."""

    def _run(self, root: Path, fake_forge, held: bool | str):
        from orchestune.integrator.worktree import IntegrationWorktree

        if held == "unreadable":
            holds = root / "worktrees" / ".holds"
            holds.mkdir(parents=True)
            (holds / "broken.json").write_text("{not json")
        elif held:
            IntegrationWorktree(root, "integration/temp-parent-issue-1-old").write_hold(
                parent_issue_number=1, attempt_id="a", reason="stop unconfirmed"
            )
        with patch("orchestune.worktree_ops.temp_branches.run_git") as run_git:
            run_git.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="origin/integration/temp-parent-issue-1-old 100\n",
                stderr="",
            )
            fake_forge.list_open_prs.return_value = []
            return prune_stale_integration_temp_branches(
                root, forge=fake_forge, now=1_000, max_age_seconds=100
            )

    def test_a_stale_branch_without_a_hold_is_collected(self, fake_forge, tmp_path):
        assert self._run(tmp_path, fake_forge, held=False) == [
            "integration/temp-parent-issue-1-old"
        ]

    def test_a_held_branch_is_kept(self, fake_forge, tmp_path):
        assert self._run(tmp_path, fake_forge, held=True) == []
        fake_forge.delete_branch.assert_not_called()

    def test_unreadable_hold_records_collect_nothing(self, fake_forge, tmp_path):
        assert self._run(tmp_path, fake_forge, held="unreadable") == []
        fake_forge.delete_branch.assert_not_called()
