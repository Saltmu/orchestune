from __future__ import annotations

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
        last_reopened_at=reopened_at,
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
