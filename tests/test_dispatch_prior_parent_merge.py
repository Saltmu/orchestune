from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

from orchestune.dispatch.prior_parent_merge import (
    PriorParentMergeStatus,
    evaluate_prior_parent_merge,
    reconcile_prior_parent_merges,
)
from orchestune.labels import StatusLabel
from orchestune.models import IssueRecord, PrRecord, Task


def _merged_pr(**changes: object) -> PrRecord:
    values: dict[str, object] = {
        "number": 300,
        "head_ref": "claude/issue-101-child-task",
        "changed_files": (),
        "state": "MERGED",
        "base_ref": "parent/issue-100",
        "is_cross_repository": False,
        "closes_issue_numbers": (101,),
        "merged_at": "2026-09-01T12:00:00Z",
        "merge_commit_oid": "a" * 40,
    }
    values.update(changes)
    return PrRecord(**values)  # type: ignore[arg-type]


def _evaluate(
    prs: list[PrRecord],
    *,
    reopened_at: str | None = None,
    reachable: bool | None = True,
):
    return evaluate_prior_parent_merge(
        issue_number=101,
        parent_issue_number=100,
        subtask_id="child-task",
        prs=prs,
        get_last_reopened_at=lambda: reopened_at,
        merge_commit_is_reachable=lambda _sha, _base: reachable,
    )


def test_accepts_merged_canonical_child_pr_with_reachable_parent_evidence():
    result = _evaluate([_merged_pr()])

    assert result.status is PriorParentMergeStatus.ALREADY_MERGED
    assert result.pr_number == 300
    assert result.base_ref == "parent/issue-100"
    assert result.merged_at == "2026-09-01T12:00:00Z"


def test_accepts_explicit_closing_reference_when_head_is_not_canonical():
    result = _evaluate([_merged_pr(head_ref="fix/manual-repair")])

    assert result.status is PriorParentMergeStatus.ALREADY_MERGED


def test_does_not_treat_title_or_body_mentions_as_completion_evidence():
    result = _evaluate(
        [
            _merged_pr(
                head_ref="fix/unrelated",
                closes_issue_numbers=(),
                title="follow-up for #101",
                body="mentions #101 only",
            )
        ]
    )

    assert result.status is PriorParentMergeStatus.NOT_FOUND


def test_rejects_other_parent_and_cross_repository_prs():
    wrong_parent = _evaluate([_merged_pr(base_ref="parent/issue-999")])
    cross_repository = _evaluate([_merged_pr(is_cross_repository=True)])

    assert wrong_parent.status is PriorParentMergeStatus.NOT_FOUND
    assert cross_repository.status is PriorParentMergeStatus.NOT_FOUND


def test_reopen_at_or_after_merge_prevents_reclosing_including_same_second():
    same_second = _evaluate([_merged_pr()], reopened_at="2026-09-01T12:00:00Z")
    later = _evaluate([_merged_pr()], reopened_at="2026-09-01T12:00:01Z")

    assert same_second.status is PriorParentMergeStatus.NOT_FOUND
    assert later.status is PriorParentMergeStatus.NOT_FOUND


def test_branch_recreation_or_new_parent_tip_invalidates_old_merge_record():
    result = _evaluate([_merged_pr()], reachable=False)

    assert result.status is PriorParentMergeStatus.NOT_FOUND


def test_missing_required_metadata_or_reachability_is_indeterminate():
    missing_timestamp = _evaluate([_merged_pr(merged_at="")])
    missing_reachability = _evaluate([_merged_pr()], reachable=None)

    assert missing_timestamp.status is PriorParentMergeStatus.INDETERMINATE
    assert missing_reachability.status is PriorParentMergeStatus.INDETERMINATE


def test_conflicting_canonical_head_and_closing_reference_is_indeterminate():
    result = _evaluate(
        [
            _merged_pr(
                head_ref="claude/issue-999-other-task",
                closes_issue_numbers=(101,),
            )
        ]
    )

    assert result.status is PriorParentMergeStatus.INDETERMINATE


def _task() -> Task:
    return Task(
        issue_number=101,
        subtask_id="child-task",
        footprint=(),
        symbols=(),
        risk=False,
        priority="medium",
        progress_partial=False,
        status_labels=(StatusLabel.QUEUED,),
        created_at="2026-09-01T00:00:00Z",
        parent_number=100,
    )


def _issue() -> IssueRecord:
    return IssueRecord(
        number=101,
        title="child",
        body="",
        labels=(StatusLabel.QUEUED,),
        created_at="2026-09-01T00:00:00Z",
        parent={"number": 100},
    )


def _forge_for_reconciliation(issue: IssueRecord) -> MagicMock:
    forge = MagicMock()
    forge.list_merged_prs_for_base.return_value = [_merged_pr()]
    forge.get_issue_last_reopened_at.return_value = None
    forge.is_merge_commit_reachable_from.return_value = True
    forge.get_issue.return_value = issue
    forge.list_comments.return_value = []
    return forge


