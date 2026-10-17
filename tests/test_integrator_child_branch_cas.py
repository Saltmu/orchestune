"""Regression coverage for Issue #819 child-branch finalization."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
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
    handle_denied_deletion,
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


def _seed_escalation(fake_forge, proof: TaskIntegrationProof | None = None) -> None:
    """Leave the child Issue as three refused cycles and an escalation would."""
    for run in ("run-1", "run-2", "run-3"):
        handle_denied_deletion(
            fake_forge, proof or _proof(), f"origin/{_RECEIPT_BASE}", run, 100
        )
    fake_forge.add_label.reset_mock()
    fake_forge.ensure_labels.reset_mock()


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
    _seed_escalation(fake_forge)
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
    _seed_escalation(fake_forge)
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
    _seed_escalation(fake_forge)
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


def _run_blocked(fake_forge, tmp_path: Path, *, tip: str | None):
    ctx = _retry_ctx(fake_forge, tmp_path, "run-9", BLOCKED_LABEL)
    with (
        patch(
            "orchestune.integrator.steps.delete_remote_branch_if_matches",
            autospec=True,
        ) as conditional_delete,
        patch(
            "orchestune.integrator.steps.read_remote_branch_tip",
            autospec=True,
            return_value=tip,
        ),
    ):
        result = RetryChildIssueCloseStep().execute(ctx)
    return ctx, result, conditional_delete


def test_a_blocked_child_stays_held_when_its_receipt_cannot_be_found(
    fake_forge, tmp_path: Path
):
    """A transient receipt lookup failure must not send a labeled child back
    through merge and finalization, which would retry the forbidden deletion."""
    store = _store_comments(fake_forge)
    _seed_escalation(fake_forge)
    store[1] = [c for c in store[1] if DENIAL_MARKER in c["body"]]

    ctx, result, conditional_delete = _run_blocked(fake_forge, tmp_path, tip="a" * 40)

    assert ctx.active_done_tasks == []
    assert result["finalization_deferred"] == [1]
    conditional_delete.assert_not_called()
    fake_forge.close_issue.assert_not_called()


def test_a_blocked_child_is_settled_from_the_newest_proof_not_the_oldest_receipt(
    fake_forge, tmp_path: Path
):
    """Receipts for A and B exist (A first). The block is on B, whose tip is
    still on the remote, so the child must keep holding."""
    store = _store_comments(fake_forge)
    newer = TaskIntegrationProof(
        issue_number=1,
        subtask_id="task-1",
        branch_name="claude/issue-1-task-1",
        source_sha="b" * 40,
    )
    store[1].append(
        {"body": render_integration_receipt(newer, _RECEIPT_BASE), "author": "bot"}
    )
    _seed_escalation(fake_forge, newer)

    ctx, result, _ = _run_blocked(fake_forge, tmp_path, tip="b" * 40)

    assert ctx.active_done_tasks == []
    assert result["finalization_deferred"] == [1]
    fake_forge.remove_label.assert_not_called()


def test_a_blocked_child_whose_proof_does_not_reach_the_parent_stays_held(
    fake_forge, tmp_path: Path
):
    _store_comments(fake_forge)
    _seed_escalation(fake_forge)
    fake_forge.is_merge_commit_reachable_from.return_value = False

    ctx, result, conditional_delete = _run_blocked(fake_forge, tmp_path, tip=None)

    assert ctx.active_done_tasks == []
    assert result["finalization_deferred"] == [1]
    conditional_delete.assert_not_called()
    fake_forge.close_issue.assert_not_called()
    fake_forge.remove_label.assert_not_called()


def _setup_receipt_comments(fake_forge):
    comments_by_issue: dict[int, list[dict[str, Any]]] = {}

    def add_comment(issue_number, body):
        comments_by_issue.setdefault(int(issue_number), []).append(
            {"body": body, "author": fake_forge.get_authenticated_user()}
        )

    def list_comments(issue_number):
        return list(comments_by_issue.get(int(issue_number), []))

    fake_forge.add_comment.side_effect = add_comment
    fake_forge.list_comments.side_effect = list_comments


def test_ensure_integration_receipt_persists_pr_number_and_does_not_duplicate(
    fake_forge,
):
    from orchestune.integrator.finalization import (
        ensure_integration_receipt,
        find_latest_integration_receipt,
    )

    _setup_receipt_comments(fake_forge)

    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="c" * 40,
    )

    # 初回保存
    assert ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=123
    )
    # 同一PR番号での再保存は重複せず True
    assert ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=123
    )

    comments = fake_forge.list_comments(10)
    assert len(comments) == 1

    target = find_latest_integration_receipt(
        fake_forge, 10, "task-10", "origin/parent/issue-1"
    )
    assert target is not None
    assert target.has_integration_pr_key is True
    assert target.integration_pr_number == 123
    assert target.notice_base_branch == "parent/issue-1"


def test_find_latest_integration_receipt_prefers_new_form_over_old_form(
    fake_forge,
):
    from orchestune.integrator.finalization import (
        ensure_integration_receipt,
        find_latest_integration_receipt,
        render_integration_receipt,
    )

    _setup_receipt_comments(fake_forge)

    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="d" * 40,
    )
    # 旧形式 receipt を先に追加
    fake_forge.add_comment(
        10, render_integration_receipt(proof, "origin/parent/issue-1")
    )
    # 新形式 receipt（PR番号付き）を追加
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=456
    )

    target = find_latest_integration_receipt(
        fake_forge, 10, "task-10", "origin/parent/issue-1"
    )
    assert target is not None
    assert target.has_integration_pr_key is True
    assert target.integration_pr_number == 456


def test_find_latest_integration_receipt_with_null_pr_number(fake_forge):
    from orchestune.integrator.finalization import (
        ensure_integration_receipt,
        find_latest_integration_receipt,
    )

    _setup_receipt_comments(fake_forge)

    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="e" * 40,
    )
    # integration_pr_number が None（null）
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=None
    )

    target = find_latest_integration_receipt(
        fake_forge, 10, "task-10", "origin/parent/issue-1"
    )
    assert target is not None
    assert target.has_integration_pr_key is True
    assert target.integration_pr_number is None


def test_retry_close_defers_and_does_not_close_when_notice_is_unknown(
    fake_forge, tmp_path: Path
):
    from orchestune.integrator.finalization import ensure_integration_receipt
    from orchestune.pr_link_notice import render_merged_notice

    _setup_receipt_comments(fake_forge)

    task = make_task(
        10,
        subtask_id="task-10",
        status_labels=("status:done", "integration:included"),
    )
    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="f" * 40,
    )
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=789
    )
    # 他者によるマーカーがあり、collaborators API が失敗することにより UNKNOWN
    fake_forge.create_issue_comment(
        10, render_merged_notice(789, "parent/issue-1"), author="untrusted_author"
    )
    fake_forge.list_notice_trusted_authors.side_effect = RuntimeError(
        "No collaborators API"
    )

    ctx = IntegrationContext(
        config=IntegratorConfig(
            parent_issue_number=1,
            base_branch="parent/issue-1",
            apply=True,
            forge=fake_forge,
        ),
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-1",
        temp_branch="integration/temp-parent-issue-1-test",
        active_done_tasks=[task],
    )

    report = RetryChildIssueCloseStep().execute(ctx)

    assert 10 not in report.get("retried_closed_issues", [])
    assert 10 in report.get("finalization_deferred", [])
    fake_forge.close_issue.assert_not_called()


def test_retry_close_closes_with_generic_comment_when_pr_number_is_null(
    fake_forge, tmp_path: Path
):
    from orchestune.integrator.finalization import ensure_integration_receipt

    _setup_receipt_comments(fake_forge)

    task = make_task(
        10,
        subtask_id="task-10",
        status_labels=("status:done", "integration:included"),
    )
    proof = TaskIntegrationProof(
        issue_number=10,
        subtask_id="task-10",
        branch_name="claude/issue-10-task-10",
        source_sha="1" * 40,
    )
    ensure_integration_receipt(
        fake_forge, proof, "origin/parent/issue-1", integration_pr_number=None
    )

    ctx = IntegrationContext(
        config=IntegratorConfig(
            parent_issue_number=1,
            base_branch="parent/issue-1",
            apply=True,
            forge=fake_forge,
        ),
        repository_root=tmp_path,
        original_root=tmp_path,
        base_branch="origin/parent/issue-1",
        temp_branch="integration/temp-parent-issue-1-test",
        active_done_tasks=[task],
    )

    report = RetryChildIssueCloseStep().execute(ctx)

    assert report.get("retried_closed_issues") == [10]
    fake_forge.close_issue.assert_called_once()
    assert "完了済み" in (fake_forge.close_issue.call_args.kwargs.get("comment") or "")
