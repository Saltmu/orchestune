"""Receipt and notice retry regressions for Issue #685."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestune.forge import Forge
from orchestune.infra.git_cli import ConditionalBranchDeletionResult
from orchestune.integrator import RetryChildIssueCloseStep as PublicRetryStep
from orchestune.integrator.finalization import render_integration_receipt
from orchestune.integrator.steps import RetryChildIssueCloseStep
from orchestune.integrator.types import IntegrationContext, IntegratorConfig
from orchestune.pr_link_notice import render_merged_notice
from tests.conftest import make_task
from tests.forge_event_support import install_event_comment_store
from tests.test_integrator_finalization import BASE, comment, payload, proof


def retry_context(forge, root: Path, *, included=True, apply=True, number=1):
    labels = ("status:done", "integration:included") if included else ("status:done",)
    return IntegrationContext(
        config=IntegratorConfig(parent_issue_number=100, apply=apply, forge=forge),
        repository_root=root,
        original_root=root,
        base_branch="origin/parent/issue-100",
        temp_branch="integration/temp-test",
        active_done_tasks=[make_task(number, status_labels=labels)],
    )


@pytest.mark.parametrize(
    "failed_api", ["get_authenticated_user", "list_all_issue_comments"]
)
def test_included_child_is_held_when_receipt_cannot_be_read(
    fake_forge: MagicMock, tmp_path: Path, failed_api: str
):
    getattr(fake_forge, failed_api).side_effect = RuntimeError("temporary outage")
    ctx = retry_context(fake_forge, tmp_path)

    report = RetryChildIssueCloseStep().execute(ctx)

    fake_forge.close_issue.assert_not_called()
    fake_forge.add_label.assert_not_called()
    assert ctx.active_done_tasks == []
    assert report["finalization_deferred"] == [1]


def fresh_forge(comments=()):
    forge = MagicMock(spec=Forge)
    store = install_event_comment_store(forge)
    store.extend(dict(c) for c in comments)
    forge.get_authenticated_user.return_value = "bot"
    forge.list_notice_trusted_authors.return_value = frozenset({"bot"})
    forge.is_merge_commit_reachable_from.return_value = True
    return forge


def stored_receipt(
    number=1, pr_number=100, *, id=1, at="2026-10-18T00:00:00Z", **fields
):
    data = payload(**fields)
    data.update(
        issue_number=number,
        subtask_id=f"task-{number}",
        integration_pr_number=pr_number,
    )
    return dict(comment(data, id=id, at=at), issue_number=number)


def test_restarts_restore_each_child_pr_and_do_not_overwrite_cycle_context(tmp_path):
    comments = [stored_receipt(1, 100), stored_receipt(2, 200)]
    for _ in range(2):
        forge = fresh_forge(comments)
        forge.create_issue_comment.side_effect = RuntimeError("outage")
        ctx = retry_context(forge, tmp_path)
        ctx.active_done_tasks += retry_context(
            forge, tmp_path, number=2
        ).active_done_tasks
        ctx.integration_pr_number = 999
        report = RetryChildIssueCloseStep().execute(ctx)
        assert report["finalization_deferred"] == [1, 2]
        assert ctx.active_done_tasks == []
        assert ctx.integration_pr_number == 999
        forge.close_issue.assert_not_called()
        comments = forge.event_comments

    forge = fresh_forge(comments)
    ctx = retry_context(forge, tmp_path)
    ctx.active_done_tasks += retry_context(forge, tmp_path, number=2).active_done_tasks
    assert RetryChildIssueCloseStep().execute(ctx)["retried_closed_issues"] == [1, 2]
    assert forge.create_issue_comment.call_args_list[0].args == (
        1,
        render_merged_notice(100, BASE),
    )
    assert forge.create_issue_comment.call_args_list[1].args == (
        2,
        render_merged_notice(200, BASE),
    )


def test_post_accepted_then_response_lost_and_close_failure_recover_without_duplicates(
    tmp_path,
):
    forge = fresh_forge([stored_receipt()])
    original_post = forge.create_issue_comment.side_effect

    def accept_then_fail(*args):
        original_post(*args)
        raise RuntimeError("response lost")

    forge.create_issue_comment.side_effect = accept_then_fail
    report = RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path))
    assert report["finalization_deferred"] == [1]
    forge.close_issue.assert_not_called()
    assert len(forge.event_comments) == 2

    second = fresh_forge(forge.event_comments)
    second.close_issue.side_effect = RuntimeError("close failed")
    assert (
        RetryChildIssueCloseStep().execute(retry_context(second, tmp_path))[
            "retried_closed_issues"
        ]
        == []
    )
    second.create_issue_comment.assert_not_called()

    third = fresh_forge(second.event_comments)
    assert RetryChildIssueCloseStep().execute(retry_context(third, tmp_path))[
        "retried_closed_issues"
    ] == [1]
    third.create_issue_comment.assert_not_called()
    third.close_issue.assert_called_once_with(1, "completed", comment=None)


def test_lookup_outage_recovers_with_a_new_forge(tmp_path):
    forge = fresh_forge([stored_receipt()])
    forge.list_all_issue_comments.side_effect = RuntimeError("outage")
    report = RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path))
    assert report["finalization_deferred_reasons"] == {1: "receipt_lookup_failed"}
    forge.close_issue.assert_not_called()
    restarted = fresh_forge(forge.event_comments)
    assert RetryChildIssueCloseStep().execute(retry_context(restarted, tmp_path))[
        "retried_closed_issues"
    ] == [1]
    restarted.create_issue_comment.assert_called_once()


@pytest.mark.parametrize("included", [False, True])
def test_invalid_receipt_holds_before_any_deletion_label_or_remerge(tmp_path, included):
    forge = fresh_forge([stored_receipt(), stored_receipt(pr_number=True)])
    with patch(
        "orchestune.integrator.child_issue_close.delete_remote_branch_if_matches"
    ) as deletion:
        ctx = retry_context(forge, tmp_path, included=included)
        report = RetryChildIssueCloseStep().execute(ctx)
    assert ctx.active_done_tasks == []
    assert report["finalization_deferred_reasons"] == {1: "receipt_invalid"}
    deletion.assert_not_called()
    forge.add_label.assert_not_called()
    forge.close_issue.assert_not_called()


def test_label_recovery_uses_same_latest_proof_as_notice(tmp_path):
    forge = fresh_forge(
        [
            stored_receipt(id=1),
            stored_receipt(pr_number=200, source_sha="b" * 40, id=2),
        ]
    )
    with patch(
        "orchestune.integrator.child_issue_close.delete_remote_branch_if_matches",
        return_value=ConditionalBranchDeletionResult.ALREADY_ABSENT,
    ) as deletion:
        report = RetryChildIssueCloseStep().execute(
            retry_context(forge, tmp_path, included=False)
        )
    assert report["retried_closed_issues"] == [1]
    forge.is_merge_commit_reachable_from.assert_called_once_with("b" * 40, BASE)
    deletion.assert_called_once_with(tmp_path, proof().branch_name, "b" * 40)
    forge.create_issue_comment.assert_called_once_with(
        1, render_merged_notice(200, BASE)
    )
    forge.list_comments.assert_not_called()
    assert (
        forge.list_all_issue_comments.call_count == 2
    )  # one receipt lookup + notice read


@pytest.mark.parametrize("guard", ["unreachable", "tip_mismatch", "label_failure"])
def test_latest_proof_does_not_fall_back_to_old_proof_or_bypass_cas(tmp_path, guard):
    forge = fresh_forge(
        [stored_receipt(id=1), stored_receipt(source_sha="b" * 40, id=2)]
    )
    if guard == "unreachable":
        forge.is_merge_commit_reachable_from.side_effect = (
            lambda sha, _: sha == "a" * 40
        )
    if guard == "label_failure":
        forge.add_label.side_effect = RuntimeError("label outage")
    result = (
        ConditionalBranchDeletionResult.TIP_MISMATCH
        if guard == "tip_mismatch"
        else ConditionalBranchDeletionResult.ALREADY_ABSENT
    )
    with patch(
        "orchestune.integrator.child_issue_close.delete_remote_branch_if_matches",
        return_value=result,
    ) as deletion:
        ctx = retry_context(forge, tmp_path, included=False)
        RetryChildIssueCloseStep().execute(ctx)
    forge.close_issue.assert_not_called()
    forge.create_issue_comment.assert_not_called()
    forge.is_merge_commit_reachable_from.assert_called_once_with("b" * 40, BASE)
    assert bool(ctx.active_done_tasks) == (guard != "label_failure")
    if guard == "unreachable":
        deletion.assert_not_called()


@pytest.mark.parametrize("included", [True, False])
def test_receipt_order_conflict_holds_even_without_included(tmp_path, included):
    forge = fresh_forge([stored_receipt(), stored_receipt(pr_number=200)])
    ctx = retry_context(forge, tmp_path, included=included)
    report = RetryChildIssueCloseStep().execute(ctx)
    assert ctx.active_done_tasks == []
    assert report["finalization_deferred_reasons"] == {1: "receipt_order_unavailable"}
    forge.close_issue.assert_not_called()


@pytest.mark.parametrize("state", ["absent", "legacy", "null"])
def test_generic_close_only_for_confirmed_compatible_states(tmp_path, state):
    forge = fresh_forge()
    if state == "null":
        forge.event_comments.append(stored_receipt(pr_number=None))
    if state == "legacy":
        forge.create_issue_comment(1, render_integration_receipt(proof(), BASE))
        forge.create_issue_comment.reset_mock()
    assert RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path))[
        "retried_closed_issues"
    ] == [1]
    forge.create_issue_comment.assert_not_called()
    assert "完了済み" in forge.close_issue.call_args.kwargs["comment"]


def test_unlabeled_lookup_outage_keeps_existing_unrecovered_behavior(tmp_path):
    forge = fresh_forge()
    forge.list_all_issue_comments.side_effect = RuntimeError("outage")
    ctx = retry_context(forge, tmp_path, included=False)
    RetryChildIssueCloseStep().execute(ctx)
    assert len(ctx.active_done_tasks) == 1
    forge.close_issue.assert_not_called()


def test_dry_run_performs_no_receipt_reads_or_mutations(tmp_path):
    forge = fresh_forge([stored_receipt()])
    RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path, apply=False))
    forge.get_authenticated_user.assert_not_called()
    forge.list_all_issue_comments.assert_not_called()
    forge.close_issue.assert_not_called()
    forge.add_label.assert_not_called()
    forge.create_issue_comment.assert_not_called()


def test_public_class_identity():
    from orchestune.integrator.child_issue_close import (
        RetryChildIssueCloseStep as Extracted,
    )

    assert PublicRetryStep is RetryChildIssueCloseStep is Extracted


@pytest.mark.parametrize("included", [True, False])
@pytest.mark.parametrize("failure", ["notice_read", "trust", "write"])
def test_notice_gate_still_holds_after_receipt_lookup(tmp_path, included, failure):
    forge = fresh_forge([stored_receipt()])
    if failure == "notice_read":
        native_list = forge.list_all_issue_comments.side_effect
        first = native_list(1)
        forge.list_all_issue_comments.side_effect = [first, RuntimeError("read outage")]
    elif failure == "trust":
        forge.create_issue_comment(1, render_merged_notice(100, BASE), author="other")
        forge.create_issue_comment.reset_mock()
        forge.list_notice_trusted_authors.side_effect = RuntimeError("trust outage")
    else:
        forge.create_issue_comment.side_effect = RuntimeError("write outage")
    with patch(
        "orchestune.integrator.child_issue_close.delete_remote_branch_if_matches",
        return_value=ConditionalBranchDeletionResult.ALREADY_ABSENT,
    ):
        ctx = retry_context(forge, tmp_path, included=included)
        report = RetryChildIssueCloseStep().execute(ctx)
    assert report["finalization_deferred"] == [1]
    assert ctx.active_done_tasks == []
    forge.close_issue.assert_not_called()


def test_corrected_receipt_history_can_resume_after_invalid_hold(tmp_path):
    forge = fresh_forge([stored_receipt(), stored_receipt(pr_number=-1, id=2)])
    assert RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path))[
        "finalization_deferred"
    ] == [1]
    forge.event_comments.append(stored_receipt(pr_number=200, id=3))
    assert RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path))[
        "finalization_deferred"
    ] == [1]
    forge.event_comments[:] = [c for c in forge.event_comments if c["id"] != 2]
    assert RetryChildIssueCloseStep().execute(retry_context(forge, tmp_path))[
        "retried_closed_issues"
    ] == [1]
    forge.create_issue_comment.assert_called_once_with(
        1, render_merged_notice(200, BASE)
    )
