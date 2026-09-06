"""Regression coverage for Issue #819 child-branch finalization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from orchestune.infra.git_cli import (
    ConditionalBranchDeletionResult,
    delete_remote_branch_if_matches,
    fetch_remote_branch,
)
from orchestune.integrator.finalization import (
    find_integration_receipt,
    render_integration_receipt,
)
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.steps import (
    AutoMergeChildIntegrationStep,
    RetryChildIssueCloseStep,
)
from orchestune.integrator.types import IntegrationContext, IntegratorConfig
from tests.conftest import make_task


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _configure_identity(repository: Path) -> None:
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")


def _commit(repository: Path, filename: str, contents: str, message: str) -> str:
    (repository / filename).write_text(contents, encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def test_conditional_delete_rejects_a_tip_that_changed_during_ci(tmp_path: Path):
    """A proof for A must not delete a later B pushed to the same branch."""
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(remote))

    author = tmp_path / "author"
    _git(tmp_path, "init", "-b", "main", str(author))
    _configure_identity(author)
    _commit(author, "base.txt", "base\n", "base")
    _git(author, "remote", "add", "origin", str(remote))
    _git(author, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(author, "checkout", "-b", "child")
    commit_a = _commit(author, "child.txt", "A\n", "child A")
    _git(author, "push", "-u", "origin", "child")

    integrator = tmp_path / "integrator"
    _git(tmp_path, "clone", str(remote), str(integrator))
    fetch_remote_branch(integrator, "child")
    assert _git(integrator, "rev-parse", "origin/child") == commit_a

    writer = tmp_path / "writer"
    _git(tmp_path, "clone", str(remote), str(writer))
    _configure_identity(writer)
    _git(writer, "checkout", "child")
    commit_b = _commit(writer, "child.txt", "A\nB\n", "child B")
    _git(writer, "push", "origin", "child")

    result = delete_remote_branch_if_matches(integrator, "child", commit_a)

    assert result is ConditionalBranchDeletionResult.TIP_MISMATCH
    assert (
        _git(integrator, "ls-remote", "origin", "refs/heads/child").split()[0]
        == commit_b
    )


def test_conditional_delete_removes_an_unchanged_proven_tip(tmp_path: Path):
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(remote))
    author = tmp_path / "author"
    _git(tmp_path, "init", "-b", "main", str(author))
    _configure_identity(author)
    _commit(author, "base.txt", "base\n", "base")
    _git(author, "remote", "add", "origin", str(remote))
    _git(author, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(author, "checkout", "-b", "child")
    commit_a = _commit(author, "child.txt", "A\n", "child A")
    _git(author, "push", "-u", "origin", "child")

    integrator = tmp_path / "integrator"
    _git(tmp_path, "clone", str(remote), str(integrator))

    assert (
        delete_remote_branch_if_matches(integrator, "child", commit_a)
        is ConditionalBranchDeletionResult.DELETED
    )
    assert _git(integrator, "ls-remote", "origin", "refs/heads/child") == ""
    assert (
        delete_remote_branch_if_matches(integrator, "child", commit_a)
        is ConditionalBranchDeletionResult.ALREADY_ABSENT
    )


def test_moved_child_tip_is_not_labeled_or_closed(fake_forge, tmp_path: Path):
    """A newer B must remain actionable even after A reached the parent branch."""
    task = make_task(1, subtask_id="task-1", status_labels=("status:done",))
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="a" * 40,
    )
    config = IntegratorConfig(apply=True, parent_issue_number=100, forge=fake_forge)
    ctx = IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-100",
        temp_branch="integration/temp-parent-issue-100-test",
        merged_tasks=[task.subtask_id],
        merged_task_proofs={task.subtask_id: proof},
        active_done_tasks=[task],
        integration_pr_number=123,
    )

    with (
        patch("orchestune.integrator.steps.run_git"),
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            return_value=ConditionalBranchDeletionResult.TIP_MISMATCH,
        ) as conditional_delete,
    ):
        result = AutoMergeChildIntegrationStep().execute(ctx)

    assert result["status"] == "success"
    conditional_delete.assert_called_once_with(
        tmp_path, proof.branch_name, proof.source_sha
    )
    fake_forge.add_label.assert_not_called()
    fake_forge.close_issue.assert_not_called()


def test_receipt_recovers_after_delete_before_label(fake_forge, tmp_path: Path):
    """A retry completes a proven child when the prior process died post-delete."""
    task = make_task(1, subtask_id="task-1", status_labels=("status:done",))
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="a" * 40,
    )
    fake_forge.list_comments.return_value = [
        {
            "body": render_integration_receipt(proof, "parent/issue-100"),
            "author": "bot",
        }
    ]
    config = IntegratorConfig(apply=True, parent_issue_number=100, forge=fake_forge)
    ctx = IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-100",
        temp_branch="integration/temp-parent-issue-100-test",
        active_done_tasks=[task],
    )

    with patch(
        "orchestune.integrator.steps.delete_remote_branch_if_matches",
        return_value=ConditionalBranchDeletionResult.ALREADY_ABSENT,
    ):
        result = RetryChildIssueCloseStep().execute(ctx)

    assert result["retried_closed_issues"] == [1]
    fake_forge.is_merge_commit_reachable_from.assert_called_once_with(
        proof.source_sha, "parent/issue-100"
    )
    fake_forge.add_label.assert_called_once_with(1, "integration:included")
    fake_forge.close_issue.assert_called_once()


def test_receipt_from_a_different_author_is_not_accepted(fake_forge):
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="a" * 40,
    )
    fake_forge.get_authenticated_user.return_value = "orchestune-integrator[bot]"
    fake_forge.list_comments.return_value = [
        {
            "body": render_integration_receipt(proof, "parent/issue-100"),
            "author": "untrusted-user",
        }
    ]

    recovered = find_integration_receipt(
        fake_forge,
        proof.issue_number,
        proof.subtask_id,
        proof.branch_name,
        "parent/issue-100",
    )

    assert recovered is None


def test_receipt_embedded_in_a_trusted_comment_is_not_accepted(fake_forge):
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="a" * 40,
    )
    fake_forge.list_comments.return_value = [
        {
            "body": "CI output follows:\n"
            + render_integration_receipt(proof, "parent/issue-100"),
            "author": "bot",
        }
    ]

    recovered = find_integration_receipt(
        fake_forge,
        proof.issue_number,
        proof.subtask_id,
        proof.branch_name,
        "parent/issue-100",
    )

    assert recovered is None