def test_reconciliation_repairs_verified_merge_and_excludes_same_cycle_launch():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == {101}
    assert result.events[0]["action"] == "already_merged"
    forge.add_label.assert_called_once_with(101, StatusLabel.DONE)
    forge.remove_label.assert_called_once_with(101, StatusLabel.QUEUED)
    forge.close_issue.assert_called_once_with(101, "completed")
    assert forge.list_merged_prs_for_base.call_count == 2


def test_reconciliation_dry_run_does_not_mutate_and_reports_the_repair():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=False, issues_by_number={101: issue}
    )

    assert result.events[0]["action"] == "already_merged_dry_run"
    forge.add_label.assert_not_called()
    forge.add_comment.assert_not_called()
    forge.close_issue.assert_not_called()


def test_indeterminate_evidence_holds_only_its_own_task_without_mutation():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)
    forge.list_merged_prs_for_base.side_effect = RuntimeError("temporary API failure")

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == set()
    assert result.events[0]["action"] == "indeterminate"
    forge.add_label.assert_not_called()
    forge.close_issue.assert_not_called()


def test_partial_repair_failure_holds_without_marking_dependencies_completed():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)
    forge.add_label.side_effect = RuntimeError("temporary label API failure")

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == set()
    assert result.events[0]["action"] == "already_merged_repair_pending"


def test_closed_without_terminal_label_gets_label_normalized_without_reclose():
    """#862: an Issue closed externally between the initial scan and the

    fresh re-verification (but never labeled `status:done`/`status:not-needed`)
    must still have its label normalized in the same cycle, so live
    dependency-resolution precondition checks (which read current labels)
    recognize the completion — without re-closing or re-notifying.
    """
    issue = _issue()
    issue = dataclasses.replace(issue, state="CLOSED")
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == {101}
    forge.add_label.assert_called_once_with(101, StatusLabel.DONE)
    forge.remove_label.assert_called_once_with(101, StatusLabel.QUEUED)
    forge.close_issue.assert_not_called()
    forge.add_comment.assert_not_called()


def test_closed_with_terminal_label_already_present_is_a_no_op():
    issue = _issue()
    issue = dataclasses.replace(issue, state="CLOSED", labels=(StatusLabel.DONE,))
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == {101}
    forge.add_label.assert_not_called()
    forge.remove_label.assert_not_called()
    forge.close_issue.assert_not_called()
    forge.add_comment.assert_not_called()


def test_closed_with_terminal_label_but_stale_primary_finishes_cleanup_on_retry():
    """PR #863 review: a prior cycle may have added status:done but then

    failed to remove the stale status:queued label. That must not look like
    an already-normalized no-op forever; the leftover primary label needs to
    be cleaned up without re-adding status:done or re-closing/notifying.
    """
    issue = _issue()
    issue = dataclasses.replace(
        issue, state="CLOSED", labels=(StatusLabel.DONE, StatusLabel.QUEUED)
    )
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == {101}
    forge.add_label.assert_not_called()
    forge.remove_label.assert_called_once_with(101, StatusLabel.QUEUED)
    forge.close_issue.assert_not_called()
    forge.add_comment.assert_not_called()


def test_closed_label_normalization_failure_holds_without_marking_completed():
    issue = _issue()
    issue = dataclasses.replace(issue, state="CLOSED")
    forge = _forge_for_reconciliation(issue)
    forge.add_label.side_effect = RuntimeError("temporary label API failure")

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == set()
    assert result.events[0]["action"] == "already_merged_repair_pending"
    forge.close_issue.assert_not_called()


def test_closed_stale_primary_cleanup_failure_holds_for_retry_next_cycle():
    issue = _issue()
    issue = dataclasses.replace(
        issue, state="CLOSED", labels=(StatusLabel.DONE, StatusLabel.QUEUED)
    )
    forge = _forge_for_reconciliation(issue)
    forge.remove_label.side_effect = RuntimeError("temporary label API failure")

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == set()
    assert result.events[0]["action"] == "already_merged_repair_pending"
    forge.add_label.assert_not_called()
    forge.close_issue.assert_not_called()


def test_active_worktree_defers_repair_without_closing_or_marking_done():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge,
        {101: _task()},
        apply=True,
        issues_by_number={101: issue},
        active_issue_numbers=frozenset({101}),
    )

    assert result.held_issue_numbers == set()
    assert result.completed_issue_numbers == set()
    assert result.events == ()
    forge.list_merged_prs_for_base.assert_not_called()
    forge.close_issue.assert_not_called()


def test_initial_scan_shares_empty_parent_history_between_sibling_tasks():
    first = _issue()
    second = dataclasses.replace(first, number=102)
    forge = _forge_for_reconciliation(first)
    forge.list_merged_prs_for_base.return_value = []
    tasks = {
        101: _task(),
        102: dataclasses.replace(_task(), issue_number=102, subtask_id="second-child"),
    }

    result = reconcile_prior_parent_merges(
        forge,
        tasks,
        apply=False,
        issues_by_number={101: first, 102: second},
    )

    assert result.held_issue_numbers == set()
    forge.list_merged_prs_for_base.assert_called_once_with("parent/issue-100")


