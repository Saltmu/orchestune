"""Regression coverage for Issue #819 child-branch finalization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestune.infra.git_cli import (
    ConditionalBranchDeletionResult,
    RemoteBranchReadError,
    delete_remote_branch_if_matches,
    fetch_remote_branch,
    read_remote_branch_tip,
)
from orchestune.integrator.finalization import (
    find_integration_receipt,
    render_integration_receipt,
)
from orchestune.integrator.finalization_retry import (
    BLOCKED_LABEL,
)
from orchestune.integrator.finalization_retry import (
    MARKER as DENIAL_MARKER,
)
from orchestune.integrator.proofs import TaskIntegrationProof
from orchestune.integrator.steps import (
    AutoMergeChildIntegrationStep,
    RetryChildIssueCloseStep,
)
from orchestune.integrator.types import IntegrationContext, IntegratorConfig
from orchestune.task_branch_resolution import ResolutionSource
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
    config = IntegratorConfig(
        apply=True, parent_issue_number=100, forge=fake_forge, child_review_gate="off"
    )
    ctx = IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-100",
        temp_branch="integration/temp-parent-issue-100-test",
        merged_tasks=[task.subtask_id],
        merged_task_proofs={task.issue_number: proof},
        active_done_tasks=[task],
        integration_pr_number=123,
    )

    with (
        patch("orchestune.integrator.steps.run_git", autospec=True),
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            autospec=True,
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


def test_fallback_receipt_finalizes_without_deleting_either_branch(
    fake_forge, tmp_path: Path
):
    task = make_task(1, subtask_id="task-1", status_labels=("status:done",))
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="feat/issue-1-task-1",
        source_sha="a" * 40,
        source=ResolutionSource.PR_FALLBACK,
    )
    config = IntegratorConfig(apply=True, parent_issue_number=100, forge=fake_forge)
    ctx = IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-100",
        temp_branch="integration/temp-parent-issue-100-test",
        merged_tasks=[task.subtask_id],
        merged_task_proofs={task.issue_number: proof},
        task_merge_receipts={task.issue_number: proof.merge_receipt},
        active_done_tasks=[task],
    )

    with (
        patch(
            "orchestune.integrator.steps.ensure_integration_receipt",
            autospec=True,
            return_value=True,
        ),
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            autospec=True,
        ) as conditional_delete,
    ):
        finalized = AutoMergeChildIntegrationStep()._finalize_merged_child_tasks(ctx)

    assert finalized == {"task-1"}
    conditional_delete.assert_not_called()


def test_already_integrated_verification_uses_receipt_oid_not_branch_tip(
    fake_forge, tmp_path: Path
):
    task = make_task(1, subtask_id="task-1", status_labels=("status:done",))
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="feat/issue-1-task-1",
        source_sha="a" * 40,
        source=ResolutionSource.PR_FALLBACK,
    )
    fake_forge.is_merge_commit_reachable_from.return_value = True
    config = IntegratorConfig(apply=True, parent_issue_number=100, forge=fake_forge)
    ctx = IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-100",
        temp_branch="integration/temp-parent-issue-100-test",
        merged_tasks=[task.subtask_id],
        merged_task_proofs={task.issue_number: proof},
        task_merge_receipts={task.issue_number: proof.merge_receipt},
        active_done_tasks=[task],
    )

    assert AutoMergeChildIntegrationStep()._verify_already_integrated(ctx) is True
    fake_forge.is_merge_commit_reachable_from.assert_called_once_with(
        "a" * 40, "parent/issue-100"
    )
    fake_forge.is_current_branch_tip_merged_into.assert_not_called()


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
        autospec=True,
        return_value=ConditionalBranchDeletionResult.ALREADY_ABSENT,
    ):
        result = RetryChildIssueCloseStep().execute(ctx)

    assert result["retried_closed_issues"] == [1]
    fake_forge.is_merge_commit_reachable_from.assert_called_once_with(
        proof.source_sha, "parent/issue-100"
    )
    fake_forge.add_label.assert_called_once_with(1, "integration:included")
    fake_forge.close_issue.assert_called_once()


def test_receipt_label_failure_does_not_return_task_to_integration(
    fake_forge, tmp_path: Path
):
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
    fake_forge.add_label.side_effect = RuntimeError("temporary API failure")
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
        autospec=True,
        return_value=ConditionalBranchDeletionResult.ALREADY_ABSENT,
    ):
        result = RetryChildIssueCloseStep().execute(ctx)

    assert result["status"] == "no_done_tasks"
    assert ctx.active_done_tasks == []
    fake_forge.close_issue.assert_not_called()


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
        "parent/issue-100",
    )

    assert recovered is None


def test_receipt_oid_validation_is_case_consistent(fake_forge):
    proof = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="A" * 40,
    )
    fake_forge.list_comments.return_value = [
        {
            "body": render_integration_receipt(proof, "parent/issue-100"),
            "author": "bot",
        }
    ]

    recovered = find_integration_receipt(
        fake_forge,
        proof.issue_number,
        proof.subtask_id,
        "parent/issue-100",
    )

    assert recovered == proof


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
        "parent/issue-100",
    )

    assert recovered is None


def _origin_with_child(tmp_path: Path) -> tuple[Path, Path, str]:
    """A bare origin holding ``child`` at one commit, plus an integrator clone."""
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
    return remote, integrator, commit_a


def test_a_remote_that_forbids_deletions_is_classified_as_denied(tmp_path: Path):
    remote, integrator, commit_a = _origin_with_child(tmp_path)
    _git(remote, "config", "receive.denyDeletes", "true")

    result = delete_remote_branch_if_matches(integrator, "child", commit_a)

    assert result is ConditionalBranchDeletionResult.DENIED
    assert _git(integrator, "ls-remote", "origin", "refs/heads/child") != ""


def test_a_declining_pre_receive_hook_is_classified_as_denied(tmp_path: Path):
    """GitHub rulesets reject through the same `[remote rejected]` path."""
    remote, integrator, commit_a = _origin_with_child(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\necho 'GH013: Repository rule violations found' >&2\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = delete_remote_branch_if_matches(integrator, "child", commit_a)

    assert result is ConditionalBranchDeletionResult.DENIED


def test_an_unreachable_remote_is_failed_not_denied(tmp_path: Path):
    """No permanent-failure evidence: connection problems stay transient."""
    _remote, integrator, commit_a = _origin_with_child(tmp_path)
    _git(integrator, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    result = delete_remote_branch_if_matches(integrator, "child", commit_a)

    assert result is ConditionalBranchDeletionResult.FAILED


def test_a_moved_tip_is_still_a_tip_mismatch_when_deletions_are_forbidden(
    tmp_path: Path,
):
    """The lease check runs before the remote policy can reject the push."""
    remote, integrator, _commit_a = _origin_with_child(tmp_path)
    _git(remote, "config", "receive.denyDeletes", "true")

    result = delete_remote_branch_if_matches(integrator, "child", "b" * 40)

    assert result is ConditionalBranchDeletionResult.TIP_MISMATCH


def test_read_remote_branch_tip_reports_the_tip_or_absence(tmp_path: Path):
    _remote, integrator, commit_a = _origin_with_child(tmp_path)

    assert read_remote_branch_tip(integrator, "child") == commit_a
    assert read_remote_branch_tip(integrator, "no-such-branch") is None


def test_read_remote_branch_tip_does_not_mistake_a_failure_for_absence(
    tmp_path: Path,
):
    _remote, integrator, _commit_a = _origin_with_child(tmp_path)
    _git(integrator, "remote", "set-url", "origin", str(tmp_path / "missing.git"))

    with pytest.raises(RemoteBranchReadError):
        read_remote_branch_tip(integrator, "child")


_RECEIPT_BASE = "parent/issue-100"


def _proof() -> TaskIntegrationProof:
    return TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="a" * 40,
    )


def _store_comments(fake_forge) -> dict[int, list[dict[str, str]]]:
    """Back the Forge double with a comment store that starts with the receipt."""
    store: dict[int, list[dict[str, str]]] = {
        1: [
            {
                "body": render_integration_receipt(_proof(), _RECEIPT_BASE),
                "author": "bot",
            }
        ]
    }
    fake_forge.list_comments.side_effect = lambda n: [dict(c) for c in store.get(n, [])]
    fake_forge.add_comment.side_effect = lambda n, body: store.setdefault(n, []).append(
        {"body": body, "author": "bot"}
    )
    return store


def _retry_ctx(fake_forge, tmp_path: Path, run_id: str, *labels: str):
    task = make_task(1, subtask_id="task-1", status_labels=("status:done", *labels))
    config = IntegratorConfig(
        apply=True,
        parent_issue_number=100,
        forge=fake_forge,
        integration_run_id=run_id,
    )
    return IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch=f"origin/{_RECEIPT_BASE}",
        temp_branch="integration/temp-parent-issue-100-test",
        active_done_tasks=[task],
    )


def _retry_with(fake_forge, tmp_path: Path, run_id: str, deletion, *labels: str):
    ctx = _retry_ctx(fake_forge, tmp_path, run_id, *labels)
    with patch(
        "orchestune.integrator.steps.delete_remote_branch_if_matches",
        autospec=True,
        return_value=deletion,
    ) as conditional_delete:
        result = RetryChildIssueCloseStep().execute(ctx)
    return ctx, result, conditional_delete


def _denial_events(store) -> list[str]:
    return [c["body"] for c in store[1] if DENIAL_MARKER in c["body"]]


def test_denied_deletion_is_deferred_and_counted_not_reintegrated(
    fake_forge, tmp_path: Path
):
    store = _store_comments(fake_forge)

    ctx, result, _ = _retry_with(
        fake_forge, tmp_path, "run-1", ConditionalBranchDeletionResult.DENIED
    )

    # No worktree / push / PR round trip: the task leaves the cycle entirely.
    assert result["status"] == "no_done_tasks"
    assert ctx.active_done_tasks == []
    assert result["finalization_deferred"] == [1]
    assert len(_denial_events(store)) == 1
    fake_forge.close_issue.assert_not_called()
    fake_forge.add_label.assert_not_called()


def test_transient_failure_is_deferred_but_never_counted(fake_forge, tmp_path: Path):
    store = _store_comments(fake_forge)

    ctx, result, _ = _retry_with(
        fake_forge, tmp_path, "run-1", ConditionalBranchDeletionResult.FAILED
    )

    assert ctx.active_done_tasks == []
    assert result["finalization_deferred"] == [1]
    assert _denial_events(store) == []
    fake_forge.close_issue.assert_not_called()


def test_moved_tip_still_returns_the_task_to_integration(fake_forge, tmp_path: Path):
    store = _store_comments(fake_forge)

    ctx, result, _ = _retry_with(
        fake_forge, tmp_path, "run-1", ConditionalBranchDeletionResult.TIP_MISMATCH
    )

    assert [task.issue_number for task in ctx.active_done_tasks] == [1]
    assert result["status"] == "success"
    assert _denial_events(store) == []


def test_the_third_denied_cycle_escalates_without_changing_status_labels(
    fake_forge, tmp_path: Path
):
    store = _store_comments(fake_forge)

    escalated = []
    for run in ("run-1", "run-2", "run-3"):
        _ctx, result, _ = _retry_with(
            fake_forge, tmp_path, run, ConditionalBranchDeletionResult.DENIED
        )
        escalated.append(result.get("finalization_escalated", []))

    assert escalated == [[], [], [1]]
    fake_forge.add_label.assert_called_once_with(1, BLOCKED_LABEL)
    fake_forge.remove_label.assert_not_called()
    fake_forge.close_issue.assert_not_called()
    assert len(_denial_events(store)) == 4  # three denials + the terminal record


def test_a_blocked_child_does_not_attempt_deletion_and_holds(
    fake_forge, tmp_path: Path
):
    _store_comments(fake_forge)
    ctx = _retry_ctx(fake_forge, tmp_path, "run-4", BLOCKED_LABEL)

    with (
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            autospec=True,
        ) as conditional_delete,
        patch(
            "orchestune.integrator.steps.read_remote_branch_tip",
            autospec=True,
            return_value="a" * 40,
        ),
    ):
        result = RetryChildIssueCloseStep().execute(ctx)

    conditional_delete.assert_not_called()
    assert ctx.active_done_tasks == []
    assert result["finalization_deferred"] == [1]
    fake_forge.close_issue.assert_not_called()


def test_a_blocked_child_is_finalized_once_the_branch_was_removed(
    fake_forge, tmp_path: Path
):
    _store_comments(fake_forge)
    ctx = _retry_ctx(fake_forge, tmp_path, "run-4", BLOCKED_LABEL)

    with (
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            autospec=True,
        ) as conditional_delete,
        patch(
            "orchestune.integrator.steps.read_remote_branch_tip",
            autospec=True,
            return_value=None,
        ),
    ):
        result = RetryChildIssueCloseStep().execute(ctx)

    conditional_delete.assert_not_called()
    assert result["retried_closed_issues"] == [1]
    fake_forge.remove_label.assert_called_once_with(1, BLOCKED_LABEL)
    fake_forge.add_label.assert_called_once_with(1, "integration:included")
    fake_forge.close_issue.assert_called_once()


def test_a_blocked_child_whose_tip_moved_returns_to_integration(
    fake_forge, tmp_path: Path
):
    _store_comments(fake_forge)
    ctx = _retry_ctx(fake_forge, tmp_path, "run-4", BLOCKED_LABEL)

    with patch(
        "orchestune.integrator.steps.read_remote_branch_tip",
        autospec=True,
        return_value="b" * 40,
    ):
        RetryChildIssueCloseStep().execute(ctx)

    assert [task.issue_number for task in ctx.active_done_tasks] == [1]
    fake_forge.remove_label.assert_called_once_with(1, BLOCKED_LABEL)
    fake_forge.close_issue.assert_not_called()


def test_denied_deletion_in_the_merge_cycle_is_counted_and_not_finalized(
    fake_forge, tmp_path: Path
):
    store = _store_comments(fake_forge)
    task = make_task(1, subtask_id="task-1", status_labels=("status:done",))
    config = IntegratorConfig(
        apply=True,
        parent_issue_number=100,
        forge=fake_forge,
        child_review_gate="off",
        integration_run_id="run-1",
    )
    ctx = IntegrationContext(
        config=config,
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch=f"origin/{_RECEIPT_BASE}",
        temp_branch="integration/temp-parent-issue-100-test",
        merged_tasks=[task.subtask_id],
        merged_task_proofs={task.issue_number: _proof()},
        active_done_tasks=[task],
        integration_pr_number=123,
    )

    with (
        patch("orchestune.integrator.steps.run_git", autospec=True),
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            autospec=True,
            return_value=ConditionalBranchDeletionResult.DENIED,
        ),
    ):
        result = AutoMergeChildIntegrationStep().execute(ctx)

    assert result["status"] == "success"
    assert len(_denial_events(store)) == 1
    fake_forge.add_label.assert_not_called()
    fake_forge.close_issue.assert_not_called()