def test_initial_scan_separates_parent_histories_and_refetches_next_cycle():
    first = _issue()
    second = dataclasses.replace(_issue(), number=102, parent={"number": 200})
    forge = _forge_for_reconciliation(first)
    forge.list_merged_prs_for_base.return_value = []
    tasks = {
        101: _task(),
        102: dataclasses.replace(
            _task(), issue_number=102, subtask_id="second-child", parent_number=200
        ),
    }

    for _ in range(2):
        reconcile_prior_parent_merges(
            forge,
            tasks,
            apply=False,
            issues_by_number={101: first, 102: second},
        )

    assert forge.list_merged_prs_for_base.call_args_list == [
        (("parent/issue-100",), {}),
        (("parent/issue-200",), {}),
        (("parent/issue-100",), {}),
        (("parent/issue-200",), {}),
    ]


def test_initial_scan_shares_parent_history_failure_and_holds_siblings():
    first = _issue()
    second = dataclasses.replace(first, number=102)
    forge = _forge_for_reconciliation(first)
    forge.list_merged_prs_for_base.side_effect = RuntimeError("temporary API failure")
    tasks = {
        101: _task(),
        102: dataclasses.replace(_task(), issue_number=102, subtask_id="second-child"),
    }

    result = reconcile_prior_parent_merges(
        forge,
        tasks,
        apply=True,
        issues_by_number={101: first, 102: second},
    )

    assert result.held_issue_numbers == {101, 102}
    assert [event["action"] for event in result.events] == [
        "indeterminate",
        "indeterminate",
    ]
    forge.list_merged_prs_for_base.assert_called_once_with("parent/issue-100")
    forge.add_label.assert_not_called()
    forge.close_issue.assert_not_called()


def test_lazy_reopen_provider_not_called_when_pr_list_is_empty():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)
    forge.list_merged_prs_for_base.return_value = []

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == set()
    forge.get_issue_last_reopened_at.assert_not_called()


def test_lazy_reopen_provider_not_called_when_prs_do_not_match_issue():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)
    forge.list_merged_prs_for_base.return_value = [
        _merged_pr(head_ref="claude/issue-999-other", closes_issue_numbers=(999,))
    ]

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == set()
    forge.get_issue_last_reopened_at.assert_not_called()


def test_lazy_reopen_provider_not_called_when_candidate_fails_earlier_validation():
    provider = MagicMock(return_value=None)
    # PR with cross_repository=True, missing merged_at, or conflicting head/closing
    prs = [
        _merged_pr(is_cross_repository=True),
        _merged_pr(merged_at=""),
        _merged_pr(
            head_ref="claude/issue-999-other",
            closes_issue_numbers=(101,),
        ),
    ]

    result = evaluate_prior_parent_merge(
        issue_number=101,
        parent_issue_number=100,
        subtask_id="child-task",
        prs=prs,
        get_last_reopened_at=provider,
        merge_commit_is_reachable=lambda _sha, _base: True,
    )

    provider.assert_not_called()
    assert result.status is PriorParentMergeStatus.INDETERMINATE


def test_lazy_reopen_provider_called_at_most_once_per_evaluation_with_multiple_candidates():
    provider = MagicMock(return_value="2026-08-01T00:00:00Z")
    prs = [
        _merged_pr(number=301, merged_at="2026-09-01T12:00:00Z"),
        _merged_pr(number=302, merged_at="2026-09-01T13:00:00Z"),
    ]

    result = evaluate_prior_parent_merge(
        issue_number=101,
        parent_issue_number=100,
        subtask_id="child-task",
        prs=prs,
        get_last_reopened_at=provider,
        merge_commit_is_reachable=lambda _sha, _base: True,
    )

    assert result.status is PriorParentMergeStatus.ALREADY_MERGED
    assert result.pr_number == 302
    assert provider.call_count == 1


def test_lazy_reopen_provider_failure_fails_closed_as_indeterminate_without_mutation():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)
    forge.get_issue_last_reopened_at.side_effect = RuntimeError("API error on events")

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == set()
    assert result.events[0]["action"] == "indeterminate"
    forge.add_label.assert_not_called()
    forge.close_issue.assert_not_called()


def test_repair_refetches_reopen_timestamp_on_pre_mutation_revalidation():
    issue = _issue()
    forge = _forge_for_reconciliation(issue)

    result = reconcile_prior_parent_merges(
        forge, {101: _task()}, apply=True, issues_by_number={101: issue}
    )

    assert result.held_issue_numbers == {101}
    assert result.completed_issue_numbers == {101}
    # First call during initial scan, second call during _apply_or_preview_verified_repair re-verification
    assert forge.get_issue_last_reopened_at.call_count == 2
